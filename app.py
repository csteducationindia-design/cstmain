from flask import Flask, request, jsonify, render_template, redirect, url_for, send_from_directory
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

# Set up logging to show in Coolify console
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Basic Setup ---
app = Flask(__name__, template_folder='templates')
app.config['SECRET_KEY'] = 'a_very_secret_key_that_should_be_changed'
CORS(app, supports_credentials=True)

# --- Email Configuration ---
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', 'your_email@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', 'your_google_app_password')
mail = Mail(app)

# --- Database Configuration ---
basedir = os.path.abspath(os.path.dirname(__file__))

# Define the persistent data directory
data_dir = os.path.join(basedir, 'data')

# Create the directory if it doesn't exist
if not os.path.exists(data_dir):
    try:
        os.makedirs(data_dir)
        print(f"--- Created persistent data directory: {data_dir} ---")
    except OSError as e:
        print(f"--- Error creating data directory: {e} ---")

# Save the DB inside the persistent 'data' folder
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(data_dir, 'institute.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- FIREBASE INITIALIZATION HELPER ---
def init_firebase():
    """Initializes Firebase from Env Var OR JSON File"""
    if not firebase_admin._apps:
        try:
            # 1. Try Environment Variable first
            firebase_env = os.environ.get('FIREBASE_CREDENTIALS_JSON')
            if firebase_env:
                cred_dict = json.loads(firebase_env.strip("'").strip('"'))
                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
                logger.info("--- Firebase Initialized from ENV VAR ---")
                return True
            
            # 2. Try Local File (fallback)
            # Look for the specific filename you uploaded
            cred_path = os.path.join(basedir, 'firebase_credentials.json.json') 
            if not os.path.exists(cred_path):
                # Try the standard name just in case
                cred_path = os.path.join(basedir, 'firebase_credentials.json')
                
            if os.path.exists(cred_path):
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred)
                logger.info(f"--- Firebase Initialized from FILE: {cred_path} ---")
                return True
            else:
                logger.warning("--- Firebase Credentials NOT FOUND (Env or File) ---")
                return False
                
        except Exception as e:
            logger.error(f"--- Firebase Init FAILED: {e} ---")
            return False
    return True

# --- MIGRATION UTILITY ---
def check_and_upgrade_db():
    """Checks for missing columns and adds them if necessary."""
    try:
        inspector = inspect(db.engine)
        
        # 1. Check User Table Columns
        if inspector.has_table("user"):
            columns = [col['name'] for col in inspector.get_columns('user')]
            
            with db.engine.connect() as conn:
                def add_column(conn, table, column, type):
                    if column not in columns:
                        print(f"--- MIGRATING DB: Adding {column} column to {table}... ---")
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {type}"))
                        conn.commit()

                add_column(conn, 'user', 'fcm_token', 'VARCHAR(500)')
                add_column(conn, 'user', 'pincode', 'VARCHAR(20)')
                add_column(conn, 'user', 'dob', 'VARCHAR(20)') 
                add_column(conn, 'user', 'profile_photo_url', 'VARCHAR(300)')
                add_column(conn, 'user', 'gender', 'VARCHAR(20)')
                add_column(conn, 'user', 'father_name', 'VARCHAR(100)')
                add_column(conn, 'user', 'mother_name', 'VARCHAR(100)')
                add_column(conn, 'user', 'address_line1', 'VARCHAR(200)')
                add_column(conn, 'user', 'city', 'VARCHAR(100)')
                add_column(conn, 'user', 'state', 'VARCHAR(100)')

        # 2. Check Fee Structure Table Columns (CRITICAL FIX FOR FEE LOGIC)
        if inspector.has_table("fee_structure"):
            columns = [col['name'] for col in inspector.get_columns('fee_structure')]
            if 'course_id' not in columns:
                print("--- MIGRATING DB: Adding course_id to fee_structure ---")
                with db.engine.connect() as conn:
                    conn.execute(text("ALTER TABLE fee_structure ADD COLUMN course_id INTEGER REFERENCES course(id)"))
                    conn.commit()

    except Exception as e:
        print(f"--- MIGRATION WARNING: {e} ---")
# -------------------------


# --- File Upload Configuration ---
UPLOAD_FOLDER = os.path.join(basedir, 'uploads')
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
# Separate allowed extensions for notes vs images
ALLOWED_NOTE_EXTENSIONS = {'txt', 'pdf', 'doc', 'docx', 'ppt', 'pptx'}
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

# --- Security and Login Manager Setup ---
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'serve_login_page'

# --- Third-Party SMS API Configuration (Uses Env Vars defined above) ---

@login_manager.user_loader
def load_user(user_id):
    """Loads user for Flask-Login."""
    try:
        user_id_int = int(user_id)
    except (ValueError, TypeError):
        return None
    return db.session.get(User, user_id_int)


# --- NEW ASSOCIATION TABLE ---
# Table to link Students (User) to Courses (Many-to-Many)
student_course_association = db.Table('student_course',
    db.Column('student_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('course_id', db.Integer, db.ForeignKey('course.id'), primary_key=True)
)


# --- Database Models ---
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(60), nullable=False)
    role = db.Column(db.String(50), nullable=False) # 'admin', 'teacher', 'student', 'parent'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    phone_number = db.Column(db.String(20), nullable=True)
    
    parent_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True) 
    
    can_edit = db.Column(db.Boolean, default=True) # Admin permission flag
    
    # --- EXPANDED STUDENT FIELDS ---
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
    # --- END EXPANDED FIELDS ---
    
    children = db.relationship('User', foreign_keys=[parent_id], backref=db.backref('parent', remote_side=[id]))

    # NEW: Relationship for many-to-many courses
    courses_enrolled = db.relationship('Course', secondary=student_course_association, lazy='subquery',
                                       backref=db.backref('students', lazy=True))

    def to_dict(self):
        """Serializes User object to dictionary."""
        return {
            "id": self.id, "name": self.name, "email": self.email, "role": self.role,
            "created_at": self.created_at.strftime('%Y-%m-%d'),
            "phone_number": self.phone_number, "parent_id": self.parent_id,
            "can_edit": self.can_edit,
            "dob": self.dob,
            "profile_photo_url": self.profile_photo_url,
            
            # --- NEW EXPANDED FIELDS ---
            "gender": self.gender,
            "father_name": self.father_name,
            "mother_name": self.mother_name,
            "address_line1": self.address_line1,
            "city": self.city,
            "state": self.state,
            "pincode": self.pincode,
            # Pass course IDs for frontend edit forms
            "course_ids": [c.id for c in self.courses_enrolled] if self.role == 'student' else []
        }

class Course(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    subjects = db.Column(db.String(300), nullable=False) # Comma-separated
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    teacher = db.relationship('User', backref=db.backref('courses', lazy=True))

    def to_dict(self):
        """Serializes Course object to dictionary."""
        return {
            "id": self.id, "name": self.name,
            # FIX: Ensure subjects field is split only if not None
            "subjects": [s.strip() for s in self.subjects.split(',')] if self.subjects else [],
            "teacher_id": self.teacher_id,
            "teacher_name": self.teacher.name if self.teacher else "Unassigned"
        }

class AcademicSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    start_date = db.Column(db.String(20), nullable=False) # Storing as string YYYY-MM-DD
    end_date = db.Column(db.String(20), nullable=False)   # Storing as string YYYY-MM-DD
    status = db.Column(db.String(20), nullable=False) # 'Active', 'Inactive'

    def to_dict(self):
        """Serializes AcademicSession object to dictionary."""
        return {
            "id": self.id, "name": self.name, "start_date": self.start_date,
            "end_date": self.end_date, "status": self.status
        }

class Announcement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    content = db.Column(db.Text, nullable=False)
    target_group = db.Column(db.String(50), nullable=False) # 'all', 'teachers', 'students', 'parents', 'admin'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        """Serializes Announcement object to dictionary."""
        return {
            "id": self.id, "title": self.title, "content": self.content,
            "target_group": self.target_group,
            "created_at": self.created_at.strftime('%Y-%m-%d %H:%M')
        }

class FeeStructure(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    academic_session_id = db.Column(db.Integer, db.ForeignKey('academic_session.id'), nullable=False)
    # NEW: Link fee to a course to prevent overwriting other courses' fees
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=True) 
    total_amount = db.Column(db.Float, nullable=False)
    due_date = db.Column(db.Date, nullable=False, default=date.today)

    def to_dict(self):
        """Serializes FeeStructure object to dictionary."""
        session = db.session.get(AcademicSession, self.academic_session_id)
        # NEW: Get course name for display
        course = db.session.get(Course, self.course_id) if self.course_id else None
        return {
            "id": self.id, "name": self.name,
            "academic_session_id": self.academic_session_id,
            "session_name": session.name if session else "N/A",
            "course_id": self.course_id,
            "course_name": course.name if course else "All Courses (Global)",
            "total_amount": self.total_amount,
            "due_date": self.due_date.strftime('%Y-%m-%d') if self.due_date else None
        }

class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    fee_structure_id = db.Column(db.Integer, db.ForeignKey('fee_structure.id'), nullable=False)
    amount_paid = db.Column(db.Float, nullable=False)
    payment_date = db.Column(db.DateTime, default=datetime.utcnow)
    payment_method = db.Column(db.String(50), nullable=False) # 'Cash', 'Card', 'Bank Transfer'

    def to_dict(self):
        """Serializes Payment object to dictionary."""
        return {
            "id": self.id, "student_id": self.student_id,
            "fee_structure_id": self.fee_structure_id,
            "amount_paid": self.amount_paid,
            "payment_date": self.payment_date.strftime('%Y-%m-%d %H:%M'),
            "payment_method": self.payment_method
        }

class Attendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    check_in_time = db.Column(db.DateTime, default=datetime.utcnow) # Stores date and time
    status = db.Column(db.String(10), nullable=False) # 'Present', 'Absent', 'Checked-In'

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
        """Serializes Message object to dictionary."""
        sender = db.session.get(User, self.sender_id)
        return {
            "id": self.id,
            "sender_id": self.sender_id,
            "sender_name": sender.name if sender else "N/A",
            "recipient_id": self.recipient_id,
            "content": self.content,
            "sent_at": self.sent_at.strftime('%Y-%m-%d %H:%M')
        }

class SharedNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(300), nullable=False) # Unique saved name (e.g., UUID.pdf)
    original_filename = db.Column(db.String(300), nullable=False) # Original upload name (e.g., notes.pdf)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=True)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    course = db.relationship('Course', backref=db.backref('notes', lazy=True))
    teacher = db.relationship('User', backref=db.backref('notes', lazy=True))

    def to_dict(self):
        """Serializes SharedNote object to dictionary."""
        return {
            "id": self.id,
            "filename": self.filename, 
            "original_filename": self.original_filename, 
            "title": self.title,
            "description": self.description,
            "course_id": self.course_id,
            "course_name": self.course.name if self.course else "N/A",
            "teacher_id": self.teacher_id,
            "teacher_name": self.teacher.name if self.teacher else "N/A",
            "created_at": self.created_at.strftime('%Y-%m-%d %H:%M')
        }

