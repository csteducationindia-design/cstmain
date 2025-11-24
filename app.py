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

# --- Basic Setup ---
app = Flask(__name__, template_folder='templates')
app.config['SECRET_KEY'] = 'a_very_secret_key_that_should_be_changed'
CORS(app, supports_credentials=True)

# --- Firebase Initialization ---
if not firebase_admin._apps:
    firebase_env = os.environ.get('FIREBASE_CREDENTIALS_JSON')
    if firebase_env:
        try:
            # Clean string just in case
            firebase_env = firebase_env.strip("'").strip('"')
            cred_dict = json.loads(firebase_env)
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
            print("--- Firebase Initialized from Env Var ---")
        except json.JSONDecodeError as e:
            print(f"--- FIREBASE JSON ERROR: {e} ---")
    elif os.path.exists("firebase_credentials.json"):
        cred = credentials.Certificate("firebase_credentials.json")
        firebase_admin.initialize_app(cred)
        print("--- Firebase Initialized from Local File ---")
    else:
        print("--- WARNING: No Firebase Credentials Found ---")

# --- Database Configuration (Persistent) ---
basedir = os.path.abspath(os.path.dirname(__file__))
data_dir = os.path.join(basedir, 'data')

if not os.path.exists(data_dir):
    try:
        os.makedirs(data_dir)
        print(f"--- Created persistent data directory: {data_dir} ---")
    except OSError as e:
        print(f"--- Error creating data directory: {e} ---")

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(data_dir, 'institute.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- Database Migration Helper (Runs automatically) ---
def check_and_upgrade_db():
    with app.app_context():
        try:
            inspector = inspect(db.engine)
            if inspector.has_table("user"):
                columns = [col['name'] for col in inspector.get_columns('user')]
                if 'fcm_token' not in columns:
                    with db.engine.connect() as conn:
                        conn.execute(text("ALTER TABLE user ADD COLUMN fcm_token VARCHAR(500)"))
                        conn.commit()
                if 'pincode' not in columns:
                    with db.engine.connect() as conn:
                        conn.execute(text("ALTER TABLE user ADD COLUMN pincode VARCHAR(20)"))
                        conn.commit()
        except Exception as e:
            print(f"--- MIGRATION WARNING: {e} ---")
check_and_upgrade_db()

# --- Upload Configuration ---
UPLOAD_FOLDER = os.path.join(basedir, 'uploads')
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
ALLOWED_NOTE_EXTENSIONS = {'txt', 'pdf', 'doc', 'docx', 'ppt', 'pptx'}
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

# --- Security ---
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'serve_login_page'

@login_manager.user_loader
def load_user(user_id):
    try:
        user_id_int = int(user_id)
    except (ValueError, TypeError):
        return None
    return db.session.get(User, user_id_int)

# --- Models ---
student_course_association = db.Table('student_course',
    db.Column('student_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('course_id', db.Integer, db.ForeignKey('course.id'), primary_key=True)
)

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(60), nullable=False)
    role = db.Column(db.String(50), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    phone_number = db.Column(db.String(20), nullable=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    can_edit = db.Column(db.Boolean, default=True)
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
    
    children = db.relationship('User', foreign_keys=[parent_id], backref=db.backref('parent', remote_side=[id]))
    courses_enrolled = db.relationship('Course', secondary=student_course_association, lazy='subquery', backref=db.backref('students', lazy=True))

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "email": self.email, "role": self.role,
            "created_at": self.created_at.strftime('%Y-%m-%d'), "phone_number": self.phone_number,
            "parent_id": self.parent_id, "can_edit": self.can_edit, "dob": self.dob,
            "profile_photo_url": self.profile_photo_url, "gender": self.gender,
            "father_name": self.father_name, "mother_name": self.mother_name,
            "address_line1": self.address_line1, "city": self.city, "state": self.state, "pincode": self.pincode,
            "course_ids": [c.id for c in self.courses_enrolled] if self.role == 'student' else []
        }

class Course(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    subjects = db.Column(db.String(300), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    teacher = db.relationship('User', backref=db.backref('courses', lazy=True))

    def to_dict(self):
        return {"id": self.id, "name": self.name, "subjects": [s.strip() for s in self.subjects.split(',')] if self.subjects else [], "teacher_id": self.teacher_id, "teacher_name": self.teacher.name if self.teacher else "Unassigned"}

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
    total_amount = db.Column(db.Float, nullable=False)
    due_date = db.Column(db.Date, nullable=False, default=date.today)
    def to_dict(self):
        session = db.session.get(AcademicSession, self.academic_session_id)
        return {"id": self.id, "name": self.name, "academic_session_id": self.academic_session_id, "session_name": session.name if session else "N/A", "total_amount": self.total_amount, "due_date": self.due_date.strftime('%Y-%m-%d')}

class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    fee_structure_id = db.Column(db.Integer, db.ForeignKey('fee_structure.id'), nullable=False)
    amount_paid = db.Column(db.Float, nullable=False)
    payment_date = db.Column(db.DateTime, default=datetime.utcnow)
    payment_method = db.Column(db.String(50), nullable=False)
    def to_dict(self): return {"id": self.id, "student_id": self.student_id, "fee_structure_id": self.fee_structure_id, "amount_paid": self.amount_paid, "payment_date": self.payment_date.strftime('%Y-%m-%d %H:%M'), "payment_method": self.payment_method}

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
        return {"id": self.id, "sender_id": self.sender_id, "sender_name": sender.name if sender else "N/A", "recipient_id": self.recipient_id, "content": self.content, "sent_at": self.sent_at.strftime('%Y-%m-%d %H:%M')}

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
    def to_dict(self): return {"id": self.id, "filename": self.filename, "original_filename": self.original_filename, "title": self.title, "description": self.description, "course_id": self.course_id, "course_name": self.course.name if self.course else "N/A", "teacher_id": self.teacher_id, "teacher_name": self.teacher.name if self.teacher else "N/A", "created_at": self.created_at.strftime('%Y-%m-%d %H:%M')}

# --- Helper Functions ---
def allowed_file(filename, extension_set):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in extension_set

def send_actual_sms(phone_number, message_body, template_id=None):
    base_url = os.environ.get('SMS_API_URL', 'http://servermsg.com/api/SmsApi/SendSingleApi')
    user_id = os.environ.get('SMS_API_USER_ID')
    password = os.environ.get('SMS_API_PASSWORD')
    sender_id = os.environ.get('SMS_API_SENDER_ID')
    entity_id = os.environ.get('SMS_API_ENTITY_ID')
    if not template_id: template_id = os.environ.get('SMS_API_DEFAULT_TEMPLATE_ID')
    if not all([user_id, password, sender_id, entity_id, template_id, phone_number]): return False
    payload = {'UserID': user_id, 'Password': password, 'SenderID': sender_id, 'Phno': phone_number, 'Msg': message_body, 'EntityID': entity_id, 'TemplateID': template_id}
    try:
        requests.get(base_url, params=payload, timeout=10)
        return True
    except: return False

def send_push_notification(user_id, title, body):
    user = db.session.get(User, user_id)
    if user and user.fcm_token:
        try:
            message = messaging.Message(notification=messaging.Notification(title=title, body=body), token=user.fcm_token)
            messaging.send(message)
            return True
        except: return False
    return False

def send_fee_alert_sms(user, student_name, balance, due_date):
    """
    UPDATED: Accepts student_name explicitly so parent gets 'Dear [Student Name]'
    """
    if user and user.phone_number:
        clean_balance = int(balance)
        try:
            d_obj = datetime.strptime(due_date, '%Y-%m-%d')
            formatted_date = d_obj.strftime('%d-%b-%Y')
        except:
            formatted_date = due_date
            
        institute_phone = "9822826307"
        # Use student_name in the message, even if sending to parent
        message = f"Dear {student_name}, your fee of Rs {clean_balance} is pending. Due: {formatted_date}. CST Institute {institute_phone}"
        fee_template_id = "1707176388002841408"
        return send_actual_sms(user.phone_number, message, template_id=fee_template_id)
    return False

def send_mock_whatsapp(user, subject, body):
    return True

def calculate_fee_status(student_id):
    payments = Payment.query.filter_by(student_id=student_id).all()
    total_paid = sum(p.amount_paid for p in payments)
    active_fee_structure = FeeStructure.query.order_by(FeeStructure.id.desc()).first()
    total_due = active_fee_structure.total_amount if active_fee_structure else 0.00
    due_date = active_fee_structure.due_date if active_fee_structure else date.today()
    balance = total_due - total_paid
    pending_days = 0
    try:
        if balance > 0 and isinstance(due_date, date):
            today = date.today()
            if today > due_date: pending_days = (today - due_date).days * -1 
            else: pending_days = (due_date - today).days 
    except: pass
    return {"total_due": total_due, "total_paid": total_paid, "balance": balance, "due_date": due_date.strftime('%Y-%m-%d'), "pending_days": pending_days}

# --- Routes ---
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
def logout(): logout_user(); return jsonify({"message": "Logout successful"})

@app.route('/api/check_session', methods=['GET'])
def check_session(): return jsonify({"logged_in": True, "user": current_user.to_dict()}) if current_user.is_authenticated else jsonify({"logged_in": False}), 401

@app.route('/api/save_fcm_token', methods=['POST'])
@login_required
def save_fcm_token():
    token = request.json.get('token')
    if token:
        current_user.fcm_token = token
        db.session.commit()
        return jsonify({"message": "Token saved"}), 200
    return jsonify({"message": "No token provided"}), 400

# --- Admin API Endpoints ---
@app.route('/api/users', methods=['GET', 'POST', 'PUT'])
@login_required
def manage_users():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    if request.method == 'POST':
        data = request.form
        if User.query.filter_by(email=data['email']).first(): return jsonify({"message": "Email exists"}), 400
        hashed_password = bcrypt.generate_password_hash(data['password']).decode('utf-8')
        profile_photo_url = None
        if 'profile_photo_file' in request.files:
            file = request.files['profile_photo_file']
            if file and allowed_file(file.filename, ALLOWED_IMAGE_EXTENSIONS):
                filename = f"{uuid.uuid4()}{os.path.splitext(file.filename)[1]}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                profile_photo_url = f"/uploads/{filename}"
        new_user = User(name=data['name'], email=data['email'], password=hashed_password, role=data['role'], phone_number=data.get('phone_number'), parent_id=int(data.get('parent_id')) if data.get('parent_id') else None, can_edit=True, dob=data.get('dob'), profile_photo_url=profile_photo_url, gender=data.get('gender'), father_name=data.get('father_name'), mother_name=data.get('mother_name'), address_line1=data.get('address_line1'), city=data.get('city'), state=data.get('state'), pincode=data.get('pincode'))
        db.session.add(new_user)
        if new_user.role == 'student' and request.form.get('course_ids'):
            course = db.session.get(Course, int(request.form.get('course_ids')))
            if course: new_user.courses_enrolled.append(course)
        db.session.commit()
        return jsonify(new_user.to_dict()), 201
    if request.method == 'PUT':
        data = request.form
        user = db.session.get(User, data.get('id'))
        if not user: return jsonify({"message": "User not found"}), 404
        user.name = data.get('name', user.name)
        user.email = data.get('email', user.email)
        user.phone_number = data.get('phone_number', user.phone_number)
        if 'parent_id' in data: user.parent_id = int(data.get('parent_id')) if data.get('parent_id') else None
        if data.get('password'): user.password = bcrypt.generate_password_hash(data.get('password')).decode('utf-8')
        if user.role == 'student' and request.form.get('course_ids'):
            user.courses_enrolled = []
            course = db.session.get(Course, int(request.form.get('course_ids')))
            if course: user.courses_enrolled.append(course)
        db.session.commit()
        return jsonify(user.to_dict()), 200
    
    search = request.args.get('search', '').lower()
    query = User.query
    if search: query = query.filter(or_(User.name.ilike(f'%{search}%'), User.email.ilike(f'%{search}%'), User.phone_number.ilike(f'%{search}%')))
    return jsonify([u.to_dict() for u in query.all()])

@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@login_required
def delete_user(user_id):
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    user = db.session.get(User, user_id)
    if user: db.session.delete(user); db.session.commit(); return jsonify({'message': 'Deleted'})
    return jsonify({'message': 'Not found'}), 404

@app.route('/api/send_sms_alert', methods=['POST'])
@login_required
def send_fee_alert():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    data = request.get_json()
    student_id = data.get('student_id')
    student = db.session.get(User, student_id)
    if not student: return jsonify({"message": "Student not found"}), 404
    status = calculate_fee_status(student_id)
    
    # Update: Pass student.name to ensure correct name in SMS
    student_alerted = send_fee_alert_sms(student, student.name, status['balance'], status['due_date'])

    parent_alerted = False
    parent = db.session.get(User, student.parent_id) if student.parent_id else None
    if parent:
        # Update: Send student.name even to parent
        parent_alerted = send_fee_alert_sms(parent, student.name, status['balance'], status['due_date'])

    # Push Notification
    push_msg = f"Dear {student.name}, fee of Rs {int(status['balance'])} is pending."
    send_push_notification(student_id, "Fee Reminder", push_msg)
    if parent: send_push_notification(parent.id, "Fee Reminder", push_msg)
    
    return jsonify({"message": "Fee Alert Sent"}), 200

@app.route('/api/teacher/attendance', methods=['POST'])
@login_required
def record_attendance():
    if current_user.role != 'teacher': return jsonify({"message": "Access denied"}), 403
    data = request.get_json()
    attendance_date = datetime.strptime(data['date'], '%Y-%m-%d').date()
    attendance_template_id = "1707176388022694296"
    
    for record in data['attendance_data']:
        existing = Attendance.query.filter_by(student_id=record['student_id']).filter(db.func.date(Attendance.check_in_time) == attendance_date).first()
        if existing: existing.status = record['status']
        else: db.session.add(Attendance(student_id=record['student_id'], check_in_time=datetime.combine(attendance_date, datetime.min.time()), status=record['status']))
        
        student = db.session.get(User, record['student_id'])
        parent = db.session.get(User, student.parent_id) if student.parent_id else None
        
        if record['status'] == 'Absent':
            # SMS
            try: fmt_date = attendance_date.strftime('%d-%b-%Y')
            except: fmt_date = str(attendance_date)
            msg = f"Dear {student.name}, your attendance is marked Absent for date {fmt_date}. Please contact CST Institute."
            if student.phone_number: send_actual_sms(student.phone_number, msg, template_id=attendance_template_id)
            if parent and parent.phone_number: send_actual_sms(parent.phone_number, msg, template_id=attendance_template_id)
            
            # Push
            send_push_notification(student.id, "Attendance Alert", f"Marked ABSENT for {fmt_date}")
            if parent: send_push_notification(parent.id, "Attendance Alert", f"{student.name} marked ABSENT for {fmt_date}")
            
        elif record['status'] == 'Present':
             # Push Only
             send_push_notification(student.id, "Attendance", "Marked PRESENT")
             if parent: send_push_notification(parent.id, "Attendance", f"{student.name} marked PRESENT")

    db.session.commit()
    return jsonify({"message": "Attendance recorded"}), 201

@app.route('/api/teacher/upload_note', methods=['POST'])
@login_required
def upload_note():
    if current_user.role != 'teacher': return jsonify({"message": "Access denied"}), 403
    file = request.files['file']
    if file and allowed_file(file.filename, ALLOWED_NOTE_EXTENSIONS):
        filename = f"{uuid.uuid4()}{os.path.splitext(file.filename)[1]}"
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        new_note = SharedNote(filename=filename, original_filename=secure_filename(file.filename), title=request.form.get('title'), description=request.form.get('description'), course_id=int(request.form.get('course_id')), teacher_id=current_user.id)
        db.session.add(new_note)
        db.session.commit()
        
        # Push Notification
        course = db.session.get(Course, new_note.course_id)
        if course:
            for student in course.students:
                send_push_notification(student.id, "New Study Material", f"New note in {course.name}: {new_note.title}")

        return jsonify({"message": "Note uploaded", "note": new_note.to_dict()}), 201
    return jsonify({"message": "Invalid file"}), 400

@app.route('/api/announcements', methods=['GET', 'POST'])
@login_required
def manage_announcements():
    if request.method == 'POST':
        if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
        data = request.get_json()
        new_announcement = Announcement(title=data['title'], content=data['content'], target_group=data['target_group'])
        db.session.add(new_announcement)
        db.session.commit()
        
        # Push Notification
        target = data['target_group']
        recipients = User.query.all() if target == 'all' else User.query.filter_by(role={'teachers':'teacher', 'students':'student', 'parents':'parent'}.get(target, target)).all()
        for user in recipients:
            if user.id != current_user.id: send_push_notification(user.id, f"📢 {data['title']}", data['content'][:100])
            
        return jsonify(new_announcement.to_dict()), 201
    return jsonify([a.to_dict() for a in Announcement.query.order_by(Announcement.created_at.desc()).all()])

@app.route('/api/announcements/<int:announcement_id>', methods=['DELETE'])
@login_required
def delete_announcement(announcement_id):
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    announcement = db.session.get_or_404(Announcement, announcement_id)
    db.session.delete(announcement)
    db.session.commit()
    return jsonify({'message': 'Announcement deleted successfully'})

# --- Other Standard Routes ---
@app.route('/api/courses', methods=['GET', 'POST', 'PUT'])
@login_required
def manage_courses():
    if request.method == 'GET': return jsonify([c.to_dict() for c in Course.query.all()])
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    data = request.get_json()
    if request.method == 'POST':
        db.session.add(Course(name=data['name'], subjects=data['subjects'], teacher_id=int(data['teacher_id']) if data.get('teacher_id') else None))
    elif request.method == 'PUT':
        c = db.session.get(Course, data['id'])
        if c: c.name=data['name']; c.subjects=data['subjects']; c.teacher_id=int(data['teacher_id']) if data.get('teacher_id') else None
    db.session.commit()
    return jsonify({"message": "Saved"}), 200

@app.route('/api/courses/<int:course_id>', methods=['DELETE'])
@login_required
def delete_course(course_id):
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    course = db.session.get_or_404(Course, course_id)
    db.session.delete(course)
    db.session.commit()
    return jsonify({'message': 'Course deleted'})

@app.route('/api/sessions', methods=['GET', 'POST', 'PUT'])
@login_required
def manage_sessions():
    if request.method == 'GET': return jsonify([s.to_dict() for s in AcademicSession.query.all()])
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    data = request.get_json()
    if request.method == 'POST':
        db.session.add(AcademicSession(name=data['name'], start_date=data['start_date'], end_date=data['end_date'], status=data['status']))
    elif request.method == 'PUT':
        s = db.session.get(AcademicSession, data['id'])
        if s: s.name=data['name']; s.start_date=data['start_date']; s.end_date=data['end_date']; s.status=data['status']
    db.session.commit()
    return jsonify({"message": "Saved"}), 200

@app.route('/api/sessions/<int:session_id>', methods=['DELETE'])
@login_required
def delete_session(session_id):
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    session = db.session.get_or_404(AcademicSession, session_id)
    db.session.delete(session)
    db.session.commit()
    return jsonify({'message': 'Session deleted'})

@app.route('/api/fee_structures', methods=['GET', 'POST', 'PUT'])
@login_required
def manage_fee_structures():
    if request.method == 'GET': return jsonify([f.to_dict() for f in FeeStructure.query.all()])
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    data = request.get_json()
    try: due = datetime.strptime(data['due_date'], '%Y-%m-%d').date()
    except: return jsonify({"message": "Invalid date"}), 400
    if request.method == 'POST':
        db.session.add(FeeStructure(name=data['name'], academic_session_id=data['academic_session_id'], total_amount=data['total_amount'], due_date=due))
    elif request.method == 'PUT':
        f = db.session.get(FeeStructure, data['id'])
        if f: f.name=data['name']; f.academic_session_id=data['academic_session_id']; f.total_amount=data['total_amount']; f.due_date=due
    db.session.commit()
    return jsonify({"message": "Saved"}), 200

@app.route('/api/payments', methods=['POST'])
@login_required
def record_payment():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    data = request.get_json()
    new_payment = Payment(student_id=data['student_id'], fee_structure_id=data['fee_structure_id'], amount_paid=data['amount_paid'], payment_method=data['payment_method'])
    db.session.add(new_payment)
    db.session.commit()
    return jsonify({"message": "Payment recorded", "payment_id": new_payment.id}), 201

@app.route('/api/receipt/<int:payment_id>', methods=['GET'])
def serve_receipt(payment_id):
    payment = db.session.get(Payment, payment_id)
    if not payment: return "Not Found", 404
    student = db.session.get(User, payment.student_id)
    fee_struct = db.session.get(FeeStructure, payment.fee_structure_id)
    html = f"""<html><head><title>Receipt #{payment.id}</title><style>body {{ font-family: sans-serif; padding: 20px; }} .receipt {{ border: 1px solid #ddd; padding: 20px; max-width: 500px; margin: auto; }} table {{ width: 100%; margin-top: 20px; }} td {{ padding: 8px; border-bottom: 1px solid #eee; }} .print-btn {{ display: block; margin: 20px auto; padding: 10px 20px; background: #007bff; color: white; border: none; cursor: pointer; }} @media print {{ .print-btn {{ display: none; }} }}</style></head><body><div class="receipt"><h1>CST Institute</h1><p style="text-align: center;">Payment Receipt</p><table><tr><td>Receipt No:</td><td>#{payment.id}</td></tr><tr><td>Date:</td><td>{payment.payment_date.strftime('%d-%b-%Y')}</td></tr><tr><td>Student:</td><td>{student.name}</td></tr><tr><td>Fee Type:</td><td>{fee_struct.name if fee_struct else 'N/A'}</td></tr><tr><td>Amount:</td><td><strong>Rs. {payment.amount_paid}</strong></td></tr></table><button class="print-btn" onclick="window.print()">Print Receipt</button></div></body></html>"""
    return html

@app.route('/api/reports/fee_pending', methods=['GET'])
@login_required
def fee_pending_report():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    students = User.query.filter_by(role='student').all()
    report = []
    for s in students:
        status = calculate_fee_status(s.id)
        if status['balance'] > 0:
            report.append({"student_id": s.id, "student_name": s.name, "balance": status['balance'], "due_date": status['due_date'], "pending_days": status['pending_days'], "phone_number": s.phone_number})
    return jsonify(report)

# --- Student/Parent/Teacher Specific Routes ---
@app.route('/api/student/fees', methods=['GET'])
@login_required
def get_student_fees():
    status = calculate_fee_status(current_user.id)
    payments = Payment.query.filter_by(student_id=current_user.id).order_by(Payment.payment_date.desc()).all()
    history = [{"date": p.payment_date.strftime('%Y-%m-%d'), "amount": p.amount_paid, "method": p.payment_method} for p in payments]
    return jsonify({**status, "history": history})

@app.route('/api/student/notes', methods=['GET'])
@login_required
def get_student_notes():
    if current_user.role != 'student': return jsonify({"message": "Access denied"}), 403
    course_ids = [c.id for c in current_user.courses_enrolled]
    notes = SharedNote.query.filter(SharedNote.course_id.in_(course_ids)).order_by(SharedNote.created_at.desc()).all()
    return jsonify([n.to_dict() for n in notes])

@app.route('/api/parent/children', methods=['GET'])
@login_required
def get_parent_children():
    if current_user.role != 'parent': return jsonify({"message": "Access denied"}), 403
    children = User.query.filter_by(parent_id=current_user.id).all()
    return jsonify([c.to_dict() for c in children])

@app.route('/api/parent/child_data/<int:student_id>', methods=['GET'])
@login_required
def get_child_data(student_id):
    if current_user.role != 'parent': return jsonify({"message": "Access denied"}), 403
    student = db.session.get(User, student_id)
    if not student or student.parent_id != current_user.id: return jsonify({"message": "Not authorized"}), 403
    
    attendance = Attendance.query.filter_by(student_id=student.id).order_by(Attendance.check_in_time.desc()).limit(5).all()
    att_data = [{"date": a.check_in_time.strftime('%Y-%m-%d'), "time": a.check_in_time.strftime('%H:%M'), "status": a.status} for a in attendance]
    
    grades = Grade.query.filter_by(student_id=student.id).all()
    grade_data = []
    for g in grades:
        course = db.session.get(Course, g.course_id)
        grade_data.append({"course_name": course.name if course else "N/A", "assessment_name": g.assessment_name, "marks_obtained": g.marks_obtained, "total_marks": g.total_marks})
        
    fees = calculate_fee_status(student.id)
    
    return jsonify({
        "profile": student.to_dict(),
        "attendance": att_data,
        "grades": grade_data,
        "fees": fees
    })

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
    course_ids = [c.id for c in Course.query.filter_by(teacher_id=current_user.id).all()]
    students = User.query.join(student_course_association).join(Course).filter(Course.id.in_(course_ids)).distinct().all()
    return jsonify([s.to_dict() for s in students])
    
@app.route('/api/teacher/notify', methods=['POST'])
@login_required
def send_notification_to_student():
    if current_user.role not in ['teacher', 'admin']: return jsonify({"message": "Access denied"}), 403
    data = request.get_json()
    student = db.session.get(User, data.get('student_id'))
    if not student: return jsonify({"message": "Student not found"}), 404
    
    msg_type = data.get('type')
    subject = data.get('subject')
    body = data.get('body')
    
    db.session.add(Message(sender_id=current_user.id, recipient_id=student.id, content=f"{subject}: {body}"))
    db.session.commit()
    
    if msg_type == 'push':
        send_push_notification(student.id, subject, body)
        if student.parent_id: send_push_notification(student.parent_id, f"Child Alert: {subject}", body)
    
    return jsonify({"message": "Notification sent"}), 200

@app.route('/api/user/upload_photo/<int:user_id>', methods=['POST'])
@login_required
def upload_profile_photo(user_id):
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    file = request.files.get('profile_photo_file')
    if file and allowed_file(file.filename, ALLOWED_IMAGE_EXTENSIONS):
        filename = f"{uuid.uuid4()}{os.path.splitext(file.filename)[1]}"
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        user = db.session.get(User, user_id)
        user.profile_photo_url = f"/uploads/{filename}"
        db.session.commit()
        return jsonify({"message": "Uploaded", "user": user.to_dict()}), 200
    return jsonify({"message": "Invalid file"}), 400

# --- Serving Static Files ---
@app.route('/')
def serve_login_page():
    if current_user.is_authenticated: return redirect(url_for(f'serve_{current_user.role}_page'))
    return render_template('login.html')

@app.route('/admin')
@login_required
def serve_admin_page(): return render_template('admin.html') if current_user.role == 'admin' else redirect('/')

@app.route('/teacher')
@login_required
def serve_teacher_page(): return render_template('teacher.html') if current_user.role == 'teacher' else redirect('/')

@app.route('/student')
@login_required
def serve_student_page(): return render_template('student.html') if current_user.role == 'student' else redirect('/')

@app.route('/parent')
@login_required
def serve_parent_page(): return render_template('parent.html') if current_user.role == 'parent' else redirect('/')

@app.route('/uploads/<filename>')
def serve_file(filename): return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/firebase-messaging-sw.js')
def sw(): return send_from_directory(app.static_folder, 'firebase-messaging-sw.js')

if __name__ == '__main__':
    with app.app_context(): db.create_all()
    app.run(debug=True, host='0.0.0.0', port=5000)