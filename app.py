from flask import Flask, request, jsonify, render_template, redirect, url_for, send_from_directory, send_file
from sqlalchemy import or_, inspect, text
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
import firebase_admin
from firebase_admin import credentials, messaging
import logging
import pandas as pd
from io import BytesIO
import threading

# =========================================================
# CONFIGURATION & SETUP
# =========================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'fallback_secret_key_change_this')
CORS(app, supports_credentials=True)

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
    if firebase_admin._apps: return True
    firebase_env = os.environ.get('FIREBASE_CREDENTIALS_JSON')
    if firebase_env:
        try:
            val = firebase_env.strip()
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            val = val.replace('\\"', '"')
            cred_dict = json.loads(val)
            if 'private_key' in cred_dict:
                cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred)
            return True
        except Exception as e:
            logger.error(f"Firebase Init Error: {e}")
    return False

def send_push_notification(user_id, title, body):
    if not init_firebase(): return "Firebase not initialized"
    with app.app_context():
        user = db.session.get(User, user_id)
        if not user or not user.fcm_token: return "No Token"
        try:
            message = messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                token=user.fcm_token,
            )
            messaging.send(message)
            return "Sent"
        except Exception as e:
            return f"Error: {str(e)}"

def send_actual_sms(phone_number, message_body, template_id=None):
    base_url = os.environ.get('SMS_API_URL', 'http://servermsg.com/api/SmsApi/SendSingleApi')
    user_id = os.environ.get('SMS_API_USER_ID')
    password = os.environ.get('SMS_API_PASSWORD')
    sender_id = os.environ.get('SMS_API_SENDER_ID')
    entity_id = os.environ.get('SMS_API_ENTITY_ID')
    
    if not template_id:
        template_id = os.environ.get('SMS_API_DEFAULT_TEMPLATE_ID', '1707176388002841408')

    if not all([user_id, password, sender_id, entity_id, template_id, phone_number]):
        return False

    payload = {
        'UserID': user_id, 'Password': password, 'SenderID': sender_id,
        'Phno': phone_number, 'Msg': message_body, 'EntityID': entity_id, 'TemplateID': template_id
    }

    try:
        response = requests.get(base_url, params=payload, timeout=10)
        return response.status_code == 200
    except:
        return False

def allowed_file(filename, extension_set):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in extension_set

def background_notify(app_ctx, user_ids, subject, body):
    with app_ctx:
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

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(60), nullable=False)
    role = db.Column(db.String(50), nullable=False)
    admission_number = db.Column(db.String(50), unique=True, nullable=True)
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
    session_id = db.Column(db.Integer, db.ForeignKey('academic_session.id'), nullable=True)
    
    courses_enrolled = db.relationship('Course', secondary=student_course_association, lazy='subquery', backref=db.backref('students', lazy=True))

    def to_dict(self):
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
            "session_id": self.session_id, 
            "session_name": sess_name,
            "admission_number": self.admission_number,
            "course_ids": [c.id for c in self.courses_enrolled] if self.role == 'student' else []
        }