# --- Utility Functions ---

def allowed_file(filename, extension_set):
    """Checks if file extension is in the allowed set."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in extension_set

def send_fee_alert_sms(user, balance, due_date):
    if user and user.phone_number:
        # 1. Format Date 
        if isinstance(due_date, str):
            try:
                d_obj = datetime.strptime(due_date, '%Y-%m-%d')
                formatted_date = d_obj.strftime('%d-%b-%Y')
            except:
                formatted_date = due_date
        else:
            formatted_date = due_date.strftime('%d-%b-%Y') if due_date else "N/A"

        # 2. Format Amount
        clean_balance = int(balance) 

        # 3. Institute Phone Number (Update with actual support number)
        institute_phone = "9822826307" 

        # 4. Construct Message
        # Template: "Dear {#var#}, your fee of Rs {#var#} is pending. Due: {#var#}. CST Institute {#var#}"
        message = f"Dear {user.name}, your fee of Rs {clean_balance} is pending. Due: {formatted_date}. CST Institute {institute_phone}"
        
        # Fee Template ID
        fee_template_id = os.environ.get('SMS_API_FEE_TEMPLATE_ID', "1707176388002841408") # Default ID
        
        return send_actual_sms(user.phone_number, message, template_id=fee_template_id)
    return False

def send_actual_sms(phone_number, message_body, template_id=None):
    """
    Sends an SMS using ServerMSG API (Indian DLT Compliant).
    """
    # 1. Get Credentials from Environment Variables
    base_url = os.environ.get('SMS_API_URL', 'http://servermsg.com/api/SmsApi/SendSingleApi')
    user_id = os.environ.get('SMS_API_USER_ID')
    password = os.environ.get('SMS_API_PASSWORD')
    sender_id = os.environ.get('SMS_API_SENDER_ID')
    entity_id = os.environ.get('SMS_API_ENTITY_ID')
    
    # If no specific template ID is passed, try to use a default one 
    if not template_id:
        template_id = os.environ.get('SMS_API_DEFAULT_TEMPLATE_ID', '1707176388002841408')

    if not all([user_id, password, sender_id, entity_id, template_id, phone_number]):
        print(f"--- [SMS ERROR] Missing Config. Checked: UserID={bool(user_id)}, Pass={bool(password)}, Sender={bool(sender_id)}, Entity={bool(entity_id)}, Template={bool(template_id)} ---")
        return False

    # 2. Prepare Parameters
    payload = {
        'UserID': user_id,
        'Password': password,
        'SenderID': sender_id,
        'Phno': phone_number,
        'Msg': message_body,
        'EntityID': entity_id,
        'TemplateID': template_id
    }

    try:
        print(f"--- [SMS] Sending to {phone_number} via ServerMSG ---")
        response = requests.get(base_url, params=payload, timeout=10)
        
        if response.status_code == 200:
            print(f"--- [SMS SUCCESS] Response: {response.text} ---")
            return True
        else:
            print(f"--- [SMS FAILED] Status {response.status_code}: {response.text} ---")
            return False
    except Exception as e:
        print(f"--- [SMS EXCEPTION] {e} ---")
        return False

def send_mock_whatsapp(user, subject, body):
    """Mocks sending a WhatsApp message."""
    if user and user.phone_number:
        sender_role = current_user.role if current_user and current_user.is_authenticated else "System"
        sender_name = current_user.name if current_user and current_user.is_authenticated else "Admin"
        message = f"WhatsApp from {sender_role} ({sender_name}): {subject} - {body}"
        print(f"--- [MOCK WHATSAPP] to {user.name} ({user.phone_number}) ---\n{message}\n--------------------------------")
        return True
    return False

def send_push_notification(user_id, title, body):
    # 1. LAZY INITIALIZATION
    if not init_firebase():
        return False

    # 2. SEND PUSH
    with app.app_context(): # Ensure we are in app context to access DB
        user = db.session.get(User, user_id)
        
        if not user:
            logger.warning(f"Push Failed: User ID {user_id} not found.")
            return False
            
        if not user.fcm_token:
            # logger.warning(f"Push Failed: User {user.name} (ID: {user.id}) has NO FCM Token. They must log in to the App once.")
            return False

        try:
            message = messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                token=user.fcm_token,
            )
            response = messaging.send(message)
            logger.info(f"Push Sent to {user.name}: {response}")
            return True
        except Exception as e:
            logger.error(f"Push Error for {user.name}: {e}")
            # If token is invalid (stale), maybe clear it?
            if 'registration-token-not-registered' in str(e):
                logger.info(f"Token invalid for {user.name}, clearing it.")
                user.fcm_token = None
                db.session.commit()
            return False

def calculate_fee_status(student_id):
    """
    Calculates fee status based on enrolled courses.
    This fixes the issue where one fee structure overwrites another.
    Fees are now summed based on the courses the student is actually enrolled in.
    """
    student = db.session.get(User, student_id)
    if not student:
        return {"total_due": 0, "total_paid": 0, "balance": 0, "due_date": "N/A", "pending_days": 0}

    payments = Payment.query.filter_by(student_id=student_id).all()
    total_paid = sum(p.amount_paid for p in payments)

    total_due = 0.0
    due_dates = []

    # 1. Calculate fee based on Enrolled Courses
    # Iterates through student's courses and finds the fee structure assigned to each course.
    if student.courses_enrolled:
        for course in student.courses_enrolled:
            # Find the most recent fee structure assigned to this specific course
            fee_struct = FeeStructure.query.filter_by(course_id=course.id).order_by(FeeStructure.id.desc()).first()
            if fee_struct:
                total_due += fee_struct.total_amount
                if fee_struct.due_date:
                    due_dates.append(fee_struct.due_date)
    
    # 2. Optional: Check for Global Fees (Fees with no course_id, if any)
    # This ensures backward compatibility or miscellaneous fees
    global_fees = FeeStructure.query.filter(FeeStructure.course_id == None).all()
    # Uncomment next line if you want global fees to be added ON TOP of course fees
    # for gf in global_fees: total_due += gf.total_amount 

    # Determine earliest due date from the student's courses
    final_due_date = min(due_dates) if due_dates else date.today()
    balance = total_due - total_paid

    try:
        if balance > 0:
            today = date.today()
            if today > final_due_date:
                pending_days = (today - final_due_date).days * -1 # Overdue (negative)
            else:
                pending_days = (final_due_date - today).days # Pending (positive)
        else:
            pending_days = 0 
    except TypeError: 
        pending_days = 0

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
    
    # Send SMS (using existing logic from send_fee_alert_sms)
    student_alerted = send_fee_alert_sms(student, status['balance'], status['due_date'])
    if parent:
        parent_alerted = send_fee_alert_sms(parent, status['balance'], status['due_date'])

    # Send Push Notification
    push_title = "Fee Reminder"
    push_body = f"Fee of Rs {status['balance']:.2f} pending. Due: {status['due_date']}."
    
    send_push_notification(student.id, push_title, push_body)
    if parent:
        send_push_notification(parent.id, f"Child Alert: {push_title}", push_body)
    
    return student_alerted or (parent_alerted if parent else False)


# --- Bulk Upload Utility ---
def process_bulk_users(file_stream):
    """Parses CSV file stream and creates User objects."""
    stream = StringIO(file_stream.decode('utf-8'))
    reader = csv.DictReader(stream)

    users_added = []
    users_failed = []

    rows_to_process = list(reader) 

    # 1. Process Parents, Teachers, Admins (non-students) first
    for row in rows_to_process:
        role = row.get('role', '').strip().lower()
        if role in ['parent', 'teacher', 'admin']:
            try:
                name = row['name'].strip()
                email = row['email'].strip()
                password = row['password'].strip()
                phone_number = row.get('phone_number', '').strip() or None

                if not all([name, email, password]):
                    raise ValueError(f"Missing required field(s) for {role} {name}.")

                if User.query.filter_by(email=email).first():
                    users_failed.append(f"Email already exists: {email}")
                    continue

                hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')

                new_user = User(
                    name=name, email=email, password=hashed_password, role=role,
                    phone_number=phone_number, parent_id=None,
                    can_edit=False if role == 'admin' else True
                )
                db.session.add(new_user)

            except Exception as e:
                users_failed.append(f"Error creating {role} {row.get('name', 'N/A')}: {str(e)}")
                db.session.rollback() 

    try:
        db.session.commit() 
    except Exception as e:
        db.session.rollback()
        users_failed.append(f"Database commit error for non-students: {str(e)}")
        return {"added": users_added, "failed": users_failed} # Early return to prevent running step 2 on error

    # 2. Process Students, linking to parents created above or existing parents
    for row in rows_to_process:
        role = row.get('role', '').strip().lower()
        if role == 'student':
            try:
                name = row['name'].strip()
                email = row['email'].strip()
                password = row['password'].strip()
                phone_number = row.get('phone_number', '').strip() or None
                
                dob = row.get('dob', '').strip() or None 
                profile_photo_url = row.get('profile_photo_url', '').strip() or None 
                gender = row.get('gender', '').strip() or None
                father_name = row.get('father_name', '').strip() or None
                mother_name = row.get('mother_name', '').strip() or None
                address_line1 = row.get('address_line1', '').strip() or None
                city = row.get('city', '').strip() or None
                state = row.get('state', '').strip() or None
                pincode = row.get('pincode', '').strip() or None
                course_ids_str = row.get('course_ids', '').strip() 

                if not all([name, email, password]):
                    raise ValueError(f"Missing required field(s) for student {name}.")

                if User.query.filter_by(email=email).first():
                    users_failed.append(f"Email already exists: {email}")
                    continue

                parent_id = None
                parent_email = row.get('parent_email', '').strip()

                if parent_email:
                    parent_user = User.query.filter_by(email=parent_email, role='parent').first()
                    if parent_user:
                        parent_id = parent_user.id
                    else:
                        users_failed.append(f"Student {name}: Parent email '{parent_email}' not found or failed creation. Skipping student.")
                        continue 

                hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')

                new_user = User(
                    name=name, email=email, password=hashed_password, role=role,
                    phone_number=phone_number, parent_id=parent_id,
                    can_edit=True,
                    dob=dob, 
                    profile_photo_url=profile_photo_url,
                    gender=gender,
                    father_name=father_name,
                    mother_name=mother_name,
                    address_line1=address_line1,
                    city=city,
                    state=state,
                    pincode=pincode
                )
                db.session.add(new_user)
                
                # Enroll student in courses
                if course_ids_str:
                    course_ids = [int(cid.strip()) for cid in course_ids_str.split(',') if cid.strip().isdigit()]
                    courses = Course.query.filter(Course.id.in_(course_ids)).all()
                    new_user.courses_enrolled.extend(courses)

                users_added.append(email) 

            except Exception as e:
                users_failed.append(f"Error creating student {row.get('name', 'N/A')}: {str(e)}")
                db.session.rollback() 

    try:
        db.session.commit() # Commit all successfully processed students
    except Exception as e:
        db.session.rollback()
        users_failed.append(f"Database commit error for students: {str(e)}")
        
    return {"added": users_added, "failed": users_failed}


# --- Authentication and Session API Endpoints ---
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    user = User.query.filter_by(email=data.get('email')).first()
    if user and bcrypt.check_password_hash(user.password, data.get('password')):
        login_user(user, remember=True)
        return jsonify({"message": "Login successful", "user": user.to_dict()})
    return jsonify({"message": "Invalid email or password"}), 401

@app.route('/api/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify({"message": "Logout successful"})

@app.route('/api/check_session', methods=['GET'])
def check_session():
    if current_user.is_authenticated:
        return jsonify({"logged_in": True, "user": current_user.to_dict()})
    return jsonify({"logged_in": False}), 401

# --- Admin API Endpoints ---
@app.route('/api/users', methods=['GET', 'POST', 'PUT'])
@login_required
def manage_users():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403

    # POST (Create User)
    if request.method == 'POST':
        data = request.form
        if User.query.filter_by(email=data['email']).first():
            return jsonify({"message": "Email address already exists"}), 400

        if not data.get('password'):
            return jsonify({"message": "Password is required for new user"}), 400
            
        hashed_password = bcrypt.generate_password_hash(data['password']).decode('utf-8')
        
        # Handle file upload for profile photo
        profile_photo_url = None
        if 'profile_photo_file' in request.files:
            file = request.files['profile_photo_file']
            if file and file.filename != '' and allowed_file(file.filename, ALLOWED_IMAGE_EXTENSIONS):
                original_filename = secure_filename(file.filename)
                ext = os.path.splitext(original_filename)[1]
                unique_filename = f"{uuid.uuid4()}{ext}"
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
                file.save(file_path)
                profile_photo_url = f"/uploads/{unique_filename}" 

        new_user = User(
            name=data['name'], email=data['email'], password=hashed_password, role=data['role'],
            phone_number=data.get('phone_number'),
            parent_id=int(data.get('parent_id')) if data.get('parent_id') and data.get('parent_id').isdigit() else None,
            can_edit=False if data['role'] == 'admin' else True,
            dob=data.get('dob'),
            profile_photo_url=profile_photo_url, 
            gender=data.get('gender'),
            father_name=data.get('father_name'),
            mother_name=data.get('mother_name'),
            address_line1=data.get('address_line1'),
            city=data.get('city'),
            state=data.get('state'),
            pincode=data.get('pincode')
        )
        db.session.add(new_user)
        
        # FIX: Ensure course assignment works for POST
        if new_user.role == 'student':
            course_id_str = request.form.get('course_ids') 
            if course_id_str and course_id_str.isdigit():
                try:
                    course = db.session.get(Course, int(course_id_str))
                    if course:
                        new_user.courses_enrolled.append(course)
                except ValueError:
                    pass # Ignore if course_id is not a valid integer
                
        try:
            db.session.commit()
            return jsonify(new_user.to_dict()), 201
        except Exception as e:
            db.session.rollback()
            return jsonify({"message": f"Database error creating user: {e}"}), 500


    # PUT (Update User)
    if request.method == 'PUT':
        data = request.form
        user_id = data.get('id')
        try:
            user = db.session.get(User, int(user_id))
        except (ValueError, TypeError):
            return jsonify({"message": "Invalid User ID"}), 400

        if not user:
            return jsonify({"message": "User not found"}), 404

        if 'email' in data and data['email'] != user.email:
            if User.query.filter(User.email == data['email'], User.id != int(user_id)).first():
                return jsonify({"message": "Email address already exists for another user"}), 400

        user.name = data.get('name', user.name)
        user.email = data.get('email', user.email)
        user.phone_number = data.get('phone_number', user.phone_number)
        user.role = data.get('role', user.role)
        
        # --- FIX 1: Safely handle Parent ID and empty strings ---
        parent_id_str = data.get('parent_id')
        user.parent_id = int(parent_id_str) if parent_id_str and parent_id_str.isdigit() else None
        
        # Student-specific fields
        user.dob = data.get('dob', user.dob)
        user.gender = data.get('gender', user.gender)
        user.father_name = data.get('father_name', user.father_name)
        user.mother_name = data.get('mother_name', user.mother_name)
        
        # Ensure address fields are correctly pulled or retain old value if key not present
        user.address_line1 = data.get('address_line1', user.address_line1)
        user.city = data.get('city', user.city)
        user.state = data.get('state', user.state)
        user.pincode = data.get('pincode', user.pincode)


        # FIX: Ensure course assignment works for PUT
        if user.role == 'student':
            course_id_str = request.form.get('course_ids')
            
            # Clear existing enrollments
            user.courses_enrolled = []
            
            if course_id_str and course_id_str.isdigit():
                try:
                    course = db.session.get(Course, int(course_id_str))
                    if course:
                        user.courses_enrolled.append(course)
                except ValueError:
                    pass
            
        new_password = data.get('password')
        if new_password:
            user.password = bcrypt.generate_password_hash(new_password).decode('utf-8')

        try:
            db.session.commit()
            return jsonify(user.to_dict()), 200
        except Exception as e:
            db.session.rollback()
            return jsonify({"message": f"Database error updating user: {e}"}), 500

    # GET: Add search capability
    search_term = request.args.get('search', '').lower()
    query = User.query
    if search_term:
        query = query.filter(
            or_(
                User.name.ilike(f'%{search_term}%'),
                User.email.ilike(f'%{search_term}%'),
                User.phone_number.ilike(f'%{search_term}%')
            )
        )
        
    users = query.all()
    return jsonify([user.to_dict() for user in users])

# NEW: Endpoint to handle profile photo upload separately (used for PUT requests)

@app.route('/api/user/upload_photo/<int:user_id>', methods=['POST'])
@login_required
def upload_profile_photo(user_id):
    if current_user.role != 'admin':
        return jsonify({"message": "Access denied"}), 403

    # FIX: use db.session.get instead of get_or_404
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"message": "User not found"}), 404

    if 'profile_photo_file' not in request.files:
        return jsonify({"message": "No file part in request"}), 400

    file = request.files['profile_photo_file']

    if file.filename == '':
        return jsonify({"message": "No selected file"}), 400

    if file and allowed_file(file.filename, ALLOWED_IMAGE_EXTENSIONS):
        original_filename = secure_filename(file.filename)
        ext = os.path.splitext(original_filename)[1]
        unique_filename = f"{uuid.uuid4()}{ext}"
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)

        try:
            file.save(file_path)

            # Delete old photo if it exists and is a local file
            if user.profile_photo_url and user.profile_photo_url.startswith('/uploads/'):
                old_path = os.path.join(basedir, user.profile_photo_url.lstrip('/'))
                if os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except Exception as e:
                        print(f"Warning: Could not delete old photo {old_path}: {e}")

            # Save the new file path
            user.profile_photo_url = f"/uploads/{unique_filename}"
            db.session.commit()

            return jsonify({"message": "Profile photo uploaded successfully!", "user": user.to_dict()}), 200

        except Exception as e:
            db.session.rollback()
            return jsonify({"message": f"An error occurred: {e}"}), 500
    else:
        return jsonify({"message": "File type not allowed. Use png, jpg, jpeg, or gif."}), 400



@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@login_required
def delete_user(user_id):
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    user = User.query.get_or_404(user_id)

    if user.id == current_user.id:
        return jsonify({'message': 'Cannot delete your own admin account'}), 403

    try:
        # Manually delete dependent records to ensure clean deletion (Cascading)
        if user.role == 'student':
            Payment.query.filter_by(student_id=user_id).delete()
            Grade.query.filter_by(student_id=user_id).delete()
            Attendance.query.filter_by(student_id=user_id).delete()
            Message.query.filter(or_(Message.recipient_id == user_id, Message.sender_id == user_id)).delete()
            user.courses_enrolled = [] # Remove course associations
        elif user.role == 'parent':
            User.query.filter_by(parent_id=user_id).update({"parent_id": None})
            Message.query.filter(or_(Message.recipient_id == user_id, Message.sender_id == user_id)).delete()
        elif user.role == 'teacher':
            Course.query.filter_by(teacher_id=user_id).update({"teacher_id": None})
            Message.query.filter(or_(Message.recipient_id == user_id, Message.sender_id == user_id)).delete()

        db.session.delete(user)
        db.session.commit()
        return jsonify({'message': 'User deleted'})
    except Exception as e:
        db.session.rollback()
        print(f"--- ERROR DELETING USER {user_id}: {e} ---")
        return jsonify({'message': f'Failed to delete user due to internal error. Check logs: {e}'}), 500

@app.route('/api/bulk_upload/users', methods=['POST'])
@login_required
def bulk_upload_users():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403

    if 'file' not in request.files:
        return jsonify({"message": "No file part in the request"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"message": "No selected file"}), 400

    if file:
        file_content = file.read()
        results = process_bulk_users(file_content)

        added_count = len(results.get('added', []))
        failed_count = len(results.get('failed', []))

        if added_count == 0 and failed_count > 0:
             return jsonify({
                "message": f"Bulk upload failed. {failed_count} records had errors. Check CSV format and parent links.",
                "details": results
            }), 400
        elif added_count == 0 and failed_count == 0:
             return jsonify({
                 "message": "Bulk upload processed, but no valid user records were found in the file.",
                 "details": results
             }), 400
        else:
            return jsonify({
                "message": f"Bulk upload finished. {added_count} users added, {failed_count} failed.",
                "details": results
            })


@app.route('/api/admin/bulk_notify', methods=['POST'])
@login_required
def admin_bulk_notify():
    if current_user.role != 'admin':
        return jsonify({"message": "Access denied"}), 403

    data = request.get_json()
    subject = data.get('subject', 'Important Notification')
    body = data.get('body')
    notify_type = data.get('type') # 'sms', 'whatsapp'
    target_role = data.get('target_role') # 'all', 'teachers', 'students', 'parents'

    if not body or notify_type not in ['sms', 'whatsapp']:
        return jsonify({"message": "Missing body or invalid notification type."}), 400

    if target_role == 'all':
        users = User.query.filter(User.id != current_user.id).all()
    elif target_role in ['teachers', 'students', 'parents']:
        users = User.query.filter_by(role=target_role).all()
    else:
        return jsonify({"message": "Invalid target role specified."}), 400

    success_count = 0
    fail_count = 0
    no_phone_count = 0

    for user in users:
        if not user.phone_number:
            no_phone_count += 1
            continue 

        notification_sent = False

        message_content = f"ADMIN NOTICE ({subject}):\n\n{body}"
        new_message = Message(sender_id=current_user.id, recipient_id=user.id, content=message_content)
        db.session.add(new_message)

        if notify_type == 'sms':
            # Use a generic template ID for bulk SMS
            bulk_template_id = os.environ.get('SMS_API_BULK_TEMPLATE_ID', '1707176388002841408')
            if send_actual_sms(user.phone_number, f"{subject}: {body}", template_id=bulk_template_id):
                notification_sent = True
        elif notify_type == 'whatsapp':
            if send_mock_whatsapp(user, subject, body):
                notification_sent = True
        
        # Always send push notification if token is available
        send_push_notification(user.id, subject, body)

        if notification_sent:
            success_count += 1
        else:
            fail_count += 1 

    db.session.commit() 

    total_attempts = len(users)
    final_fail_count = fail_count + no_phone_count

    if success_count > 0:
        return jsonify({
            "message": f"Bulk notification ({notify_type.upper()}) finished. {success_count} sent successfully. {final_fail_count} failed ({no_phone_count} no phone, {fail_count} errors).",
            "sent_count": success_count,
            "failed_count": final_fail_count
        }), 200
    else:
        if total_attempts == 0:
             message = f"Bulk notification ({notify_type.upper()}) not sent: No users found for the target group '{target_role}'."
        elif no_phone_count == total_attempts:
             message = f"Bulk notification ({notify_type.upper()}) failed: {no_phone_count} targeted users do not have a phone number."
        else:
             message = f"Bulk notification ({notify_type.upper()}) failed. {fail_count} errors occurred. {no_phone_count} users skipped (no phone)."

        return jsonify({
            "message": message,
            "sent_count": 0,
            "failed_count": final_fail_count
        }), 400


@app.route('/api/parents', methods=['GET'])
@login_required
def get_parents():
    parents = User.query.filter_by(role='parent').all()
    return jsonify([p.to_dict() for p in parents])

@app.route('/api/teachers', methods=['GET'])
@login_required
def get_teachers():
    return jsonify([teacher.to_dict() for teacher in User.query.filter_by(role='teacher').all()])

@app.route('/api/courses', methods=['GET', 'POST', 'PUT']) 
@login_required
def manage_courses():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    
    if request.method == 'POST':
        data = request.get_json()
        teacher_id = data.get('teacher_id')
        
        # FIX: Ensure teacher_id is None if empty string
        teacher_id_int = int(teacher_id) if teacher_id and str(teacher_id).isdigit() else None
        
        try:
            new_course = Course(
                name=data['name'],
                subjects=data['subjects'],
                teacher_id=teacher_id_int
            )
            db.session.add(new_course)
            db.session.commit()
            return jsonify(new_course.to_dict()), 201
        except Exception as e:
            db.session.rollback()
            print(f"--- ERROR SAVING COURSE (POST): {e} ---")
            return jsonify({"message": f"Failed to save course. Check logs for details: {e}"}), 500
    
    if request.method == 'PUT':
        data = request.get_json()
        course_id = data.get('id')
        try:
            course = db.session.get(Course, int(course_id))
        except (ValueError, TypeError):
            return jsonify({"message": "Invalid Course ID"}), 400
            
        if not course:
            return jsonify({"message": "Course not found"}), 404

        teacher_id = data.get('teacher_id')
        # FIX: Ensure teacher_id is None if empty string
        teacher_id_int = int(teacher_id) if teacher_id and str(teacher_id).isdigit() else None
        
        try:
            course.name = data.get('name', course.name)
            course.subjects = data.get('subjects', course.subjects)
            course.teacher_id = teacher_id_int
            
            db.session.commit()
            return jsonify(course.to_dict()), 200
        except Exception as e:
            db.session.rollback()
            print(f"--- ERROR SAVING COURSE (PUT): {e} ---")
            return jsonify({"message": f"Failed to update course. Check logs for details: {e}"}), 500
        
    return jsonify([c.to_dict() for c in Course.query.all()])

@app.route('/api/courses/<int:course_id>', methods=['DELETE'])
@login_required
def delete_course(course_id):
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    course = db.session.get_or_404(Course, course_id)
    try:
        Grade.query.filter_by(course_id=course_id).delete()
        db.session.delete(course)
        db.session.commit()
        return jsonify({'message': 'Course deleted successfully'})
    except Exception as e:
        db.session.rollback()
        print(f"--- ERROR DELETING COURSE: {e} ---")
        return jsonify({'message': f'Failed to delete course due to internal error. Check logs: {e}'}), 500


@app.route('/api/sessions', methods=['GET', 'POST', 'PUT']) 
@login_required
def manage_sessions():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    
    if request.method == 'POST':
        data = request.get_json()
        try:
            new_session = AcademicSession(
                name=data['name'],
                start_date=data['start_date'],
                end_date=data['end_date'],
                status=data['status']
            )
            db.session.add(new_session)
            db.session.commit()
            return jsonify(new_session.to_dict()), 201
        except Exception as e:
            db.session.rollback()
            print(f"--- ERROR SAVING SESSION (POST): {e} ---")
            return jsonify({"message": f"Failed to save session due to DB error: {e}"}), 500

    if request.method == 'PUT': 
        data = request.get_json()
        session_id = data.get('id')
        try:
            session = db.session.get(AcademicSession, int(session_id))
        except (ValueError, TypeError):
            return jsonify({"message": "Invalid Session ID"}), 400
            
        if not session:
            return jsonify({"message": "Session not found"}), 404
            
        try:
            session.name = data.get('name', session.name)
            session.start_date = data.get('start_date', session.start_date)
            session.end_date = data.get('end_date', session.end_date)
            session.status = data.get('status', session.status)

            db.session.commit()
            return jsonify(session.to_dict()), 200
        except Exception as e:
            db.session.rollback()
            print(f"--- ERROR SAVING SESSION (PUT): {e} ---")
            return jsonify({"message": f"Failed to update session due to DB error: {e}"}), 500


    return jsonify([s.to_dict() for s in AcademicSession.query.all()])

@app.route('/api/sessions/<int:session_id>', methods=['DELETE'])
@login_required
def delete_session(session_id):
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    session = db.session.get_or_404(AcademicSession, session_id)
    linked_fees = FeeStructure.query.filter_by(academic_session_id=session_id).count()
    if linked_fees > 0:
        return jsonify({'message': f'Cannot delete session. It is linked to {linked_fees} fee structure(s).'}), 400

    try:
        db.session.delete(session)
        db.session.commit()
        return jsonify({'message': 'Academic session deleted successfully'})
    except Exception as e:
        db.session.rollback()
        print(f"--- ERROR DELETING SESSION: {e} ---")
        return jsonify({'message': f'Failed to delete session due to internal error. Check logs: {e}'}), 500


@app.route('/api/announcements', methods=['GET', 'POST'])
@login_required
def manage_announcements():
    if request.method == 'POST':
        if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
        data = request.get_json()
        
        new_announcement = Announcement(
            title=data['title'],
            content=data['content'],
            target_group=data['target_group']
        )
        db.session.add(new_announcement)
        db.session.commit()

        # Send Push Notification
        try:
            target = data['target_group']
            recipients = []
            if target == 'all':
                recipients = User.query.filter(User.id != current_user.id).all()
            else:
                role_map = {'teachers': 'teacher', 'students': 'student', 'parents': 'parent', 'admin': 'admin'}
                db_role = role_map.get(target, target) 
                recipients = User.query.filter_by(role=db_role).filter(User.id != current_user.id).all()

            for user in recipients:
                send_push_notification(user.id, f"📢 {data['title']}", data['content'][:100])
        except Exception as e:
            print(f"Push Error (Announcements): {e}")

        return jsonify(new_announcement.to_dict()), 201
    
    # GET request logic...
    return jsonify([a.to_dict() for a in Announcement.query.order_by(Announcement.created_at.desc()).all()])

@app.route('/api/announcements/<int:announcement_id>', methods=['DELETE'])
@login_required
def delete_announcement(announcement_id):
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    announcement = db.session.get_or_404(Announcement, announcement_id)
    db.session.delete(announcement)
    db.session.commit()
    return jsonify({'message': 'Announcement deleted successfully'})

@app.route('/api/fee_structures', methods=['GET', 'POST', 'PUT']) 
@login_required
def manage_fee_structures():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    
    if request.method == 'POST':
        data = request.get_json()
        try:
            # FIX: Ensure all required fields exist before date parsing
            if not all([data.get('due_date'), data.get('academic_session_id'), data.get('total_amount'), data.get('name')]):
                return jsonify({"message": "Missing required fee structure fields."}), 400

            due_date_obj = datetime.strptime(data['due_date'], '%Y-%m-%d').date()
            
            # Handle Course ID (Optional but recommended for specific fees)
            c_id = data.get('course_id')
            course_id_int = int(c_id) if c_id and str(c_id).isdigit() else None

            new_structure = FeeStructure(
                name=data['name'],
                academic_session_id=data['academic_session_id'],
                course_id=course_id_int,  # Save course ID here
                total_amount=data['total_amount'],
                due_date=due_date_obj
            )
            db.session.add(new_structure)
            db.session.commit()
            return jsonify(new_structure.to_dict()), 201
        except Exception as e:
            db.session.rollback()
            return jsonify({"message": f"Database error creating fee structure: {e}"}), 500

    if request.method == 'PUT':
        data = request.get_json()
        fee_id = data.get('id')
        try:
            structure = db.session.get(FeeStructure, int(fee_id))
        except (ValueError, TypeError):
             return jsonify({"message": "Invalid Fee Structure ID"}), 400
             
        if not structure:
            return jsonify({"message": "Fee structure not found"}), 404
            
        try:
            if data.get('due_date'):
                due_date_obj = datetime.strptime(data['due_date'], '%Y-%m-%d').date()
            else:
                 due_date_obj = structure.due_date 
        except ValueError:
            return jsonify({"message": "Invalid date format. Use YYYY-MM-DD."}), 400

        structure.name = data.get('name', structure.name)
        structure.academic_session_id = data.get('academic_session_id', structure.academic_session_id)
        
        # Update course_id if provided (allows changing assignment)
        c_id = data.get('course_id')
        structure.course_id = int(c_id) if c_id and str(c_id).isdigit() else None
        
        structure.total_amount = data.get('total_amount', structure.total_amount)
        structure.due_date = due_date_obj

        try:
            db.session.commit()
            return jsonify(structure.to_dict()), 200
        except Exception as e:
            db.session.rollback()
            print(f"--- ERROR SAVING COURSE (PUT): {e} ---")
            return jsonify({"message": f"Failed to update fee structure. Check logs: {e}"}), 500
        
    return jsonify([s.to_dict() for s in FeeStructure.query.order_by(FeeStructure.id.desc()).all()])

@app.route('/api/payments', methods=['POST'])
@login_required
def record_payment():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    data = request.get_json()

    if not data.get('student_id') or not data.get('fee_structure_id') or data.get('amount_paid') is None or not data.get('payment_method'):
         return jsonify({"message": "Missing required payment fields."}), 400

    new_payment = Payment(
        student_id=data['student_id'],
        fee_structure_id=data['fee_structure_id'],
        amount_paid=data['amount_paid'],
        payment_method=data['payment_method']
    )
    db.session.add(new_payment)
    
    try:
        db.session.commit() # Commit the new payment first

        receipt_message = "Receipt link available." 

        # Send Fee Alert (handles SMS and Push for both student and parent)
        student = db.session.get(User, data['student_id'])
        if student:
            status = calculate_fee_status(student.id)
            if status['balance'] > 0: 
                # Send push/sms reminders if balance is still > 0
                send_fee_alert_notifications(student.id) 

        return jsonify({"message": f"Payment recorded. {receipt_message}", "payment_id": new_payment.id}), 201

    except Exception as e:
        db.session.rollback()
        # FIX 2: Return a guaranteed JSON response on failure
        return jsonify({"message": f"Internal Error Recording Payment: {e}"}), 500


@app.route('/api/fee_status', methods=['GET'])
@login_required
def get_fee_status():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    students = User.query.filter_by(role='student').all()
    status_list = []

    for student in students:
        status = calculate_fee_status(student.id)
        latest_payment = Payment.query.filter_by(student_id=student.id).order_by(Payment.payment_date.desc()).first()

        status_list.append({
            "student_id": student.id,
            "student_name": student.name,
            "balance": status['balance'],
            "due_date": status['due_date'],
            "pending_days": status['pending_days'],
            "latest_payment_id": latest_payment.id if latest_payment else None
        })
    return jsonify(status_list)

@app.route('/api/send_sms_alert', methods=['POST'])
@login_required
def send_fee_alert():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    data = request.get_json()
    student_id = data.get('student_id')

    student = db.session.get(User, student_id)
    if not student:
        return jsonify({"message": "Student not found"}), 404

    status = calculate_fee_status(student_id)
    if status['balance'] <= 0:
        return jsonify({"message": f"{student.name} has no pending fee."}), 200

    # Send SMS
    student_alerted = send_fee_alert_sms(student, status['balance'], status['due_date'])

    parent_alerted = False
    parent = db.session.get(User, student.parent_id) if student and student.parent_id else None
    if parent:
        parent_alerted = send_fee_alert_sms(parent, status['balance'], status['due_date'])

    # Send Push Notification
    push_title = "Fee Reminder"
    push_body = f"Fee of Rs {status['balance']:.2f} is pending. Due: {status['due_date']}."
    
    send_push_notification(student.id, push_title, push_body)
    if parent:
        send_push_notification(parent.id, f"Child Alert: {push_title}", push_body)

    message = f"Alert sent to Student ({'Yes' if student_alerted else 'No SMS Phone'})."
    if parent:
        message += f" Alert sent to Parent ({'Yes' if parent_alerted else 'No SMS Phone'})."

    return jsonify({"message": message}), 200


# --- Report Endpoints ---
@app.route('/api/reports/admissions', methods=['GET'])
@login_required
def admissions_report():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    admissions = User.query.filter(User.role == 'student', User.created_at >= thirty_days_ago).all()
    return jsonify([a.to_dict() for a in admissions])

@app.route('/api/reports/attendance', methods=['GET'])
@login_required
def attendance_report():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    students = User.query.filter_by(role='student').all()
    report = []
    for student in students:
        total_classes = Attendance.query.filter_by(student_id=student.id).count()
        present_records = Attendance.query.filter_by(student_id=student.id).filter(
            Attendance.status.in_(['Present', 'Checked-In'])
        ).count()
        absent_records = total_classes - present_records

        percentage = round((present_records / total_classes) * 100) if total_classes > 0 else 0

        report.append({
            "student_id": student.id, 
            "student_name": student.name,
            "total_classes": total_classes,
            "present": present_records,
            "absent": absent_records,
            "percentage": percentage
        })
    return jsonify(report)

@app.route('/api/reports/performance', methods=['GET'])
@login_required
def performance_report():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    students = User.query.filter_by(role='student').all()
    report = []
    for student in students:
        grades = Grade.query.filter_by(student_id=student.id).all()

        total_obtained = sum(g.marks_obtained for g in grades)
        total_possible = sum(g.total_marks for g in grades)

        overall_percentage = round((total_obtained / total_possible) * 100) if total_possible > 0 else 0

        report.append({
            "student_name": student.name,
            "assessments_taken": len(grades),
            "total_score": round(total_obtained, 2),
            "overall_percentage": overall_percentage
        })
    return jsonify(report)

@app.route('/api/reports/fee_pending', methods=['GET'])
@login_required
def fee_pending_report():
    if current_user.role != 'admin':
        return jsonify({"message": "Access denied"}), 403

    students = User.query.filter_by(role='student').all()
    pending_list = []

    for student in students:
        status = calculate_fee_status(student.id)
        if status['balance'] > 0:
            latest_payment = Payment.query.filter_by(student_id=student.id).order_by(Payment.payment_date.desc()).first()
            pending_list.append({
                "student_id": student.id,
                "student_name": student.name,
                "phone_number": student.phone_number, 
                "balance": status['balance'],
                "due_date": status['due_date'],
                "pending_days": status['pending_days'],
                "latest_payment_id": latest_payment.id if latest_payment else None
            })

    return jsonify(pending_list)


# --- Student API Endpoints (FIXED WITH TRY/EXCEPT) ---
@app.route('/api/student/fees', methods=['GET'])
@login_required
def get_student_fees():
    if current_user.role != 'student': return jsonify({"message": "Access denied"}), 403
    
    try: # FIX: Added Try Block for Robust Error Handling
        status = calculate_fee_status(current_user.id)
        payments = Payment.query.filter_by(student_id=current_user.id).order_by(Payment.payment_date.desc()).all()

        history = [{
            "date": p.payment_date.strftime('%Y-%m-%d'),
            "amount": p.amount_paid,
            "method": p.payment_method
        } for p in payments]

        return jsonify({
            **status,
            "history": history
        })
    except Exception as e: # Catch all errors and return 500 JSON response
        print(f"ERROR fetching fees for student {current_user.id}: {e}")
        return jsonify({"message": f"Internal Server Error: Failed to load fee data. Error: {e}"}), 500


@app.route('/api/student/attendance', methods=['GET'])
@login_required
def get_student_attendance():
    if current_user.role != 'student': return jsonify({"message": "Access denied"}), 403
    try: # FIX: Added Try Block for Robust Error Handling
        attendance_records = Attendance.query.filter_by(student_id=current_user.id).order_by(Attendance.check_in_time.desc()).limit(10).all()
        return jsonify([{
            "date": r.check_in_time.strftime('%Y-%m-%d'),
            "time": r.check_in_time.strftime('%I:%M %p'),
            "status": r.status
        } for r in attendance_records])
    except Exception as e: # Catch all errors and return 500 JSON response
        print(f"ERROR fetching attendance for student {current_user.id}: {e}")
        return jsonify({"message": f"Internal Server Error: Failed to load attendance data. Error: {e}"}), 500

@app.route('/api/student/grades', methods=['GET'])
@login_required
def get_student_grades():
    if current_user.role != 'student': return jsonify({"message": "Access denied"}), 403
    try: # FIX: Added Try Block for Robust Error Handling
        grades = Grade.query.filter_by(student_id=current_user.id).all()
        grade_data = []
        for grade in grades:
            course = db.session.get(Course, grade.course_id)
            grade_data.append({
                "course_name": course.name if course else "N/A",
                "assessment_name": grade.assessment_name,
                "marks_obtained": grade.marks_obtained,
                "total_marks": grade.total_marks
            })
        return jsonify(grade_data)
    except Exception as e: # Catch all errors and return 500 JSON response
        print(f"ERROR fetching grades for student {current_user.id}: {e}")
        return jsonify({"message": f"Internal Server Error: Failed to load grades data. Error: {e}"}), 500

@app.route('/api/student/notes', methods=['GET'])
@login_required
def get_student_notes():
    if current_user.role != 'student':
        return jsonify({"message": "Access denied"}), 403
    
    try: # FIX: Added Try Block for Robust Error Handling
        # Students now see notes for courses they are enrolled in
        student_course_ids = [c.id for c in current_user.courses_enrolled]
        notes = SharedNote.query.filter(SharedNote.course_id.in_(student_course_ids)).order_by(SharedNote.created_at.desc()).all()
        
        return jsonify([note.to_dict() for note in notes])
    except Exception as e: # Catch all errors and return 500 JSON response
        print(f"ERROR fetching notes for student {current_user.id}: {e}")
        return jsonify({"message": f"Internal Server Error: Failed to load notes data. Error: {e}"}), 500


# --- Teacher API Endpoints ---
@app.route('/api/teacher/courses', methods=['GET'])
@login_required
def get_teacher_courses():
    if current_user.role != 'teacher': return jsonify({"message": "Access denied"}), 403
    courses = Course.query.filter_by(teacher_id=current_user.id).all()
    return jsonify([c.to_dict() for c in courses])

@app.route('/api/teacher/students', methods=['GET'])
@login_required
def get_teacher_students():
    if current_user.role != 'teacher': return jsonify({"message": "Access denied"}), 403
    
    # 1. Get the IDs of courses taught by the current teacher
    teacher_course_ids = [c.id for c in Course.query.filter_by(teacher_id=current_user.id).all()]

    if not teacher_course_ids:
        return jsonify([])

    # 2. Query the association table to find students enrolled in those courses
    enrolled_students = User.query.join(student_course_association).join(Course).filter(
        User.role == 'student',
        Course.id.in_(teacher_course_ids)
    ).distinct().all()

    return jsonify([s.to_dict() for s in enrolled_students])

@app.route('/api/teacher/attendance', methods=['POST'])
@login_required
def record_attendance():
    if current_user.role != 'teacher': 
        return jsonify({"message": "Access denied"}), 403
        
    data = request.get_json()
    attendance_date_str = data['date']
    attendance_data = data['attendance_data']
    
    try:
        attendance_date = datetime.strptime(attendance_date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({"message": "Invalid date format."}), 400
        
    # DLT Template ID for Attendance Absent
    attendance_template_id = os.environ.get('SMS_API_ATTENDANCE_TEMPLATE_ID', "1707176388022694296")
    sms_count = 0

    for record in attendance_data:
        existing_record = Attendance.query.filter_by(student_id=record['student_id']).filter(
            db.func.date(Attendance.check_in_time) == attendance_date
        ).first()

        if existing_record:
            existing_record.status = record['status']
        else:
            new_record = Attendance(
                student_id=record['student_id'],
                check_in_time=datetime.combine(attendance_date, datetime.min.time()),
                status=record['status']
            )
            db.session.add(new_record)
        
        # --- NOTIFICATION LOGIC ---
        student = db.session.get(User, record['student_id'])
        parent = db.session.get(User, student.parent_id) if student and student.parent_id else None
        
        if record['status'] == 'Absent':
            # SMS Logic
            if student and student.phone_number and attendance_template_id:
                try:
                    d_obj = datetime.strptime(attendance_date_str, '%Y-%m-%d')
                    fmt_date = d_obj.strftime('%d-%b-%Y')
                except: fmt_date = attendance_date_str
                
                msg = f"Dear {student.name}, your attendance is marked Absent for date {fmt_date}. Please contact CST Institute."
                if send_actual_sms(student.phone_number, msg, template_id=attendance_template_id): sms_count += 1
                if parent and parent.phone_number: send_actual_sms(parent.phone_number, msg, template_id=attendance_template_id)

            # Push Logic
            if student: send_push_notification(student.id, "Attendance Alert", f"Marked ABSENT for {attendance_date_str}.")
            if parent: send_push_notification(parent.id, "Child Attendance", f"{student.name} marked ABSENT for {attendance_date_str}.")

        elif record['status'] == 'Present':
            # Push Logic Only (No SMS for Present)
            if student: send_push_notification(student.id, "Attendance", f"Marked PRESENT for {attendance_date_str}.")
            if parent: send_push_notification(parent.id, "Child Attendance", f"{student.name} marked PRESENT for {attendance_date_str}.")

    db.session.commit()
    return jsonify({"message": f"Attendance recorded. Sent {sms_count} SMS alerts."}), 201

@app.route('/api/teacher/notify', methods=['POST'])
@login_required
def send_notification_to_student():
    if current_user.role not in ['teacher', 'admin']: return jsonify({"message": "Access denied"}), 403
    
    data = request.get_json()
    student_id = data.get('student_id')
    subject = data.get('subject')
    body = data.get('body')
    notify_type = data.get('type') # 'email', 'sms', 'whatsapp', 'push'

    student = db.session.get(User, student_id)
    if not student or student.role != 'student':
        return jsonify({"message": "Student not found."}), 404

    parent = db.session.get(User, student.parent_id) if student.parent_id else None

    result_messages = []

    # 1. Store as a Message (Database Record - Always happens)
    message_content = f"Subject: {subject}\n\n{body}"
    new_message_student = Message(sender_id=current_user.id, recipient_id=student_id, content=message_content)
    db.session.add(new_message_student)
    result_messages.append("Message logged in student's portal.")

    if parent:
        parent_message_content = f"MESSAGE ABOUT CHILD {student.name}:\n\n{message_content}"
        new_message_parent = Message(sender_id=current_user.id, recipient_id=parent.id, content=parent_message_content)
        db.session.add(new_message_parent)
        result_messages.append("Message logged in parent's portal.")

    db.session.commit()

    # 2. External Notification Logic
    
    if notify_type == 'email':
        try:
            msg = MailMessage(subject=subject,
                              sender=app.config['MAIL_USERNAME'],
                              recipients=[student.email])
            if parent and parent.email:
                msg.recipients.append(parent.email)
            msg.body = body
            # mail.send(msg) 
            print(f"--- [MOCK EMAIL SENT] to {student.email} ---")
            result_messages.append("Email sent successfully (mocked).")
        except Exception as e:
            print(f"Flask-Mail Error: {e}")
            result_messages.append(f"Email failed: {str(e)}.")

    elif notify_type == 'sms':
        if send_actual_sms(student.phone_number, f"{subject}: {body}"):
             result_messages.append(f"SMS sent to Student ({student.phone_number}).")
        else:
             result_messages.append(f"SMS to Student failed.")
        
        if parent:
            if send_actual_sms(parent.phone_number, f"Re: {student.name} - {subject}: {body}"):
                 result_messages.append(f"SMS sent to Parent ({parent.phone_number}).")
            else:
                 result_messages.append(f"SMS to Parent failed.")

    elif notify_type == 'whatsapp':
        if send_mock_whatsapp(student, subject, body):
            result_messages.append(f"WhatsApp sent to Student ({student.phone_number}).")
        else:
            result_messages.append(f"WhatsApp to Student failed.")
        
        if parent: 
            if send_mock_whatsapp(parent, subject, body):
                result_messages.append(f"WhatsApp sent to Parent ({parent.phone_number}).")
            else:
                result_messages.append(f"WhatsApp to Parent failed.")

    elif notify_type == 'portal': # Replaced 'push' with 'portal' for generic notifications
         if send_push_notification(student.id, subject, body):
            result_messages.append("Push Notification sent to Student.")
         else:
            result_messages.append("Push to Student failed (App not installed/No Token).")
        
         if parent:
            if send_push_notification(parent.id, f"Child Alert: {subject}", body):
                result_messages.append("Push Notification sent to Parent.")

    else:
        result_messages.append("No valid external notification type selected.")

    return jsonify({"message": " | ".join(result_messages)}), 200


@app.route('/api/teacher/reports/attendance', methods=['GET'])
@login_required
def teacher_attendance_report():
    if current_user.role != 'teacher':
        return jsonify({"message": "Access denied"}), 403

    # 1. Get IDs of courses taught by this teacher
    teacher_course_ids = [
        c.id for c in Course.query.filter_by(teacher_id=current_user.id).all()
    ]

    if not teacher_course_ids:
        # No courses assigned to this teacher
        return jsonify([])

    # 2. Get distinct students enrolled in those courses
    students = (
        User.query
        .join(student_course_association)
        .join(Course)
        .filter(
            User.role == 'student',
            Course.id.in_(teacher_course_ids)
        )
        .distinct()
        .all()
    )

    # 3. Build attendance summary per student
    report = []
    for student in students:
        total_classes = Attendance.query.filter_by(student_id=student.id).count()
        present_records = (
            Attendance.query
            .filter_by(student_id=student.id)
            .filter(Attendance.status.in_(['Present', 'Checked-In']))
            .count()
        )
        absent_records = total_classes - present_records

        report.append({
            "student_id": student.id,
            "student_name": student.name,
            "phone_number": student.phone_number,
            "total_classes": total_classes,
            "present": present_records,
            "absent": absent_records
        })

    return jsonify(report)

@app.route('/api/teacher/notes', methods=['GET', 'DELETE'])
@login_required
def manage_teacher_notes():
    if current_user.role != 'teacher':
        return jsonify({"message": "Access denied"}), 403

    if request.method == 'GET':
        notes = SharedNote.query.filter_by(teacher_id=current_user.id).order_by(SharedNote.created_at.desc()).all()
        return jsonify([note.to_dict() for note in notes])
    
    if request.method == 'DELETE':
        note_id = request.args.get('id')
        if not note_id:
            return jsonify({"message": "Note ID is required"}), 400
        
        note = db.session.get(SharedNote, note_id)
        
        if not note:
            return jsonify({"message": "Note not found"}), 404
            
        if note.teacher_id != current_user.id:
            return jsonify({"message": "You are not authorized to delete this note"}), 403
            
        try:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], note.filename)
            if os.path.exists(file_path):
                os.remove(file_path)
            
            db.session.delete(note)
            db.session.commit()
            return jsonify({"message": "Note deleted successfully"})
        except Exception as e:
            db.session.rollback()
            return jsonify({"message": f"Error deleting note: {e}"}), 500

@app.route('/api/teacher/upload_note', methods=['POST'])
@login_required
def upload_note():
    if current_user.role != 'teacher':
        return jsonify({"message": "Access denied"}), 403
        
    if 'file' not in request.files:
        return jsonify({"message": "No file part in request"}), 400
        
    file = request.files['file']
    title = request.form.get('title')
    course_id = request.form.get('course_id')
    description = request.form.get('description', '')

    if file.filename == '':
        return jsonify({"message": "No selected file"}), 400
        
    if not title or not course_id:
        return jsonify({"message": "Title and Course are required"}), 400

    if file and allowed_file(file.filename, ALLOWED_NOTE_EXTENSIONS):
        original_filename = secure_filename(file.filename)
        ext = os.path.splitext(original_filename)[1]
        unique_filename = f"{uuid.uuid4()}{ext}"
        
        try:
            # Save file to uploads folder
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
            
            # Create DB record
            new_note = SharedNote(
                filename=unique_filename,
                original_filename=original_filename,
                title=title,
                description=description,
                course_id=int(course_id),
                teacher_id=current_user.id
            )
            db.session.add(new_note)
            db.session.commit()

            # Notify Students about New Note via PUSH
            try:
                course = db.session.get(Course, int(course_id))
                if course and course.students:
                    for student in course.students:
                        send_push_notification(
                            student.id,
                            "New Study Material",
                            f"New note added in {course.name}: {title}"
                        )
            except Exception as e:
                print(f"--- Notes Push Error: {e} ---")

            return jsonify({"message": "File uploaded.", "note": new_note.to_dict()}), 201
            
        except Exception as e:
            db.session.rollback()
            return jsonify({"message": f"An error occurred: {e}"}), 500
    else:
        return jsonify({"message": "File type not allowed"}), 400


@app.route('/uploads/<filename>')
@login_required
def serve_uploaded_file(filename):
    if current_user.role not in ['student', 'parent', 'teacher', 'admin']:
        return "Access denied", 403

    # SECURITY: block path traversal (../etc), but allow normal filenames with dots
    if '..' in filename or filename.startswith('/'):
        return "Invalid filename", 400

    try:
        # Ensure file is referenced either as a SharedNote or as a profile photo
        note = SharedNote.query.filter_by(filename=filename).first()
        user = User.query.filter_by(profile_photo_url=f"/uploads/{filename}").first()
        
        if not note and not user:
            return "File not found or unauthorized", 404

        download_name = note.original_filename if note else filename

        return send_from_directory(
            app.config['UPLOAD_FOLDER'],
            filename,
            as_attachment=True if note else False,  # Download notes, inline for photos
            download_name=download_name if note else None
        )
    except FileNotFoundError:
        return "File not found.", 404

# --- Parent API Endpoints (FIXED WITH TRY/EXCEPT) ---
@app.route('/api/parent/children', methods=['GET'])
@login_required
def get_parent_children():
    if current_user.role != 'parent': return jsonify({"message": "Access denied"}), 403
    try: # FIX: Added Try Block for Robust Error Handling
        # FIX: Ensure children filtering is correct
        children = User.query.filter(User.parent_id == current_user.id).all() 
        return jsonify([c.to_dict() for c in children])
    except Exception as e:
        print(f"ERROR fetching children for parent {current_user.id}: {e}")
        return jsonify({"message": f"Internal Server Error: Failed to load children data. Error: {e}"}), 500

@app.route('/api/parent/messages', methods=['GET'])
@login_required
def get_parent_messages():
    if current_user.role != 'parent': return jsonify({"message": "Access denied"}), 403
    try: # FIX: Added Try Block for Robust Error Handling
        messages_to_parent = Message.query.filter_by(recipient_id=current_user.id).order_by(Message.sent_at.desc()).all()

        child_ids = [c.id for c in User.query.filter_by(parent_id=current_user.id).all()]
        messages_to_children = Message.query.filter(Message.recipient_id.in_(child_ids)).order_by(Message.sent_at.desc()).all()

        all_messages = sorted(messages_to_parent + messages_to_children, key=lambda m: m.sent_at, reverse=True)

        unique_messages = []
        seen_ids = set()
        for msg in all_messages:
            if msg.id not in seen_ids:
                unique_messages.append(msg)
                seen_ids.add(msg.id)

        return jsonify([m.to_dict() for m in unique_messages])
    except Exception as e:
        print(f"ERROR fetching messages for parent {current_user.id}: {e}")
        return jsonify({"message": f"Internal Server Error: Failed to load messages. Error: {e}"}), 500


@app.route('/api/parent/child_data/<int:student_id>', methods=['GET'])
@login_required
def get_child_data(student_id):
    if current_user.role not in ['parent', 'admin']:
        return jsonify({"message": "Access denied"}), 403

    try:
        # FIX: use db.session.get instead of get_or_404
        student = db.session.get(User, student_id)
        if not student:
            return jsonify({"message": "Student not found"}), 404

        # Authorization: must be an actual child of this parent (unless admin)
        if student.role != 'student' or (
            current_user.role == 'parent' and student.parent_id != current_user.id
        ):
            return jsonify({"message": "Authorization error or not a valid child."}), 403

        # Recent attendance
        attendance_records = (
            Attendance.query
            .filter_by(student_id=student.id)
            .order_by(Attendance.check_in_time.desc())
            .limit(10)
            .all()
        )
        attendance_data = [{
            "date": r.check_in_time.strftime('%Y-%m-%d'),
            "time": r.check_in_time.strftime('%I:%M %p'),
            "status": r.status
        } for r in attendance_records]

        # Grades
        grades = Grade.query.filter_by(student_id=student.id).all()
        grade_data = []
        for grade in grades:
            course = db.session.get(Course, grade.course_id)
            grade_data.append({
                "course_name": course.name if course else "N/A",
                "assessment_name": grade.assessment_name,
                "marks_obtained": grade.marks_obtained,
                "total_marks": grade.total_marks
            })

        # Fees
        fee_status = calculate_fee_status(student.id)

        return jsonify({
            "profile": student.to_dict(),
            "attendance": attendance_data,
            "grades": grade_data,
            "fees": fee_status
        })
    except Exception as e:
        print(f"ERROR fetching child data for parent {current_user.id} and student {student_id}: {e}")
        return jsonify({"message": f"Internal Server Error: Failed to load child data. Error: {e}"}), 500



# --- Receipt Route ---
@app.route('/api/receipt/<int:payment_id>', methods=['GET'])
@login_required
def serve_receipt(payment_id):
    try:
        # FIX: use db.session.get instead of get_or_404
        payment = db.session.get(Payment, payment_id)
        if not payment:
            return "Payment not found.", 404

        student = db.session.get(User, payment.student_id)
        fee_structure = db.session.get(FeeStructure, payment.fee_structure_id)

        if not student:
            return "Student record not found for this payment.", 404

        # Authorization Check (Admin, Student paying, or Parent of Student)
        is_authorized = (
            current_user.role == 'admin' or
            current_user.id == student.id or
            current_user.id == student.parent_id
        )

        if not is_authorized:
            return "Access Denied", 403

        # --- Safety Checks for Receipt Variables ---
        student_name = student.name if student and student.name else "N/A"
        fee_name = fee_structure.name if fee_structure and fee_structure.name else "Fee Payment"
        payment_amount = f"₹ {payment.amount_paid:.2f}"
        payment_date_fmt = payment.payment_date.strftime('%d-%b-%Y %I:%M %p')
        payment_method = payment.payment_method if payment.payment_method else "N/A"

        if fee_structure and fee_structure.due_date:
            fee_due_date_fmt = fee_structure.due_date.strftime('%d-%b-%Y')
        else:
            fee_due_date_fmt = "N/A"

        # Mock Institute Details
        institute_address = "CST Institute Address, Plot No 43 Om Park, Jalgaon"
        institute_city_state = "Jalgaon, Maharashtra"
        institute_contact = "9822826307"
        institute_email = "admin@cstai.in"

        html_content = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Receipt #{payment.id}</title>
            <style>
                body {{
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                    margin: 0;
                    padding: 0;
                    background-color: #f4f4f4;
                }}
                .container {{
                    width: 80%;
                    max-width: 800px;
                    margin: 20px auto;
                    background-color: #fff;
                    padding: 30px;
                    border: 1px solid #ddd;
                    box-shadow: 0 0 10px rgba(0,0,0,0.1);
                }}
                h1, h2, h3, h4 {{
                    margin: 0;
                    padding: 0;
                }}
                .header {{
                    text-align: center;
                    margin-bottom: 20px;
                }}
                .header h1 {{
                    font-size: 24px;
                    font-weight: 700;
                }}
                .header p {{
                    margin: 4px 0;
                    color: #555;
                }}
                .section-title {{
                    font-size: 18px;
                    font-weight: 600;
                    margin-top: 20px;
                    margin-bottom: 10px;
                    border-bottom: 1px solid #eee;
                    padding-bottom: 5px;
                }}
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin-top: 10px;
                }}
                table td {{
                    padding: 6px 4px;
                    vertical-align: top;
                    font-size: 14px;
                }}
                .label {{
                    font-weight: 600;
                    color: #333;
                    width: 30%;
                }}
                .value {{
                    color: #555;
                }}
                .footer {{
                    text-align: center;
                    margin-top: 30px;
                    font-size: 12px;
                    color: #777;
                }}
                .amount-box {{
                    margin-top: 15px;
                    padding: 10px;
                    border: 1px dashed #999;
                    text-align: right;
                    font-size: 16px;
                    font-weight: 600;
                }}
                @media print {{
                    body {{
                        background-color: #fff;
                    }}
                    .container {{
                        box-shadow: none;
                        border: none;
                    }}
                    .no-print {{
                        display: none;
                    }}
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>CST Institute</h1>
                    <p>{institute_address}</p>
                    <p>{institute_city_state}</p>
                    <p>Contact: {institute_contact} | Email: {institute_email}</p>
                </div>

                <h2 class="section-title">Receipt Details</h2>
                <table>
                    <tr><td class="label">Receipt No.</td><td class="value">#{payment.id}</td></tr>
                    <tr><td class="label">Payment Date</td><td class="value">{payment_date_fmt}</td></tr>
                    <tr><td class="label">Payment Method</td><td class="value">{payment_method}</td></tr>
                </table>

                <h2 class="section-title">Student Details</h2>
                <table>
                    <tr><td class="label">Student Name</td><td class="value">{student_name}</td></tr>
                    <tr><td class="label">Student ID</td><td class="value">{student.id}</td></tr>
                </table>

                <h2 class="section-title">Fee Details</h2>
                <table>
                    <tr><td class="label">Fee Name</td><td class="value">{fee_name}</td></tr>
                    <tr><td class="label">Due Date</td><td class="value">{fee_due_date_fmt}</td></tr>
                </table>

                <div class="amount-box">
                    Amount Paid: {payment_amount}
                </div>

                <div class="footer no-print">
                    <p>This is a system-generated receipt.</p>
                    <button onclick="window.print()">Print Receipt</button>
                </div>
            </div>
        </body>
        </html>
        """

        return html_content
    except Exception as e:
        print(f"ERROR generating receipt for payment {payment_id}: {e}")
        return "Internal Server Error while generating receipt.", 500



