from flask import Flask, request, jsonify, render_template, redirect, url_for, send_from_directory, send_file
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from flask_mail import Mail, Message as MailMessage
import os
import json
from datetime import datetime, timedelta, date
import csv
from io import StringIO
import requests
import uuid 
from werkzeug.utils import secure_filename 
import urllib.parse 
from sqlalchemy import or_, inspect, text
import firebase_admin
from firebase_admin import credentials, messaging
import logging
import pandas as pd
from io import BytesIO
import threading

# =========================================================
# CONFIGURATION & SETUP
# =========================================================

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'fallback_secret_key_change_this')
CORS(app, supports_credentials=True)

# Email Configuration
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', 'your_email@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', 'your_google_app_password')
mail = Mail(app)

# Database Configuration
basedir = os.path.abspath(os.path.dirname(__file__))
data_dir = os.path.join(basedir, 'data')
if not os.path.exists(data_dir):
    try:
        os.makedirs(data_dir)
    except OSError:
        pass

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(data_dir, 'institute.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Upload Configuration
UPLOAD_FOLDER = os.path.join(basedir, 'uploads')
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_NOTE_EXTENSIONS = {'txt', 'pdf', 'doc', 'docx', 'ppt', 'pptx'}
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

# Auth Setup
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'serve_login_page'

@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except:
        return None

# =========================================================
# FIREBASE & NOTIFICATION LOGIC
# =========================================================

def init_firebase():
    """Initializes Firebase. Auto-repairs the Private Key newlines."""
    if firebase_admin._apps:
        return True

    # 1. Try Environment Variable
    firebase_env = os.environ.get('FIREBASE_CREDENTIALS_JSON')
    if firebase_env:
        try:
            val = firebase_env.strip()
            
            # Remove wrapping quotes if they exist (common Coolify issue)
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]

            # Fix 1: Un-escape quotes so JSON parsing works
            val = val.replace('\\"', '"')

            # Parse the JSON
            try:
                cred_dict = json.loads(val)
            except Exception:
                # Fallback for Python-style dictionaries
                import ast
                cred_dict = ast.literal_eval(val)

            # --- THE CRITICAL FIX ---
            # The Private Key must have REAL newlines, not string "\n" characters.
            # We fix this AFTER parsing the JSON to avoid breaking the file format.
            if 'private_key' in cred_dict:
                cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
            # ------------------------

            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
            logger.info("Firebase initialized from Environment Variable.")
            return True
        except Exception as e:
            logger.error(f"Firebase Env Init Failed. Error: {e}")

    # 2. Try Local File (Backup)
    possible_paths = [
        os.path.join(basedir, 'firebase_credentials.json'),
        'firebase_credentials.json'
    ]

    for path in possible_paths:
        if os.path.exists(path):
            try:
                cred = credentials.Certificate(path)
                firebase_admin.initialize_app(cred)
                logger.info(f"Firebase initialized from file: {path}")
                return True
            except Exception as e:
                logger.error(f"Firebase File Init Failed ({path}): {e}")

    logger.warning("Firebase credentials not found. Push notifications will not work.")
    return False

def send_push_notification(user_id, title, body):
    """Sends a Push Notification via Firebase Cloud Messaging."""
    if not init_firebase():
        return "Firebase not initialized (Check server logs)"

    with app.app_context():
        user = db.session.get(User, user_id)
        
        if not user: 
            return "User not found"
        if not user.fcm_token: 
            return "User has no App Token (Not logged in on mobile)"

        try:
            message = messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                token=user.fcm_token,
            )
            messaging.send(message)
            return "Sent"
        except Exception as e:
            error_msg = str(e)
            if 'registration-token-not-registered' in error_msg or 'invalid-argument' in error_msg:
                user.fcm_token = None # Clear invalid token
                db.session.commit()
                return "Token Invalid (Cleared)"
            logger.error(f"Push Error: {e}")
            return f"Error: {error_msg}"

def send_actual_sms(phone_number, message_body, template_id=None):
    """Sends SMS using external ServerMSG API."""
    base_url = os.environ.get('SMS_API_URL', 'http://servermsg.com/api/SmsApi/SendSingleApi')
    user_id = os.environ.get('SMS_API_USER_ID')
    password = os.environ.get('SMS_API_PASSWORD')
    sender_id = os.environ.get('SMS_API_SENDER_ID')
    entity_id = os.environ.get('SMS_API_ENTITY_ID')
    
    if not template_id:
        template_id = os.environ.get('SMS_API_DEFAULT_TEMPLATE_ID', '1707176388002841408')

    if not all([user_id, password, sender_id, entity_id, template_id, phone_number]):
        logger.warning(f"SMS Skipped for {phone_number}: Missing configuration.")
        return False

    payload = {
        'UserID': user_id, 'Password': password, 'SenderID': sender_id,
        'Phno': phone_number, 'Msg': message_body, 'EntityID': entity_id, 'TemplateID': template_id
    }

    try:
        response = requests.get(base_url, params=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"SMS Error: {e}")
        return False

def send_mock_whatsapp(user, subject, body):
    """Logs a mock WhatsApp message."""
    if user and user.phone_number:
        logger.info(f"[MOCK WHATSAPP] To: {user.name} ({user.phone_number}) | Msg: {subject} - {body}")
        return True
    return False

def allowed_file(filename, extension_set):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in extension_set

import threading # Add to imports

# Add this helper function
# Updated helper function
def background_notify(app_ctx, user_ids, subject, body):
    with app_ctx:
        # Re-query users inside the thread to be safe
        users = User.query.filter(User.id.in_(user_ids)).all()
        for u in users:
            send_push_notification(u.id, subject, body)
            if u.phone_number: 
                send_actual_sms(u.phone_number, body)



# =========================================================
# DATABASE MODELS
# =========================================================