class Course(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    subjects = db.Column(db.String(300), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    teacher = db.relationship('User', backref=db.backref('courses_teaching', lazy=True))
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
    category = db.Column(db.String(50), nullable=False, default='General')
    target_group = db.Column(db.String(50), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    def to_dict(self):
        return {
            "id": self.id, "title": self.title, "content": self.content,
            "category": self.category, "target_group": self.target_group,
            "created_at": self.created_at.strftime('%Y-%m-%d %H:%M')
        }

class FeeStructure(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    academic_session_id = db.Column(db.Integer, db.ForeignKey('academic_session.id'), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=True)
    total_amount = db.Column(db.Float, nullable=False)
    due_date = db.Column(db.Date, nullable=False, default=date.today)
    def to_dict(self):
        session = db.session.get(AcademicSession, self.academic_session_id)
        course = db.session.get(Course, self.course_id) if self.course_id else None
        return {
            "id": self.id, "name": self.name, "session_name": session.name if session else "N/A",
            "course_name": course.name if course else "Global", "total_amount": self.total_amount,
            "due_date": self.due_date.strftime('%Y-%m-%d') if self.due_date else None,
            "academic_session_id": self.academic_session_id, "course_id": self.course_id
        }

class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    fee_structure_id = db.Column(db.Integer, db.ForeignKey('fee_structure.id'), nullable=False)
    amount_paid = db.Column(db.Float, nullable=False)
    payment_date = db.Column(db.DateTime, default=datetime.utcnow)
    payment_method = db.Column(db.String(50), nullable=False)
    def to_dict(self): return {"id": self.id, "student_id": self.student_id, "amount_paid": self.amount_paid, "payment_date": self.payment_date.strftime('%Y-%m-%d %H:%M'), "payment_method": self.payment_method}

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

class SharedNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(300)); original_filename = db.Column(db.String(300))
    title = db.Column(db.String(150)); description = db.Column(db.Text)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'))
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    course = db.relationship('Course', backref=db.backref('notes', lazy=True))
    def to_dict(self): return {"id": self.id, "title": self.title, "course_name": self.course.name if self.course else "N/A", "filename": self.filename, "created_at": self.created_at.strftime('%Y-%m-%d')}

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
# HELPERS
# =========================================================

# --- HELPER FUNCTION FOR FEES (Must be in app.py) ---
def calculate_fee_status(student_id):
    try:
        student = db.session.get(User, student_id)
        if not student: 
            return {"total_due": 0, "total_paid": 0, "balance": 0, "due_date": "N/A", "pending_days": 0}

        payments = Payment.query.filter_by(student_id=student_id).all()
        total_paid = sum(p.amount_paid for p in payments)
        total_due = 0.0
        due_dates = []

        # Calculate Course Fees
        if student.courses_enrolled:
            for course in student.courses_enrolled:
                for fee_struct in FeeStructure.query.filter_by(course_id=course.id).all():
                    total_due += fee_struct.total_amount
                    if fee_struct.due_date: due_dates.append(fee_struct.due_date)
        
        # Calculate Session Fees
        if student.session_id:
            for gf in FeeStructure.query.filter(FeeStructure.course_id == None, FeeStructure.academic_session_id == student.session_id).all():
                total_due += gf.total_amount
                if gf.due_date: due_dates.append(gf.due_date)

        final_due_date = min(due_dates) if due_dates else date.today()
        balance = total_due - total_paid
        pending_days = (date.today() - final_due_date).days if balance > 0 and date.today() > final_due_date else 0

        return {
            "total_due": total_due, 
            "total_paid": total_paid, 
            "balance": balance, 
            "due_date": final_due_date.strftime('%d-%b-%Y'), # Formats as 25-Nov-2025
            "pending_days": pending_days
        }
    except Exception as e:
        print(f"Fee Calculation Error: {e}")
        return {"balance": 0, "due_date": "N/A"}

def send_fee_alert_notifications(student_id):
    student = db.session.get(User, student_id)
    if not student: return False
    st = calculate_fee_status(student_id)
    if st['balance'] > 0:
        send_push_notification(student.id, "Fee Reminder", f"Fee Pending: {st['balance']}")
        if student.phone_number: send_actual_sms(student.phone_number, f"Dear {student.name}, Fee Pending: {st['balance']}")
    return True

# =========================================================
# ROUTES
# =========================================================

@app.route('/')
def serve_login_page():
    if current_user.is_authenticated: return redirect(f"/{current_user.role}")
    return render_template('login.html')

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    u = User.query.filter_by(email=data.get('email')).first()
    if u and bcrypt.check_password_hash(u.password, data.get('password')):
        login_user(u, remember=True)
        return jsonify({"message": "OK", "user": u.to_dict()})
    return jsonify({"message": "Invalid"}), 401

@app.route('/api/logout', methods=['POST'])
@login_required
def logout(): logout_user(); return jsonify({"message": "OK"})

@app.route('/api/check_session')
def check_session():
    if current_user.is_authenticated: return jsonify({"logged_in": True, "user": current_user.to_dict()})
    return jsonify({"logged_in": False}), 401

@app.route('/api/save_fcm_token', methods=['POST'])
@login_required
def save_fcm_token():
    token = request.json.get('token')
    if token:
        current_user.fcm_token = token
        db.session.commit()
    return jsonify({"msg": "Saved"})

@app.route('/<role>')
@login_required
def serve_role_page(role):
    if role in ['admin', 'teacher', 'student', 'parent'] and current_user.role == role:
        return render_template(f'{role}.html')
    return redirect('/')

@app.route('/firebase-messaging-sw.js')
def sw(): return send_from_directory(app.static_folder, 'firebase-messaging-sw.js')

# --- ADMIN ROUTES ---
@app.route('/api/users', methods=['GET', 'POST', 'PUT'])
@login_required
def api_users():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    
    # GET: List Users (with safer filtering)
    if request.method == 'GET':
        search = request.args.get('search', '').lower()
        session_id = request.args.get('session_id')
        
        q = User.query
        
        # FIX: Check if session_id is a valid number before converting
        if session_id and session_id != 'null' and str(session_id).isdigit():
             q = q.filter_by(session_id=int(session_id))
             
        if search: 
            q = q.filter(or_(User.name.ilike(f'%{search}%'), User.email.ilike(f'%{search}%')))
            
        return jsonify([u.to_dict() for u in q.all()])

    # POST/PUT: Save User
    d = request.form
    if request.method == 'POST':
        if User.query.filter_by(email=d['email']).first(): return jsonify({"msg": "Email exists"}), 400
        u = User(name=d['name'], email=d['email'], password=bcrypt.generate_password_hash(d['password']).decode('utf-8'), role=d.get('role', 'student'))
        db.session.add(u)
    else: # PUT
        u = db.session.get(User, int(d['id']))
        if not u: return jsonify({"msg": "Not found"}), 404
        if d.get('password'): u.password = bcrypt.generate_password_hash(d['password']).decode('utf-8')

    # Common Fields
    u.name = d['name']; u.email = d['email']; u.phone_number = d.get('phone_number')
    u.admission_number = d.get('admission_number')
    u.dob = d.get('dob'); u.gender = d.get('gender')
    u.father_name = d.get('father_name'); u.mother_name = d.get('mother_name')
    u.address_line1 = d.get('address_line1'); u.city = d.get('city'); u.state = d.get('state'); u.pincode = d.get('pincode')
    
    # FIX: Ensure session_id is saved correctly
    if d.get('session_id') and str(d.get('session_id')).isdigit():
        u.session_id = int(d.get('session_id'))
    
    if d.get('parent_id') and str(d.get('parent_id')).isdigit():
        u.parent_id = int(d.get('parent_id'))

    # Photo Upload
    if 'profile_photo_file' in request.files:
        file = request.files['profile_photo_file']
        if file and allowed_file(file.filename, ALLOWED_IMAGE_EXTENSIONS):
            fn = secure_filename(file.filename)
            uid = f"{uuid.uuid4()}{os.path.splitext(fn)[1]}"
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], uid))
            u.profile_photo_url = f"/uploads/{uid}"

    # Courses
    if u.role == 'student':
        c_ids = request.form.getlist('course_ids')
        if c_ids:
            u.courses_enrolled = Course.query.filter(Course.id.in_([int(cid) for cid in c_ids if str(cid).isdigit()])).all()

    db.session.commit()
    return jsonify(u.to_dict())