# --- Routes to Serve Frontend Pages ---
@app.route('/')
def serve_login_page():
    if current_user.is_authenticated:
        if current_user.role == 'admin': return redirect(url_for('serve_admin_page'))
        if current_user.role == 'teacher': return redirect(url_for('serve_teacher_page'))
        if current_user.role == 'student': return redirect(url_for('serve_student_page'))
        if current_user.role == 'parent': return redirect(url_for('serve_parent_page'))
    return render_template('login.html')

@app.route('/admin')
@login_required
def serve_admin_page():
    if current_user.role == 'admin': return render_template('admin.html')
    else: return redirect(url_for('serve_login_page'))

@app.route('/quick-admin')
def quick_admin_login():
    if app.debug:
        admin_user = User.query.filter_by(role='admin').first()
        if admin_user:
            login_user(admin_user, remember=True)
            return redirect(url_for('serve_admin_page'))
        return "No admin user found.", 404
    return "Access Denied.", 403

@app.route('/teacher')
@login_required
def serve_teacher_page():
    if current_user.role == 'teacher': return render_template('teacher.html')
    else: return redirect(url_for('serve_login_page'))

@app.route('/student')
@login_required
def serve_student_page():
    if current_user.role == 'student': return render_template('student.html')
    else: return redirect(url_for('serve_login_page'))