student_course_association = db.Table('student_course',
    db.Column('student_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('course_id', db.Integer, db.ForeignKey('course.id'), primary_key=True)
)

# 1. Update the User Model (Add session_id)
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(60), nullable=False)
    role = db.Column(db.String(50), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    phone_number = db.Column(db.String(20), nullable=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    dob = db.Column(db.String(20), nullable=True)
    profile_photo_url = db.Column(db.String(300), nullable=True)
    gender = db.Column(db.String(20), nullable=True)
    father_name = db.Column(db.String(100), nullable=True)
    mother_name = db.Column(db.String(100), nullable=True)
    address_line1 = db.Column(db.String(200), nullable=True)
    city = db.Column(db.String(100), nullable=True)
    state = db.Column(db.String(100), nullable=True)
    pincode = db.Column(db.String(20), nullable=True)
    fcm_token = db.Column(db.String(500), nullable=True)
    
    # --- CRITICAL FIX: Add session_id ---
    session_id = db.Column(db.Integer, db.ForeignKey('academic_session.id'), nullable=True)
    
    children = db.relationship('User', foreign_keys=[parent_id], backref=db.backref('parent', remote_side=[id]))
    courses_enrolled = db.relationship('Course', secondary=student_course_association, lazy='subquery',
                                       backref=db.backref('students', lazy=True))

    def to_dict(self):
        # Fetch session name for the table view
        sess_name = "Unassigned"
        if self.session_id:
            sess = db.session.get(AcademicSession, self.session_id)
            if sess: sess_name = sess.name

        return {
            "id": self.id, "name": self.name, "email": self.email, "role": self.role,
            "created_at": self.created_at.strftime('%Y-%m-%d'),
            "phone_number": self.phone_number, "parent_id": self.parent_id,
            "dob": self.dob, "profile_photo_url": self.profile_photo_url,
            "gender": self.gender, "father_name": self.father_name,
            "mother_name": self.mother_name, "address_line1": self.address_line1,
            "city": self.city, "state": self.state, "pincode": self.pincode,
            "session_id": self.session_id, # Required for Edit form
            "session_name": sess_name,     # Required for Table display
            "course_ids": [c.id for c in self.courses_enrolled] if self.role == 'student' else []
        }

# 2. Update Migration Logic (At the bottom of app.py)
def check_and_upgrade_db():
    try:
        insp = inspect(db.engine)
        user_cols = [c['name'] for c in insp.get_columns('user')]
        with db.engine.connect() as conn:
            if 'session_id' not in user_cols:
                # Add column for existing database
                conn.execute(text("ALTER TABLE user ADD COLUMN session_id INTEGER REFERENCES academic_session(id)"))
                conn.commit()
                print("Database migrated: Added session_id to user table.")
    except Exception as e: print(f"Migration Error: {e}")

class Course(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    subjects = db.Column(db.String(300), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    teacher = db.relationship('User', backref=db.backref('courses', lazy=True))

    def to_dict(self):
        return {
            "id": self.id, "name": self.name,
            "subjects": [s.strip() for s in self.subjects.split(',')] if self.subjects else [],
            "teacher_id": self.teacher_id,
            "teacher_name": self.teacher.name if self.teacher else "Unassigned"
        }

class AcademicSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    start_date = db.Column(db.String(20), nullable=False)
    end_date = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), nullable=False)
    def to_dict(self): return {"id": self.id, "name": self.name, "start_date": self.start_date, "end_date": self.end_date, "status": self.status}

class Announcement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    content = db.Column(db.Text, nullable=False)
    target_group = db.Column(db.String(50), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    def to_dict(self): return {"id": self.id, "title": self.title, "content": self.content, "target_group": self.target_group, "created_at": self.created_at.strftime('%Y-%m-%d %H:%M')}

class FeeStructure(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    academic_session_id = db.Column(db.Integer, db.ForeignKey('academic_session.id'), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=True) # Linked Course
    total_amount = db.Column(db.Float, nullable=False)
    due_date = db.Column(db.Date, nullable=False, default=date.today)
    
    def to_dict(self):
        session = db.session.get(AcademicSession, self.academic_session_id)
        course = db.session.get(Course, self.course_id) if self.course_id else None
        return {
            "id": self.id, "name": self.name,
            "academic_session_id": self.academic_session_id,
            "session_name": session.name if session else "N/A",
            "course_id": self.course_id,
            "course_name": course.name if course else "Global",
            "total_amount": self.total_amount,
            "due_date": self.due_date.strftime('%Y-%m-%d') if self.due_date else None
        }

class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    fee_structure_id = db.Column(db.Integer, db.ForeignKey('fee_structure.id'), nullable=False)
    amount_paid = db.Column(db.Float, nullable=False)
    payment_date = db.Column(db.DateTime, default=datetime.utcnow)
    payment_method = db.Column(db.String(50), nullable=False)
    def to_dict(self):
        return {"id": self.id, "student_id": self.student_id, "amount_paid": self.amount_paid, "payment_date": self.payment_date.strftime('%Y-%m-%d %H:%M'), "payment_method": self.payment_method}

class Attendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    check_in_time = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(10), nullable=False)

class Grade(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    assessment_name = db.Column(db.String(100), nullable=False)
    marks_obtained = db.Column(db.Float, nullable=False)
    total_marks = db.Column(db.Float, nullable=False)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    recipient_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    content = db.Column(db.Text, nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    def to_dict(self):
        sender = db.session.get(User, self.sender_id)
        return {"id": self.id, "sender_name": sender.name if sender else "N/A", "recipient_id": self.recipient_id, "content": self.content, "sent_at": self.sent_at.strftime('%Y-%m-%d %H:%M')}

class SharedNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(300), nullable=False)
    original_filename = db.Column(db.String(300), nullable=False)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    course = db.relationship('Course', backref=db.backref('notes', lazy=True))
    teacher = db.relationship('User', backref=db.backref('notes', lazy=True))
    def to_dict(self):
        return {"id": self.id, "title": self.title, "description": self.description, "course_name": self.course.name if self.course else "N/A", "filename": self.filename, "original_filename": self.original_filename, "created_at": self.created_at.strftime('%Y-%m-%d')}
class TimeTable(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'))
    day_of_week = db.Column(db.String(20)) # Monday, Tuesday...
    start_time = db.Column(db.String(10)) # 10:00
    end_time = db.Column(db.String(10))   # 11:00
    room_number = db.Column(db.String(50))

class SyllabusLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'))
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    topic_covered = db.Column(db.String(300))
    log_date = db.Column(db.Date, default=date.today)

class Assignment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'))
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    title = db.Column(db.String(150))
    due_date = db.Column(db.Date)

class AssignmentSubmission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer)
    student_id = db.Column(db.Integer)
    status = db.Column(db.String(20), default='Pending')



# =========================================================
# BUSINESS LOGIC & HELPERS
# =========================================================
def calculate_fee_status(student_id):
    """Fee calculation: Sums Course Fees + Global Session Fees."""
    student = db.session.get(User, student_id)
    if not student:
        return {"total_due": 0, "total_paid": 0, "balance": 0, "due_date": "N/A", "pending_days": 0}

    # 1. Total Paid
    payments = Payment.query.filter_by(student_id=student_id).all()
    total_paid = sum(p.amount_paid for p in payments)

    total_due = 0.0
    due_dates = []

    # 2. Course Fees
    if student.courses_enrolled:
        for course in student.courses_enrolled:
            course_fees = FeeStructure.query.filter_by(course_id=course.id).all()
            for fee_struct in course_fees:
                total_due += fee_struct.total_amount
                if fee_struct.due_date:
                    due_dates.append(fee_struct.due_date)
    
    # 3. Global/Session Fees (FIXED: Now Enabled)
    # This adds Admission/Annual fees linked to the Session but NOT a specific course
    if student.session_id:
        global_fees = FeeStructure.query.filter(
            FeeStructure.course_id == None, 
            FeeStructure.academic_session_id == student.session_id
        ).all()
        for gf in global_fees:
            total_due += gf.total_amount
            if gf.due_date: due_dates.append(gf.due_date)

    # 4. Final Calculation
    final_due_date = min(due_dates) if due_dates else date.today()
    balance = total_due - total_paid

    pending_days = 0
    if balance > 0:
        today = date.today()
        if today > final_due_date:
            pending_days = (today - final_due_date).days * -1 
        else:
            pending_days = (final_due_date - today).days

    return {
        "total_due": total_due,
        "total_paid": total_paid,
        "balance": balance,
        "due_date": final_due_date.strftime('%Y-%m-%d') if due_dates else "N/A",
        "pending_days": pending_days
    }
def send_fee_alert_notifications(student_id):
    student = db.session.get(User, student_id)
    if not student: return False
    
    status = calculate_fee_status(student_id)
    parent = db.session.get(User, student.parent_id) if student.parent_id else None
    
    send_fee_alert_sms(student, status['balance'], status['due_date'])
    if parent:
        send_fee_alert_sms(parent, status['balance'], status['due_date'])

    push_title = "Fee Reminder"
    push_body = f"Fee of Rs {status['balance']:.2f} pending. Due: {status['due_date']}."
    
    send_push_notification(student.id, push_title, push_body)
    if parent:
        send_push_notification(parent.id, f"Child Alert: {push_title}", push_body)
    
    return True

def send_fee_alert_sms(user, balance, due_date):
    if user and user.phone_number:
        if isinstance(due_date, str):
            try: d_obj = datetime.strptime(due_date, '%Y-%m-%d'); formatted_date = d_obj.strftime('%d-%b-%Y')
            except: formatted_date = due_date
        else:
            formatted_date = due_date.strftime('%d-%b-%Y') if due_date else "N/A"

        clean_balance = int(balance) 
        message = f"Dear {user.name}, your fee of Rs {clean_balance} is pending. Due: {formatted_date}. CST Institute"
        return send_actual_sms(user.phone_number, message)
    return False

def process_bulk_users(file_stream):
    """Parses CSV and creates users."""
    stream = StringIO(file_stream.decode('utf-8'))
    reader = csv.DictReader(stream)
    users_added = []
    users_failed = []
    rows = list(reader)

    # First pass: Non-students
    for row in rows:
        if row.get('role', '').lower() != 'student':
            try:
                if User.query.filter_by(email=row['email']).first(): continue
                pw = bcrypt.generate_password_hash(row['password']).decode('utf-8')
                u = User(name=row['name'], email=row['email'], password=pw, role=row['role'], phone_number=row.get('phone_number'))
                db.session.add(u)
                users_added.append(row['email'])
            except Exception as e: users_failed.append(f"{row['email']}: {e}")
    db.session.commit()

    # Second pass: Students
    for row in rows:
        if row.get('role', '').lower() == 'student':
            try:
                if User.query.filter_by(email=row['email']).first(): continue
                parent_id = None
                if row.get('parent_email'):
                    p = User.query.filter_by(email=row['parent_email']).first()
                    if p: parent_id = p.id
                
                pw = bcrypt.generate_password_hash(row['password']).decode('utf-8')
                u = User(name=row['name'], email=row['email'], password=pw, role='student', 
                         phone_number=row.get('phone_number'), parent_id=parent_id,
                         gender=row.get('gender'), father_name=row.get('father_name'), mother_name=row.get('mother_name'),
                         address_line1=row.get('address_line1'), city=row.get('city'), state=row.get('state'), pincode=row.get('pincode'), dob=row.get('dob'))
                db.session.add(u)
                
                if row.get('course_ids'):
                    cids = [int(c) for c in row['course_ids'].split(',') if c.strip().isdigit()]
                    courses = Course.query.filter(Course.id.in_(cids)).all()
                    u.courses_enrolled.extend(courses)
                    
                users_added.append(row['email'])
            except Exception as e: users_failed.append(f"{row['email']}: {e}")
    
    db.session.commit()
    return {"added": users_added, "failed": users_failed}

# =========================================================
# ROUTES
# =========================================================

@app.route('/')
def serve_login_page():
    if current_user.is_authenticated:
        return redirect(f"/{current_user.role}")
    return render_template('login.html')

@app.route('/admin/id_card/<int:student_id>')
@login_required
def generate_id_card(student_id):
    # Security: Only Admin or the Student themselves can view the card
    if current_user.role != 'admin' and current_user.id != student_id:
        return jsonify({"msg": "Access Denied"}), 403
        
    student = db.session.get(User, student_id)
    if not student:
        return "Student not found", 404
        
    return render_template('id_card.html', student=student)

@app.route('/admin/id_cards/bulk')
@login_required
def bulk_ids():
    if current_user.role != 'admin': return "Denied", 403
    
    query = User.query.filter_by(role='student')
    
    # FILTER LOGIC: Only filter if session_id is a valid number
    sid = request.args.get('session_id')
    if sid and sid != 'null' and sid != '':
        query = query.filter_by(session_id=int(sid))
    
    students = query.all()
    
    # Debug: Print count to console
    print(f"Generating ID cards for {len(students)} students.")
    
    return render_template('id_cards_bulk.html', students=students)

@app.route('/api/admin/student', methods=['POST'])
@login_required
def save_student():
    if current_user.role != 'admin':
        return jsonify({"error": "Unauthorized"}), 403

    form = request.form
    student_id = form.get('id')

    if student_id:
        student = db.session.get(User, int(student_id))
        if not student:
            return jsonify({"error": "Student not found"}), 404
    else:
        student = User(role='student')

    # BASIC DETAILS
    student.name = form['name']
    student.email = form['email']
    student.phone_number = form.get('phone_number')
    student.dob = form.get('dob')
    student.gender = form.get('gender')

    # PASSWORD (only if entered)
    if form.get('password'):
        student.password = bcrypt.generate_password_hash(
            form['password']
        ).decode('utf-8')

    # PARENT LINK
    student.parent_id = form.get('parent_id') or None

    # COURSE ENROLLMENT
    course_ids = request.form.getlist('course_ids')
    student.courses_enrolled = Course.query.filter(
        Course.id.in_(course_ids)
    ).all()

    # PHOTO UPLOAD (only if new file)
    file = request.files.get('profile_photo_file')
    if file and allowed_file(file.filename, ALLOWED_IMAGE_EXTENSIONS):
        filename = secure_filename(file.filename)
        path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(path)
        student.profile_photo_url = f"/uploads/{filename}"

    db.session.add(student)
    db.session.commit()

    return jsonify({"success": True})

@app.route('/<role>')
@login_required
def serve_role_page(role):
    if role in ['admin', 'teacher', 'student', 'parent'] and current_user.role == role:
        return render_template(f'{role}.html')
    return redirect('/')

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    u = User.query.filter_by(email=data.get('email')).first()
    if u and bcrypt.check_password_hash(u.password, data.get('password')):
        login_user(u, remember=True)
        return jsonify({"message": "OK", "user": u.to_dict()})
    return jsonify({"message": "Invalid credentials"}), 401

@app.route('/api/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify({"message": "Logged out"})

@app.route('/api/check_session')
def check_session():
    if current_user.is_authenticated: return jsonify({"logged_in": True, "user": current_user.to_dict()})
    return jsonify({"logged_in": False}), 401

@app.route('/api/save_fcm_token', methods=['POST'])
@login_required
def save_fcm_token():
    data = request.json
    token = data.get('token')

    if not token:
        return jsonify({"message": "Token missing"}), 400

    current_user.fcm_token = token
    db.session.commit()

    return jsonify({"message": "FCM token saved successfully"})


@app.route('/firebase-messaging-sw.js')
def sw(): return send_from_directory(app.static_folder, 'firebase-messaging-sw.js')

@app.route('/healthz', methods=['GET'])
def health_check(): return "OK", 200

# --- ADMIN ROUTES ---
@app.route('/api/users', methods=['GET', 'POST', 'PUT'])
@login_required
def api_users():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    
    # --- GET USERS ---
    if request.method == 'GET':
        search = request.args.get('search', '').lower()
        session_id = request.args.get('session_id')
        q = User.query
        
        if session_id and session_id != 'null' and session_id != '':
             if hasattr(User, 'session_id'):
                 q = q.filter_by(session_id=int(session_id))

        if search: 
            q = q.filter(or_(User.name.ilike(f'%{search}%'), User.email.ilike(f'%{search}%')))
            
        return jsonify([u.to_dict() for u in q.all()])

    # --- CREATE USER (POST) ---
    if request.method == 'POST':
        d = request.form
        if User.query.filter_by(email=d['email']).first(): return jsonify({"msg": "Email exists"}), 400
        pw = bcrypt.generate_password_hash(d['password']).decode('utf-8')

        photo_url = None
        if 'profile_photo_file' in request.files:
            f = request.files['profile_photo_file']
            if allowed_file(f.filename, ALLOWED_IMAGE_EXTENSIONS):
                fn = secure_filename(f.filename)
                uid = f"{uuid.uuid4()}{os.path.splitext(fn)[1]}"
                f.save(os.path.join(app.config['UPLOAD_FOLDER'], uid))
                photo_url = f"/uploads/{uid}"

        u = User(
            name=d['name'], email=d['email'], password=pw, role=d['role'], 
            phone_number=d.get('phone_number'),
            parent_id=d.get('parent_id') if d.get('parent_id') else None,
            profile_photo_url=photo_url, dob=d.get('dob'), gender=d.get('gender'),
            father_name=d.get('father_name'), mother_name=d.get('mother_name'), 
            address_line1=d.get('address_line1'), city=d.get('city'), 
            state=d.get('state'), pincode=d.get('pincode')
        )
        if hasattr(User, 'session_id') and d.get('session_id'):
            u.session_id = int(d.get('session_id'))

        db.session.add(u)
        
        if u.role == 'student':
            c_ids = request.form.getlist('course_ids')
            if not c_ids and d.get('course_ids'): c_ids = [d.get('course_ids')]
            if c_ids:
                valid_ids = [int(cid) for cid in c_ids if cid.isdigit()]
                if valid_ids:
                    courses = Course.query.filter(Course.id.in_(valid_ids)).all()
                    u.courses_enrolled.extend(courses)

        db.session.commit()
        return jsonify(u.to_dict()), 201

    # --- UPDATE USER (PUT) ---
    if request.method == 'PUT':
        d = request.form
        u = db.session.get(User, int(d['id']))
        if not u: return jsonify({"msg": "Not found"}), 404

        u.name = d.get('name', u.name)
        u.email = d.get('email', u.email)
        u.phone_number = d.get('phone_number', u.phone_number)
        u.dob = d.get('dob', u.dob)
        u.gender = d.get('gender', u.gender)
        u.father_name = d.get('father_name', u.father_name)
        u.mother_name = d.get('mother_name', u.mother_name)
        u.address_line1 = d.get('address_line1', u.address_line1)
        u.city = d.get('city', u.city)
        u.state = d.get('state', u.state)
        u.pincode = d.get('pincode', u.pincode)

        if hasattr(User, 'session_id') and d.get('session_id'):
            u.session_id = int(d.get('session_id'))

        if d.get('parent_id'): 
            try: u.parent_id = int(d.get('parent_id'))
            except: u.parent_id = None
        
        if d.get('password'): 
            u.password = bcrypt.generate_password_hash(d.get('password')).decode('utf-8')

        if 'profile_photo_file' in request.files:
            file = request.files['profile_photo_file']
            if file and allowed_file(file.filename, ALLOWED_IMAGE_EXTENSIONS):
                filename = secure_filename(file.filename)
                uid = f"{uuid.uuid4()}{os.path.splitext(filename)[1]}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], uid))
                u.profile_photo_url = f"/uploads/{uid}"

        if u.role == 'student':
            c_ids = request.form.getlist('course_ids')
            if not c_ids and d.get('course_ids'): c_ids = [d.get('course_ids')]
            
            # Reset and re-add courses
            if c_ids:
                u.courses_enrolled = [] 
                valid_ids = [int(cid) for cid in c_ids if cid.isdigit()]
                if valid_ids:
                    courses = Course.query.filter(Course.id.in_(valid_ids)).all()
                    u.courses_enrolled.extend(courses)

        db.session.commit()
        return jsonify(u.to_dict()), 200
@app.route('/api/admin/student/<int:id>', methods=['GET'])
@login_required
def get_student(id):
    if current_user.role != 'admin':
        return jsonify({"error": "Unauthorized"}), 403

    student = db.session.get(User, id)
    if not student or student.role != 'student':
        return jsonify({"error": "Student not found"}), 404

    return jsonify(student.to_dict())

@app.route('/api/users/<int:id>', methods=['DELETE'])
@login_required
def delete_user(id):
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    u = db.session.get(User, id)
    if not u: return jsonify({"msg": "Not found"}), 404
    
    # Cascade Delete Manual Handling
    if u.role == 'student':
        Payment.query.filter_by(student_id=id).delete()
        Grade.query.filter_by(student_id=id).delete()
        Attendance.query.filter_by(student_id=id).delete()
        Message.query.filter(or_(Message.recipient_id==id, Message.sender_id==id)).delete()
        u.courses_enrolled = []
    
    db.session.delete(u)
    db.session.commit()
    return jsonify({"msg": "Deleted"})

@app.route('/api/courses', methods=['GET', 'POST', 'PUT'])
@login_required
def manage_courses():
    if current_user.role not in ['admin', 'teacher']: return jsonify({"msg": "Denied"}), 403
    if request.method == 'GET':
        return jsonify([c.to_dict() for c in Course.query.all()])
        
    if request.method == 'POST':
        d = request.json
        c = Course(name=d['name'], subjects=d['subjects'], teacher_id=d.get('teacher_id'))
        db.session.add(c)
        db.session.commit()
        return jsonify(c.to_dict()), 201
        
    if request.method == 'PUT':
        d = request.json
        c = db.session.get(Course, int(d['id']))
        c.name = d['name']
        c.subjects = d['subjects']
        c.teacher_id = d.get('teacher_id')
        db.session.commit()
        return jsonify(c.to_dict()), 200

@app.route('/api/courses/<int:id>', methods=['DELETE'])
@login_required
def del_course(id):
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    db.session.delete(db.session.get(Course, id))
    db.session.commit()
    return jsonify({"msg": "Deleted"})

@app.route('/api/sessions', methods=['GET', 'POST', 'PUT'])
@login_required
def manage_sessions():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    if request.method == 'GET': return jsonify([s.to_dict() for s in AcademicSession.query.all()])
    
    d = request.json
    if request.method == 'POST':
        s = AcademicSession(name=d['name'], start_date=d['start_date'], end_date=d['end_date'], status=d['status'])
        db.session.add(s)
        db.session.commit()
        return jsonify(s.to_dict()), 201
        
    if request.method == 'PUT':
        s = db.session.get(AcademicSession, int(d['id']))
        s.name = d['name']
        s.start_date = d['start_date']
        s.end_date = d['end_date']
        s.status = d['status']
        db.session.commit()
        return jsonify(s.to_dict()), 200

@app.route('/api/sessions/<int:id>', methods=['DELETE'])
@login_required
def del_session(id):
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    db.session.delete(db.session.get(AcademicSession, id))
    db.session.commit()
    return jsonify({"msg": "Deleted"})

@app.route('/api/announcements', methods=['GET', 'POST'])
@login_required
def manage_announcements():
    if request.method == 'POST':
        if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
        d = request.json
        a = Announcement(title=d['title'], content=d['content'], target_group=d['target_group'])
        db.session.add(a)
        db.session.commit()
        
        # Notify
        users = User.query.all() if d['target_group'] == 'all' else User.query.filter_by(role=d['target_group'][:-1]).all()
        for u in users: send_push_notification(u.id, d['title'], d['content'][:100])
        
        return jsonify(a.to_dict()), 201
    return jsonify([a.to_dict() for a in Announcement.query.order_by(Announcement.created_at.desc()).all()])

@app.route('/api/announcements/<int:id>', methods=['DELETE'])
@login_required
def del_announcement(id):
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    db.session.delete(db.session.get(Announcement, id))
    db.session.commit()
    return jsonify({"msg": "Deleted"})

@app.route('/api/fee_structures', methods=['GET', 'POST', 'PUT'])
@login_required
def manage_fees():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    if request.method == 'GET': return jsonify([f.to_dict() for f in FeeStructure.query.all()])
    
    d = request.json
    dt = datetime.strptime(d['due_date'], '%Y-%m-%d').date()
    cid = int(d['course_id']) if d.get('course_id') else None
    
    if request.method == 'POST':
        f = FeeStructure(name=d['name'], academic_session_id=d['academic_session_id'], course_id=cid, total_amount=d['total_amount'], due_date=dt)
        db.session.add(f)
        db.session.commit()
        return jsonify(f.to_dict()), 201
        
    if request.method == 'PUT':
        f = db.session.get(FeeStructure, int(d['id']))
        f.name = d['name']
        f.academic_session_id = d['academic_session_id']
        f.course_id = cid
        f.total_amount = d['total_amount']
        f.due_date = dt
        db.session.commit()
        return jsonify(f.to_dict()), 200
        
@app.route('/api/fee_structures/<int:id>', methods=['DELETE'])
@login_required
def del_fee(id):
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    db.session.delete(db.session.get(FeeStructure, id))
    db.session.commit()
    return jsonify({"msg": "Deleted"})

@app.route('/api/payments', methods=['POST'])
@login_required
def record_payment():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    d = request.json
    p = Payment(student_id=d['student_id'], fee_structure_id=d['fee_structure_id'], amount_paid=d['amount_paid'], payment_method=d['payment_method'])
    db.session.add(p)
    db.session.commit()
    
    # Check Balance & Notify
    status = calculate_fee_status(d['student_id'])
    if status['balance'] > 0:
        send_push_notification(d['student_id'], "Fee Payment", f"Received {d['amount_paid']}. Remaining: {status['balance']}")
        send_fee_alert_sms(db.session.get(User, d['student_id']), status['balance'], status['due_date'])
        
    return jsonify({"message": "Recorded", "payment_id": p.id}), 201

@app.route('/api/fee_status', methods=['GET'])
@login_required
def fee_status():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    students = User.query.filter_by(role='student').all()
    res = []
    for s in students:
        st = calculate_fee_status(s.id)
        lp = Payment.query.filter_by(student_id=s.id).order_by(Payment.payment_date.desc()).first()
        res.append({"student_id": s.id, "student_name": s.name, "balance": st['balance'], "due_date": st['due_date'], "pending_days": st['pending_days'], "latest_payment_id": lp.id if lp else None})
    return jsonify(res)

# FIND THIS FUNCTION:
@app.route('/api/reports/fee_pending', methods=['GET'])
@login_required
def pending_report():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    
    # --- ADD THIS NEW FILTER LOGIC ---
    course_filter_id = request.args.get('course_id') # Get course ID from URL
    
    students = User.query.filter_by(role='student').all()
    res = []
    for s in students:
        # IF A COURSE IS SELECTED, SKIP STUDENTS NOT IN THAT COURSE
        if course_filter_id:
            try:
                # Check if student is enrolled in the specific course
                student_course_ids = [c.id for c in s.courses_enrolled]
                if int(course_filter_id) not in student_course_ids:
                    continue
            except:
                continue
        # -------------------------------

        st = calculate_fee_status(s.id)
        if st['balance'] > 0:
            res.append({
                "student_id": s.id, 
                "student_name": s.name, 
                "phone_number": s.phone_number, # Added phone for messaging
                "balance": st['balance'], 
                "due_date": st['due_date'], 
                "pending_days": st['pending_days']
            })
    return jsonify(res)

# --- ADD THIS NEW ROUTE BELOW THE ONE ABOVE FOR BULK MESSAGING ---
@app.route('/api/admin/notify_specific_list', methods=['POST'])
@login_required
def notify_specific_list():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    
    data = request.json
    student_ids = data.get('student_ids', [])
    message_body = data.get('message', '')
    
    if not student_ids or not message_body:
        return jsonify({"message": "Missing data"}), 400
        
    count = 0
    for sid in student_ids:
        user = db.session.get(User, sid)
        if user:
            # Send Push
            send_push_notification(user.id, "Fee Reminder", message_body)
            # Send SMS (if phone exists)
            if user.phone_number:
                send_actual_sms(user.phone_number, message_body)
            count += 1
            
    return jsonify({"message": f"Sent reminders to {count} students."})

@app.route('/api/send_sms_alert', methods=['POST'])
@login_required
def manual_sms():
    d = request.json
    s = db.session.get(User, d['student_id'])
    st = calculate_fee_status(s.id)
    send_fee_alert_sms(s, st['balance'], st['due_date'])
    send_push_notification(s.id, "Fee Reminder", f"Please pay pending fee: {st['balance']}")
    return jsonify({"message": "Sent"})

# Update the route
@app.route('/api/admin/bulk_notify', methods=['POST'])
@login_required
def bulk_notify():
    d = request.json
    users = User.query.all() if d['target_role'] == 'all' else User.query.filter_by(role=d['target_role'][:-1]).all()
    
    # EXTRACT IDs HERE
    user_ids = [u.id for u in users]
    
    # Pass user_ids instead of users
    threading.Thread(target=background_notify, args=(app.app_context(), user_ids, d['subject'], d['body'])).start()
    
    return jsonify({"message": "Sending started in background!"})

@app.route('/api/bulk_upload/users', methods=['POST'])
@login_required
def bulk_upload():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    f = request.files['file']
    res = process_bulk_users(f.read())
    return jsonify(res)

# --- TEACHER ROUTES ---
@app.route('/api/teacher/courses', methods=['GET'])
@login_required
def t_courses():
    c = Course.query.filter_by(teacher_id=current_user.id).all()
    return jsonify([x.to_dict() for x in c])

@app.route('/api/teacher/students', methods=['GET'])
@login_required
def t_students():
    t_c_ids = [c.id for c in Course.query.filter_by(teacher_id=current_user.id).all()]
    s = User.query.join(student_course_association).join(Course).filter(User.role=='student', Course.id.in_(t_c_ids)).distinct().all()
    return jsonify([x.to_dict() for x in s])

@app.route('/api/teacher/attendance', methods=['POST'])
@login_required
def t_attendance():
    d = request.json
    dt = datetime.strptime(d['date'], '%Y-%m-%d').date()
    cnt = 0
    for r in d['attendance_data']:
        ex = Attendance.query.filter_by(student_id=r['student_id']).filter(db.func.date(Attendance.check_in_time)==dt).first()
        if ex: ex.status = r['status']
        else: db.session.add(Attendance(student_id=r['student_id'], check_in_time=datetime.combine(dt, datetime.min.time()), status=r['status']))
        
        if r['status'] == 'Absent':
            send_push_notification(r['student_id'], "Attendance", f"Marked Absent on {d['date']}")
            cnt += 1
        elif r['status'] == 'Present':
            send_push_notification(r['student_id'], "Attendance", f"Marked Present on {d['date']}")
            
    db.session.commit()
    return jsonify({"message": f"Saved. Sent {cnt} Absent Alerts."})
@app.route('/api/teacher/notify', methods=['POST'])
@login_required
def teacher_notify():
    if current_user.role != 'teacher':
        return jsonify({"message": "Access denied"}), 403

    d = request.json
    student_id = d.get('student_id')
    subject = d.get('subject')
    body = d.get('body')
    msg_type = d.get('type')

    student = db.session.get(User, student_id)
    if not student:
        return jsonify({"message": "Student not found"}), 404

    if msg_type == 'portal':
        result = send_push_notification(student_id, subject, body)

    elif msg_type == 'sms':
        send_actual_sms(student.phone_number, body)
        result = "SMS Sent"

    elif msg_type == 'email':
        msg = MailMessage(subject, recipients=[student.email], body=body)
        mail.send(msg)
        result = "Email Sent"

    else:
        result = "Invalid message type"

    return jsonify({"message": result})

@app.route('/api/teacher/syllabus', methods=['POST'])
@login_required
def save_syllabus():
    if current_user.role != 'teacher':
        return jsonify({"error": "Unauthorized"}), 403

    data = request.json

    log = SyllabusLog(
        course_id=data['course_id'],
        teacher_id=current_user.id,
        topic_covered=data['topic']
    )
    db.session.add(log)
    db.session.commit()

    return jsonify({"success": True})

@app.route('/api/teacher/assignments/status')
@login_required
def assignment_status():
    assignments = Assignment.query.filter_by(
        teacher_id=current_user.id
    ).all()

    result = []
    for a in assignments:
        pending = AssignmentSubmission.query.filter_by(
            assignment_id=a.id,
            status='Pending'
        ).count()

        result.append({
            "title": a.title,
            "pending": pending
        })

    return jsonify(result)

@app.route('/api/teacher/reports/attendance', methods=['GET'])
@login_required
def t_report():
    t_c_ids = [c.id for c in Course.query.filter_by(teacher_id=current_user.id).all()]
    students = User.query.join(student_course_association).join(Course).filter(User.role=='student', Course.id.in_(t_c_ids)).distinct().all()
    res = []
    for s in students:
        tot = Attendance.query.filter_by(student_id=s.id).count()
        pres = Attendance.query.filter_by(student_id=s.id, status='Present').count()
        res.append({"student_id": s.id, "student_name": s.name, "phone_number": s.phone_number, "profile_photo_url": s.profile_photo_url, "total_classes": tot, "present": pres, "absent": tot-pres})
    return jsonify(res)

@app.route('/api/teacher/upload_note', methods=['POST'])
@login_required
def t_upload():
    f = request.files['file']
    fn = secure_filename(f.filename)
    uid = f"{uuid.uuid4()}{os.path.splitext(fn)[1]}"
    f.save(os.path.join(app.config['UPLOAD_FOLDER'], uid))
    
    n = SharedNote(filename=uid, original_filename=fn, title=request.form['title'], description=request.form['description'], course_id=int(request.form['course_id']), teacher_id=current_user.id)
    db.session.add(n)
    db.session.commit()
    
    c = db.session.get(Course, int(request.form['course_id']))
    for s in c.students:
        send_push_notification(s.id, "New Note", f"Material uploaded: {request.form['title']}")
        
    return jsonify({"message": "Uploaded"}), 201

@app.route('/api/teacher/notes', methods=['GET', 'DELETE'])
@login_required
def t_notes():
    if request.method == 'DELETE':
        n = db.session.get(SharedNote, int(request.args.get('id')))
        if n.teacher_id == current_user.id:
            db.session.delete(n)
            db.session.commit()
            return jsonify({"message": "Deleted"})
        return jsonify({"message": "Unauthorized"}), 403
    
    n = SharedNote.query.filter_by(teacher_id=current_user.id).all()
    return jsonify([x.to_dict() for x in n])

@app.route('/api/teacher/notify', methods=['POST'])
@login_required
def t_notify():
    d = request.json
    db.session.add(Message(sender_id=current_user.id, recipient_id=d['student_id'], content=d['body']))
    db.session.commit()
    
    res = send_push_notification(d['student_id'], d['subject'], d['body'])
    return jsonify({"message": f"Status: {res}"})

# --- STUDENT API ---
@app.route('/api/student/fees', methods=['GET'])
@login_required
def s_fees(): return jsonify(calculate_fee_status(current_user.id))

@app.route('/api/student/attendance', methods=['GET'])
@login_required
def s_att():
    a = Attendance.query.filter_by(student_id=current_user.id).order_by(Attendance.check_in_time.desc()).limit(10).all()
    return jsonify([{"date": x.check_in_time.strftime('%Y-%m-%d'), "time": x.check_in_time.strftime('%H:%M'), "status": x.status} for x in a])

@app.route('/api/student/grades', methods=['GET'])
@login_required
def s_grades():
    g = Grade.query.filter_by(student_id=current_user.id).all()
    res = []
    for x in g:
        c = db.session.get(Course, x.course_id)
        res.append({"course_name": c.name if c else "N/A", "assessment_name": x.assessment_name, "marks_obtained": x.marks_obtained, "total_marks": x.total_marks})
    return jsonify(res)

@app.route('/api/student/notes', methods=['GET'])
@login_required
def s_notes():
    c_ids = [c.id for c in current_user.courses_enrolled]
    n = SharedNote.query.filter(SharedNote.course_id.in_(c_ids)).order_by(SharedNote.created_at.desc()).all()
    return jsonify([x.to_dict() for x in n])

# --- PARENT API ---
@app.route('/api/parent/children', methods=['GET'])
@login_required
def p_children():
    c = User.query.filter_by(parent_id=current_user.id).all()
    return jsonify([x.to_dict() for x in c])

@app.route('/api/parent/child_data/<int:id>', methods=['GET'])
@login_required
def p_data(id):
    s = db.session.get(User, id)
    if s.parent_id != current_user.id: return jsonify({"msg": "Unauthorized"}), 403
    att = [{"date": x.check_in_time.strftime('%Y-%m-%d'), "status": x.status} for x in Attendance.query.filter_by(student_id=id).limit(5).all()]
    grades = [] # Placeholder
    fee = calculate_fee_status(id)
    return jsonify({"profile": s.to_dict(), "attendance": att, "grades": grades, "fees": fee})

@app.route('/api/parent/messages', methods=['GET'])
@login_required
def p_msgs(): return jsonify([]) # Placeholder

# --- REPORTS API ---
@app.route('/api/reports/admissions', methods=['GET'])
@login_required
def report_admin():
    d = datetime.utcnow() - timedelta(days=30)
    u = User.query.filter(User.role=='student', User.created_at >= d).all()
    return jsonify([x.to_dict() for x in u])

@app.route('/api/reports/attendance', methods=['GET'])
@login_required
def report_att():
    s = User.query.filter_by(role='student').all()
    res = []
    for u in s:
        tot = Attendance.query.filter_by(student_id=u.id).count()
        pres = Attendance.query.filter_by(student_id=u.id, status='Present').count()
        pct = round((pres/tot)*100) if tot > 0 else 0
        res.append({"student_name": u.name, "total_classes": tot, "present": pres, "absent": tot-pres, "percentage": pct})
    return jsonify(res)

@app.route('/api/reports/performance', methods=['GET'])
@login_required
def report_perf():
    s = User.query.filter_by(role='student').all()
    res = []
    for u in s:
        g = Grade.query.filter_by(student_id=u.id).all()
        ob = sum(x.marks_obtained for x in g)
        tot = sum(x.total_marks for x in g)
        pct = round((ob/tot)*100) if tot > 0 else 0
        res.append({"student_name": u.name, "assessments_taken": len(g), "total_score": ob, "overall_percentage": pct})
    return jsonify(res)
@app.route('/api/export/<report_type>', methods=['GET'])
@login_required
def export_data(report_type):
    if current_user.role != 'admin':
        return jsonify({"msg": "Denied"}), 403

    output = BytesIO()
    filename = f"{report_type}_report.xlsx"
    df = pd.DataFrame()

    # 1. Logic for Fee Pending Report
    if report_type == 'fee_pending':
        course_filter_id = request.args.get('course_id')
        students = User.query.filter_by(role='student').all()
        data = []
        
        for s in students:
            # Apply Course Filter logic
            if course_filter_id:
                student_course_ids = [c.id for c in s.courses_enrolled]
                if int(course_filter_id) not in student_course_ids:
                    continue
            
            st = calculate_fee_status(s.id)
            if st['balance'] > 0:
                data.append({
                    "Student Name": s.name,
                    "Phone": s.phone_number,
                    "Email": s.email,
                    "Total Due": st['total_due'],
                    "Paid": st['total_paid'],
                    "Balance Pending": st['balance'],
                    "Due Date": st['due_date'],
                    "Days Overdue": abs(st['pending_days']) if st['pending_days'] < 0 else 0
                })
        df = pd.DataFrame(data)

    # 2. Logic for Attendance Report
    elif report_type == 'attendance':
        students = User.query.filter_by(role='student').all()
        data = []
        for s in students:
            tot = Attendance.query.filter_by(student_id=s.id).count()
            pres = Attendance.query.filter_by(student_id=s.id, status='Present').count()
            pct = round((pres/tot)*100) if tot > 0 else 0
            data.append({
                "Student Name": s.name,
                "Total Classes": tot,
                "Present": pres,
                "Absent": tot-pres,
                "Percentage": f"{pct}%"
            })
        df = pd.DataFrame(data)

    # 3. Logic for Student List
    elif report_type == 'students':
        students = User.query.filter_by(role='student').all()
        data = []
        for s in students:
            parent = db.session.get(User, s.parent_id) if s.parent_id else None
            courses = ", ".join([c.name for c in s.courses_enrolled])
            data.append({
                "ID": s.id,
                "Name": s.name,
                "Email": s.email,
                "Phone": s.phone_number,
                "Courses": courses,
                "Parent Name": parent.name if parent else "N/A",
                "Parent Phone": parent.phone_number if parent else "N/A"
            })
        df = pd.DataFrame(data)

    # --- Generate Excel ---
    if df.empty:
        return "No data to export", 404

    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Report')

    output.seek(0)
    
    return send_file(
        output,
        download_name=filename,
        as_attachment=True,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@app.route('/api/user/upload_photo/<int:user_id>', methods=['POST'])
@login_required
def user_photo(user_id):
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    u = db.session.get(User, user_id)
    f = request.files['profile_photo_file']
    if f and allowed_file(f.filename, ALLOWED_IMAGE_EXTENSIONS):
        uid = f"{uuid.uuid4()}{os.path.splitext(f.filename)[1]}"
        f.save(os.path.join(app.config['UPLOAD_FOLDER'], uid))
        u.profile_photo_url = f"/uploads/{uid}"
        db.session.commit()
    return jsonify({"user": u.to_dict()})

@app.route('/uploads/<filename>')
def serve_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# FIND THIS FUNCTION:
@app.route('/api/receipt/<int:id>')
@login_required
def serve_receipt(id):
   
    p = db.session.get(Payment, id)
    if not p:
        return "Receipt not found", 404
        
    student = db.session.get(User, p.student_id)
    fee_struct = db.session.get(FeeStructure, p.fee_structure_id)
    
    # Simple CSS for the receipt
    html = f"""
    <html>
    <head>
        <title>Payment Receipt #{p.id}</title>
        <style>
            body {{ font-family: 'Helvetica', sans-serif; padding: 40px; color: #333; }}
            .receipt-box {{ border: 2px solid #333; padding: 20px; max-width: 600px; margin: 0 auto; }}
            .header {{ text-align: center; border-bottom: 1px solid #ccc; padding-bottom: 20px; margin-bottom: 20px; }}
            .row {{ display: flex; justify-content: space-between; margin-bottom: 10px; }}
            .label {{ font-weight: bold; }}
            .footer {{ margin-top: 30px; text-align: center; font-size: 12px; color: #777; }}
            .paid-stamp {{ color: green; font-weight: bold; border: 2px solid green; padding: 5px 10px; display: inline-block; transform: rotate(-10deg); margin-top: 20px; }}
        </style>
    </head>
    <body>
        <div class="receipt-box">
            <div class="header">
                <h2>CST Institute</h2>
                <p>Payment Receipt</p>
            </div>
            
            <div class="row"><span class="label">Receipt No:</span> <span>#{p.id}</span></div>
            <div class="row"><span class="label">Date:</span> <span>{p.payment_date.strftime('%Y-%m-%d %H:%M')}</span></div>
            <hr>
            <div class="row"><span class="label">Student Name:</span> <span>{student.name if student else 'Unknown'}</span></div>
            <div class="row"><span class="label">Student Email:</span> <span>{student.email if student else 'N/A'}</span></div>
            <div class="row"><span class="label">Fee Category:</span> <span>{fee_struct.name if fee_struct else 'General'}</span></div>
            <hr>
            <div class="row"><span class="label">Payment Method:</span> <span>{p.payment_method}</span></div>
            <div class="row" style="font-size: 1.2em; margin-top: 10px;">
                <span class="label">Amount Paid:</span> 
                <span>₹{p.amount_paid:.2f}</span>
            </div>
            
            <div style="text-align: center;">
                <div class="paid-stamp">PAID SUCCESSFUL</div>
            </div>

            <div class="footer">
                <p>This is a computer-generated receipt.</p>
                <button onclick="window.print()" style="margin-top:10px; padding: 5px 10px; cursor: pointer;">Print Receipt</button>
            </div>
        </div>
    </body>
    </html>
    """
    return html

# --- DEBUG & SETUP ---
@app.route('/debug/firebase')
def debug_fb():
    f = os.path.exists(os.path.join(basedir, 'firebase_credentials.json'))
    return jsonify({"Credential File Exists": f, "Firebase Init": bool(firebase_admin._apps)})

def check_and_upgrade_db():
    try:
        insp = inspect(db.engine)
        user_cols = [c['name'] for c in insp.get_columns('user')]
        
        with db.engine.connect() as conn:
            if 'fcm_token' not in user_cols:
                conn.execute(text("ALTER TABLE user ADD COLUMN fcm_token VARCHAR(500)"))
            
            # --- FIX: Create session_id if missing ---
            if 'session_id' not in user_cols:
                conn.execute(text("ALTER TABLE user ADD COLUMN session_id INTEGER REFERENCES academic_session(id)"))
                
            conn.commit()
            
        fee_cols = [c['name'] for c in insp.get_columns('fee_structure')]
        if 'course_id' not in fee_cols:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE fee_structure ADD COLUMN course_id INTEGER REFERENCES course(id)"))
                conn.commit()
    except Exception as e: print(f"Migration Error: {e}")

def initialize_database():
    with app.app_context():
        db.create_all()
        check_and_upgrade_db()
        init_firebase()

# Create DB tables automatically when app loads (Required for Gunicorn/Production)
with app.app_context():
    db.create_all()
    check_and_upgrade_db()
    # init_firebase() 

if __name__ == '__main__':
    initialize_database()
    app.run(debug=True, host='0.0.0.0', port=5000)