@app.route('/api/admin/student/<int:id>', methods=['GET'])
@login_required
def get_student(id):
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    return jsonify(db.session.get(User, id).to_dict())

@app.route('/api/users/<int:id>', methods=['DELETE'])
@login_required
def delete_user(id):
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    u = db.session.get(User, id)
    if u:
        if u.role == 'student':
            Payment.query.filter_by(student_id=id).delete()
            Attendance.query.filter_by(student_id=id).delete()
            u.courses_enrolled = []
        db.session.delete(u)
        db.session.commit()
    return jsonify({"msg": "Deleted"})

@app.route('/api/sessions', methods=['GET', 'POST', 'PUT'])
@login_required
def manage_sessions():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    if request.method == 'GET': return jsonify([s.to_dict() for s in AcademicSession.query.all()])
    d = request.json
    if request.method == 'POST':
        db.session.add(AcademicSession(name=d['name'], start_date=d['start_date'], end_date=d['end_date'], status=d['status']))
    elif request.method == 'PUT':
        s = db.session.get(AcademicSession, int(d['id']))
        s.name = d['name']; s.start_date = d['start_date']; s.end_date = d['end_date']; s.status = d['status']
    db.session.commit()
    return jsonify({"msg": "OK"})

@app.route('/api/courses', methods=['GET', 'POST', 'PUT', 'DELETE'])
@login_required
def manage_courses():
    if current_user.role not in ['admin', 'teacher']: return jsonify({"msg": "Denied"}), 403
    if request.method == 'GET': return jsonify([c.to_dict() for c in Course.query.all()])
    
    if request.method == 'DELETE':
        db.session.delete(db.session.get(Course, int(request.args.get('id'))))
        db.session.commit()
        return jsonify({"msg": "Deleted"})

    d = request.json
    if request.method == 'POST':
        db.session.add(Course(name=d['name'], subjects=d['subjects'], teacher_id=d.get('teacher_id')))
    elif request.method == 'PUT':
        c = db.session.get(Course, int(d['id']))
        c.name = d['name']; c.subjects = d['subjects']; c.teacher_id = d.get('teacher_id')
    db.session.commit()
    return jsonify({"msg": "OK"})