@app.route('/parent')
@login_required
def serve_parent_page():
    if current_user.role == 'parent':
        return render_template('parent.html')
    else:
        return redirect(url_for('serve_login_page'))
        
@app.route('/api/save_fcm_token', methods=['POST'])
@login_required
def save_fcm_token():
    token = request.json.get('token')
    if token:
        current_user.fcm_token = token
        db.session.commit()
        return jsonify({"message": "Token saved"}), 200
    return jsonify({"message": "No token provided"}), 400

@app.route('/firebase-messaging-sw.js')
def service_worker():
    return send_from_directory(app.static_folder, 'firebase-messaging-sw.js')

# Helper function to send notifications
def send_fee_alert_notifications(student_id):
    student = db.session.get(User, student_id)
    if not student: return False
    
    status = calculate_fee_status(student_id)
    parent = db.session.get(User, student.parent_id) if student.parent_id else None
    
    # Send SMS (using existing logic from send_fee_alert_sms)
    student_alerted = send_fee_alert_sms(student, status['balance'], status['due_date'])
    if parent:
        parent_alerted = send_fee_alert_sms(parent, status['balance'], status['due_date'])

    # Send Push Notification
    push_title = "Fee Reminder"
    push_body = f"Fee of Rs {status['balance']:.2f} pending. Due: {status['due_date']}."
    
    send_push_notification(student.id, push_title, push_body)
    if parent:
        send_push_notification(parent.id, f"Child Alert: {push_title}", push_body)
    
    return student_alerted or (parent_alerted if parent else False)