@app.route('/api/fee_structures', methods=['GET', 'POST', 'PUT', 'DELETE'])
@login_required
def manage_fees():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    if request.method == 'GET': return jsonify([f.to_dict() for f in FeeStructure.query.all()])
    
    if request.method == 'DELETE':
        db.session.delete(db.session.get(FeeStructure, int(request.args.get('id'))))
        db.session.commit()
        return jsonify({"msg": "Deleted"})

    d = request.json
    dt = datetime.strptime(d['due_date'], '%Y-%m-%d').date()
    cid = int(d['course_id']) if d.get('course_id') else None
    
    if request.method == 'POST':
        db.session.add(FeeStructure(name=d['name'], academic_session_id=d['academic_session_id'], course_id=cid, total_amount=d['total_amount'], due_date=dt))
    elif request.method == 'PUT':
        f = db.session.get(FeeStructure, int(d['id']))
        f.name = d['name']; f.academic_session_id = d['academic_session_id']; f.course_id = cid
        f.total_amount = d['total_amount']; f.due_date = dt
    db.session.commit()
    return jsonify({"msg": "OK"})

@app.route('/api/teacher/announcements', methods=['GET', 'POST', 'DELETE'])
@login_required
def manage_teacher_announcements():
    if current_user.role not in ['admin', 'teacher']: return jsonify({"msg": "Denied"}), 403
    if request.method == 'POST':
        d = request.json
        new_ann = Announcement(title=d['title'], content=d['content'], category=d['category'], target_group=d['target_group'], teacher_id=current_user.id)
        db.session.add(new_ann); db.session.commit()
        
        target = d['target_group'].lower()
        users = User.query.all() if target == 'all' else User.query.filter_by(role=target.rstrip('s')).all()
        for u in users: send_push_notification(u.id, f"[{d['category']}] {d['title']}", d['content'][:100])
        return jsonify(new_ann.to_dict()), 201

    if request.method == 'DELETE':
        ann = db.session.get(Announcement, int(request.args.get('id')))
        if ann and (ann.teacher_id == current_user.id or current_user.role == 'admin'):
            db.session.delete(ann); db.session.commit()
            return jsonify({"msg": "Deleted"})
        return jsonify({"msg": "Denied"}), 403

    anns = Announcement.query.filter_by(teacher_id=current_user.id).order_by(Announcement.created_at.desc()).all()
    return jsonify([a.to_dict() for a in anns])

@app.route('/api/teacher/courses', methods=['GET'])
@login_required
def t_courses():
    return jsonify([c.to_dict() for c in Course.query.filter_by(teacher_id=current_user.id).all()])

@app.route('/api/teacher/students', methods=['GET'])
@login_required
def t_students():
    t_c_ids = [c.id for c in Course.query.filter_by(teacher_id=current_user.id).all()]
    s = User.query.join(student_course_association).join(Course).filter(User.role=='student', Course.id.in_(t_c_ids)).distinct().all()
    return jsonify([x.to_dict() for x in s])

@app.route('/api/teacher/upload_note', methods=['POST'])
@login_required
def t_upload():
    f = request.files['file']
    fn = secure_filename(f.filename)
    uid = f"{uuid.uuid4()}{os.path.splitext(fn)[1]}"
    f.save(os.path.join(app.config['UPLOAD_FOLDER'], uid))
    n = SharedNote(filename=uid, original_filename=fn, title=request.form['title'], description=request.form['description'], course_id=int(request.form['course_id']), teacher_id=current_user.id)
    db.session.add(n); db.session.commit()
    for s in db.session.get(Course, int(request.form['course_id'])).students:
        send_push_notification(s.id, "New Note", f"Uploaded: {request.form['title']}")
    return jsonify({"msg": "Uploaded"})

@app.route('/api/teacher/notes', methods=['GET', 'DELETE'])
@login_required
def t_notes():
    if request.method == 'DELETE':
        n = db.session.get(SharedNote, int(request.args.get('id')))
        if n and n.teacher_id == current_user.id:
            db.session.delete(n); db.session.commit()
            return jsonify({"msg": "Deleted"})
        return jsonify({"msg": "Denied"}), 403
    return jsonify([x.to_dict() for x in SharedNote.query.filter_by(teacher_id=current_user.id).all()])

@app.route('/api/teacher/attendance', methods=['POST'])
@login_required
def t_attendance():
    d = request.json
    dt = datetime.strptime(d['date'], '%Y-%m-%d').date()
    for r in d['attendance_data']:
        ex = Attendance.query.filter_by(student_id=r['student_id']).filter(db.func.date(Attendance.check_in_time)==dt).first()
        if ex: ex.status = r['status']
        else: db.session.add(Attendance(student_id=r['student_id'], check_in_time=datetime.combine(dt, datetime.min.time()), status=r['status']))
        if r['status'] == 'Absent': send_push_notification(r['student_id'], "Absent Alert", f"Marked absent on {d['date']}")
    db.session.commit()
    return jsonify({"message": "Attendance Saved"})

@app.route('/api/teacher/notify', methods=['POST'])
@login_required
def t_notify():
    d = request.json
    send_push_notification(d['student_id'], d['subject'], d['body'])
    if d['type'] == 'sms': send_actual_sms(db.session.get(User, d['student_id']).phone_number, d['body'])
    return jsonify({"message": "Sent"})

# --- STUDENT ROUTES ---
@app.route('/api/student/fees', methods=['GET'])
@login_required
def s_fees(): return jsonify(calculate_fee_status(current_user.id))

@app.route('/api/student/attendance', methods=['GET'])
@login_required
def s_att():
    a = Attendance.query.filter_by(student_id=current_user.id).order_by(Attendance.check_in_time.desc()).limit(10).all()
    return jsonify([{"date": x.check_in_time.strftime('%Y-%m-%d'), "status": x.status} for x in a])

@app.route('/api/student/notes', methods=['GET'])
@login_required
def s_notes():
    c_ids = [c.id for c in current_user.courses_enrolled]
    return jsonify([x.to_dict() for x in SharedNote.query.filter(SharedNote.course_id.in_(c_ids)).all()])

# --- PARENT ROUTES ---
@app.route('/api/parent/children', methods=['GET'])
@login_required
def p_children():
    return jsonify([x.to_dict() for x in User.query.filter_by(parent_id=current_user.id).all()])