# Helper function to use later
def send_push_notification(user_id, title, body):
    # 1. LAZY INITIALIZATION: Only initialize Firebase if it hasn't been already
    if not init_firebase():
        return False

    # 2. SEND PUSH
    with app.app_context(): # Ensure we are in app context to access DB
        user = db.session.get(User, user_id)
        
        if not user:
            logger.warning(f"Push Failed: User ID {user_id} not found.")
            return False
            
        if not user.fcm_token:
            # logger.warning(f"Push Failed: User {user.name} (ID: {user.id}) has NO FCM Token. They must log in to the App once.")
            return False

        try:
            message = messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                token=user.fcm_token,
            )
            response = messaging.send(message)
            logger.info(f"Push Sent to {user.name}: {response}")
            return True
        except Exception as e:
            logger.error(f"Push Error for {user.name}: {e}")
            # If token is invalid (stale), maybe clear it?
            if 'registration-token-not-registered' in str(e):
                logger.info(f"Token invalid for {user.name}, clearing it.")
                user.fcm_token = None
                db.session.commit()
            return False
    return False

# --- NEW HEALTH CHECK ENDPOINT (Needed for Coolify) ---
@app.route('/healthz', methods=['GET'])
def health_check():
    """A simple, unprotected endpoint for external health monitoring."""
    return "OK", 200

# --- Run Application ---
# NEW: Define a dedicated function for database setup
def initialize_database():
    # FIX: Must import all necessary items inside the function scope for Python 3.x
    # in environments where the script might be executed via command line.
    from flask import Flask, request, jsonify, render_template, redirect, url_for, send_from_directory
    from flask_sqlalchemy import SQLAlchemy
    from sqlalchemy import inspect
    
    # Create a temporary Flask context for database operations
    with app.app_context():
        # Create tables if they don't exist
        db.create_all()
        print("--- Database Tables Created/Verified ---\r\n")
        
        # Run migration check just in case
        check_and_upgrade_db()
        print("--- Database Schema Upgraded/Verified ---\r\n")

if __name__ == '__main__':
    initialize_database() 
    app.run(debug=True, host='0.0.0.0', port=5000)