@app.route('/api/parent/child_data/<int:id>', methods=['GET'])
@login_required
def p_data(id):
    if db.session.get(User, id).parent_id != current_user.id: return jsonify({"msg": "Denied"}), 403
    return jsonify({"fees": calculate_fee_status(id), "attendance": [{"date": x.check_in_time.strftime('%Y-%m-%d'), "status": x.status} for x in Attendance.query.filter_by(student_id=id).limit(5).all()]})

# --- REPORTS & RECEIPTS ---
@app.route('/api/fee_status', methods=['GET'])
@login_required
def fee_status():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    # FIX: Explicitly include 'student_id': s.id so the frontend button works
    return jsonify([{
        "student_id": s.id, 
        "student_name": s.name, 
        **calculate_fee_status(s.id)
    } for s in User.query.filter_by(role='student').all()])

@app.route('/api/receipt/<int:id>')
def serve_receipt(id):
    p = db.session.get(Payment, id)
    return f"<h1>Receipt #{p.id}</h1><p>Amount: {p.amount_paid}</p><button onclick='window.print()'>Print</button>" if p else "Not Found"

@app.route('/uploads/<filename>')
def serve_file(filename): return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# --- MISSING ADMIN ROUTES FIX ---
# --- 1. FIX FOR PROBLEM 3 (ID CARDS) ---
@app.route('/admin/id_card/<int:id>')
@login_required
def generate_single_id_card(id):
    if current_user.role != 'admin': return "Denied", 403
    u = db.session.get(User, id)
    # Simple HTML ID Card Template
    html = f"""
    <div style="border:2px solid #333; width:300px; padding:20px; text-align:center; font-family:sans-serif; margin:20px;">
        <h2 style="margin:0; color:#1173d4;">CST INSTITUTE</h2>
        <p>Identity Card</p>
        <img src="{u.profile_photo_url or 'https://placehold.co/100'}" style="width:100px;height:100px;border-radius:50%;object-fit:cover;">
        <h3>{u.name}</h3>
        <p><b>Adm No:</b> {u.admission_number or 'N/A'}</p>
        <p><b>DOB:</b> {u.dob or 'N/A'}</p>
        <p><b>Course:</b> {', '.join([c.name for c in u.courses_enrolled])}</p>
        <div style="margin-top:10px; font-size:12px;">Authorized Signatory</div>
    </div>
    <button onclick="window.print()">Print</button>
    """
    return html

@app.route('/admin/id_cards/bulk')
@login_required
def generate_bulk_id_cards():
    if current_user.role != 'admin': return "Denied", 403
    users = User.query.filter_by(role='student').all()
    cards = ""
    for u in users:
        cards += f"""
        <div style="border:1px solid #ccc; width:45%; display:inline-block; margin:10px; padding:10px; page-break-inside:avoid;">
            <div style="text-align:center;">
                <h3 style="margin:0;">CST INSTITUTE</h3>
                <img src="{u.profile_photo_url or 'https://placehold.co/80'}" style="width:80px;height:80px;border-radius:50%;">
                <div><b>{u.name}</b> (Adm: {u.admission_number})</div>
            </div>
        </div>
        """
    return f"<html><body>{cards}<br><button onclick='window.print()'>Print All</button></body></html>"


@app.route('/api/announcements', methods=['GET', 'POST', 'DELETE'])
@login_required
def admin_announcements():
    # Helper route for Admin to manage announcements
    if current_user.role not in ['admin', 'teacher']: return jsonify({"msg": "Denied"}), 403
    
    if request.method == 'POST':
        d = request.json
        a = Announcement(title=d['title'], content=d['content'], category=d.get('category', 'General'), target_group=d['target_group'])
        db.session.add(a)
        db.session.commit()
        # Notification logic here...
        return jsonify(a.to_dict()), 201

    if request.method == 'DELETE':
        # Logic to delete
        a = db.session.get(Announcement, int(request.args.get('id')))
        if a: db.session.delete(a); db.session.commit()
        return jsonify({"msg": "Deleted"})

    # GET
    return jsonify([a.to_dict() for a in Announcement.query.order_by(Announcement.created_at.desc()).all()])

@app.route('/api/parents', methods=['GET'])
@login_required
def get_all_parents():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    parents = User.query.filter_by(role='parent').all()
    return jsonify([p.to_dict() for p in parents])

@app.route('/api/send_sms_alert', methods=['POST'])
@login_required
def send_sms_alert():
    if current_user.role != 'admin': 
        return jsonify({"msg": "Denied"}), 403
    
    try:
        d = request.json
        student_id = d.get('student_id')
        
        if not student_id:
            return jsonify({"message": "Error: Student ID Missing"}), 400

        u = db.session.get(User, student_id)
        if not u:
            return jsonify({"message": "Error: Student Not Found"}), 404
            
        if not u.phone_number:
            return jsonify({"message": f"Error: No phone for {u.name}"}), 404
        
        # 1. Calculate Fees
        fee_data = calculate_fee_status(student_id)
        pending_amount = fee_data.get('balance', 0)
        due_date = fee_data.get('due_date', 'N/A')
        
        # 2. Prepare Template Variables
        var1 = u.name
        var2 = str(int(pending_amount)) if pending_amount else "0"
        var3 = str(due_date)
        var4 = "7083021167" 
        
        # 3. Construct Message
        message_body = f"Dear {var1}, your fee of Rs {var2} is pending. Due: {var3}. CST Institute {var4}"
        
        # 4. Send
        template_id = "1707176388002841408"
        print(f"Sending SMS to {u.phone_number}: {message_body}") # Log to console
        
        success = send_actual_sms(u.phone_number, message_body, template_id=template_id)
        
        if success:
            return jsonify({"message": "SMS Sent Successfully"})
        else:
            return jsonify({"message": "SMS API returned failure"}), 500

    except Exception as e:
        # This catches the crash and shows it in your browser console!
        print(f"SMS CRASH: {str(e)}")
        return jsonify({"message": f"Server Error: {str(e)}"}), 500
# =========================================================
# MIGRATION & STARTUP
# =========================================================

def check_and_upgrade_db():
    try:
        insp = inspect(db.engine)
        with db.engine.connect() as conn:
            user_cols = [c['name'] for c in insp.get_columns('user')]
            if 'admission_number' not in user_cols: conn.execute(text("ALTER TABLE user ADD COLUMN admission_number VARCHAR(50)"))
            if 'session_id' not in user_cols: conn.execute(text("ALTER TABLE user ADD COLUMN session_id INTEGER"))
            if 'fcm_token' not in user_cols: conn.execute(text("ALTER TABLE user ADD COLUMN fcm_token VARCHAR(500)"))
            
            ann_cols = [c['name'] for c in insp.get_columns('announcement')]
            if 'category' not in ann_cols: conn.execute(text("ALTER TABLE announcement ADD COLUMN category VARCHAR(50) DEFAULT 'General'"))
            if 'teacher_id' not in ann_cols: conn.execute(text("ALTER TABLE announcement ADD COLUMN teacher_id INTEGER"))
            
            fee_cols = [c['name'] for c in insp.get_columns('fee_structure')]
            if 'course_id' not in fee_cols: conn.execute(text("ALTER TABLE fee_structure ADD COLUMN course_id INTEGER"))
            conn.commit()
        print("Migration successful.")
    except Exception as e: print(f"Migration Error: {e}")

# --- ADD THIS FUNCTION BACK TO FIX IMPORT ERROR ---
def initialize_database():
    with app.app_context():
        db.create_all()
        check_and_upgrade_db()
        init_firebase()

# Ensure tables exist when running via Gunicorn
with app.app_context():
    db.create_all()
    check_and_upgrade_db()

if __name__ == '__main__':
    initialize_database()
    app.run(debug=True, host='0.0.0.0', port=5000)