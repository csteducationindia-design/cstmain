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

class SharedNote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(300)); original_filename = db.Column(db.String(300))
    title = db.Column(db.String(150)); description = db.Column(db.Text)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'))
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    course = db.relationship('Course', backref=db.backref('notes', lazy=True))
    def to_dict(self): return {"id": self.id, "title": self.title, "course_name": self.course.name if self.course else "N/A", "filename": self.filename, "created_at": self.created_at.strftime('%Y-%m-%d')}

# =========================================================
# HELPER FUNCTIONS
# =========================================================
def send_whatsapp_message(phone, msg):
    # This acts as a placeholder. In production, paste your WhatsApp API code here.
    # For now, it logs to the console so you can verify it works.
    logger.info(f"WHATSAPP SENT TO {phone}: {msg}")
    return True

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
            "due_date": final_due_date.strftime('%d-%b-%Y'),
            "pending_days": pending_days
        }
    except Exception as e:
        print(f"Fee Calculation Error: {e}")
        return {"balance": 0, "due_date": "N/A"}

# =========================================================
# ROUTES
# =========================================================

@app.route('/')
def serve_login_page():
    if current_user.is_authenticated: 
        return redirect(f"/{current_user.role}")
    return render_template('login.html')

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
    return jsonify({"message": "OK"})

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
    # Security: Ensure role matches
    if role != current_user.role:
        return redirect(f"/{current_user.role}")
    if role in ['admin', 'teacher', 'student', 'parent']:
        return render_template(f'{role}.html')
    return redirect('/')

@app.route('/firebase-messaging-sw.js')
def sw(): return send_from_directory(app.static_folder, 'firebase-messaging-sw.js')

# --- ADMIN ROUTES ---
@app.route('/api/users', methods=['GET', 'POST', 'PUT'])
@login_required
def api_users():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    
    if request.method == 'GET':
        search = request.args.get('search', '').lower()
        session_id = request.args.get('session_id')
        
        q = User.query
        
        if session_id and session_id != 'null' and str(session_id).isdigit():
             q = q.filter_by(session_id=int(session_id))
             
        if search: 
            q = q.filter(or_(User.name.ilike(f'%{search}%'), User.email.ilike(f'%{search}%')))
            
        return jsonify([u.to_dict() for u in q.all()])

    d = request.form
    # Check if ID exists for Update vs Insert
    if request.method == 'POST' and not d.get('id'):
        if User.query.filter_by(email=d['email']).first(): return jsonify({"msg": "Email exists"}), 400
        u = User(name=d['name'], email=d['email'], password=bcrypt.generate_password_hash(d['password']).decode('utf-8'), role=d.get('role', 'student'))
        db.session.add(u)
    else: # Update existing (POST with ID or PUT)
        u_id = d.get('id')
        if not u_id: return jsonify({"msg": "Missing ID for update"}), 400
        u = db.session.get(User, int(u_id))
        if not u: return jsonify({"msg": "Not found"}), 404
        if d.get('password'): u.password = bcrypt.generate_password_hash(d['password']).decode('utf-8')

    u.name = d['name']; u.email = d['email']; u.phone_number = d.get('phone_number')
    u.admission_number = d.get('admission_number')
    u.dob = d.get('dob'); u.gender = d.get('gender')
    u.father_name = d.get('father_name'); u.mother_name = d.get('mother_name')
    u.address_line1 = d.get('address_line1'); u.city = d.get('city'); u.state = d.get('state'); u.pincode = d.get('pincode')
    
    if d.get('session_id') and str(d.get('session_id')).isdigit():
        u.session_id = int(d.get('session_id'))
    
    if d.get('parent_id') and str(d.get('parent_id')).isdigit():
        u.parent_id = int(d.get('parent_id'))

    if 'profile_photo_file' in request.files:
        file = request.files['profile_photo_file']
        if file and allowed_file(file.filename, ALLOWED_IMAGE_EXTENSIONS):
            fn = secure_filename(file.filename)
            uid = f"{uuid.uuid4()}{os.path.splitext(fn)[1]}"
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], uid))
            u.profile_photo_url = f"/uploads/{uid}"

    if u.role == 'student':
        c_ids = request.form.getlist('course_ids')
        if c_ids:
            u.courses_enrolled = Course.query.filter(Course.id.in_([int(cid) for cid in c_ids if str(cid).isdigit()])).all()

    db.session.commit()
    return jsonify(u.to_dict())

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

@app.route('/api/admin/student/<int:id>', methods=['GET'])
@login_required
def get_student(id):
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    return jsonify(db.session.get(User, id).to_dict())

@app.route('/api/sessions', methods=['GET', 'POST', 'PUT', 'DELETE'])
@login_required
def manage_sessions():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    if request.method == 'GET': return jsonify([s.to_dict() for s in AcademicSession.query.all()])
    
    if request.method == 'DELETE':
        db.session.delete(db.session.get(AcademicSession, int(request.args.get('id'))))
        db.session.commit()
        return jsonify({"msg": "Deleted"})

    d = request.json
    if request.method == 'POST' and not d.get('id'):
        db.session.add(AcademicSession(name=d['name'], start_date=d['start_date'], end_date=d['end_date'], status=d['status']))
    else:
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
    if request.method == 'POST' and not d.get('id'):
        db.session.add(Course(name=d['name'], subjects=d['subjects'], teacher_id=d.get('teacher_id')))
    else:
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
    
    if request.method == 'POST' and not d.get('id'):
        db.session.add(FeeStructure(name=d['name'], academic_session_id=d['academic_session_id'], course_id=cid, total_amount=d['total_amount'], due_date=dt))
    else:
        f = db.session.get(FeeStructure, int(d['id']))
        f.name = d['name']; f.academic_session_id = d['academic_session_id']; f.course_id = cid
        f.total_amount = d['total_amount']; f.due_date = dt
    db.session.commit()
    return jsonify({"msg": "OK"})

@app.route('/api/announcements', methods=['GET', 'POST', 'DELETE'])
@login_required
def admin_announcements():
    if current_user.role not in ['admin', 'teacher']: return jsonify({"msg": "Denied"}), 403
    
    if request.method == 'POST':
        d = request.json
        a = Announcement(title=d['title'], content=d['content'], category=d.get('category', 'General'), target_group=d['target_group'])
        db.session.add(a)
        db.session.commit()
        return jsonify(a.to_dict()), 201

    if request.method == 'DELETE':
        a = db.session.get(Announcement, int(request.args.get('id')))
        if a: db.session.delete(a); db.session.commit()
        return jsonify({"msg": "Deleted"})

    return jsonify([a.to_dict() for a in Announcement.query.order_by(Announcement.created_at.desc()).all()])

@app.route('/api/parents', methods=['GET'])
@login_required
def get_all_parents():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    parents = User.query.filter_by(role='parent').all()
    return jsonify([p.to_dict() for p in parents])

@app.route('/api/fee_status', methods=['GET'])
@login_required
def fee_status():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    
    course_id = request.args.get('course_id')
    query = User.query.filter_by(role='student')
    
    if course_id and str(course_id).isdigit():
        query = query.filter(User.courses_enrolled.any(id=int(course_id)))
        
    return jsonify([{
        "student_id": s.id, 
        "student_name": s.name, 
        **calculate_fee_status(s.id)
    } for s in query.all()])

@app.route('/api/send_sms_alert', methods=['POST'])
@login_required
def send_sms_alert():
    if current_user.role != 'admin': 
        return jsonify({"msg": "Denied"}), 403
    
    try:
        d = request.json
        student_id = d.get('student_id')
        
        if not student_id: return jsonify({"message": "Error: Student ID Missing"}), 400

        u = db.session.get(User, student_id)
        if not u: return jsonify({"message": "Error: Student Not Found"}), 404
            
        if not u.phone_number: return jsonify({"message": f"Error: No phone for {u.name}"}), 404
        
        fee_data = calculate_fee_status(student_id)
        pending_amount = fee_data.get('balance', 0)
        due_date = fee_data.get('due_date', 'N/A')
        
        message_body = f"Dear {u.name}, your fee of Rs {int(pending_amount)} is pending. Due: {due_date}. CST Institute 7083021167"
        template_id = "1707176388002841408"
        
        print(f"Sending SMS to {u.phone_number}: {message_body}")
        send_actual_sms(u.phone_number, message_body, template_id=template_id)
        
        return jsonify({"message": "SMS Sent Successfully"})

    except Exception as e:
        print(f"SMS CRASH: {str(e)}")
        return jsonify({"message": f"Server Error: {str(e)}"}), 500

# --- SPECIAL ADMIN ROUTES (ID Card) ---
@app.route('/admin/id_card/<int:id>')
@login_required
def generate_single_id_card(id):
    if current_user.role != 'admin': return "Denied", 403
    u = db.session.get(User, id)
    qr_url = url_for('static', filename=f'qr_codes/qr_{u.admission_number}.png')
    
    # PROBLEM 4 FIX: Professional Layout with QR
    html = f"""
    <html>
    <head>
        <style>
            @media print {{ @page {{ margin: 0; }} body {{ margin: 1cm; }} }}
            .id-card {{
                width: 3.375in; height: 2.125in; border: 1px solid #000;
                position: relative; font-family: Arial, sans-serif;
                background: white; overflow: hidden; margin-bottom: 20px;
            }}
            .header {{ background: #1173d4; color: white; height: 40px; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 16px; }}
            .photo {{ position: absolute; left: 10px; top: 50px; width: 80px; height: 100px; border: 1px solid #ccc; object-fit: cover; }}
            .info {{ position: absolute; left: 100px; top: 50px; font-size: 12px; line-height: 1.4; }}
            .qr {{ position: absolute; right: 10px; bottom: 10px; width: 60px; height: 60px; }}
            .footer {{ position: absolute; bottom: 5px; left: 10px; font-size: 10px; font-weight: bold; color: #1173d4; }}
        </style>
    </head>
    <body>
        <div class="id-card">
            <div class="header">CST INSTITUTE</div>
            <img src="{u.profile_photo_url or 'https://placehold.co/100'}" class="photo">
            <div class="info">
                <b>Name:</b> {u.name}<br>
                <b>Adm No:</b> {u.admission_number}<br>
                <b>DOB:</b> {u.dob}<br>
                <b>Contact:</b> {u.phone_number}<br>
            </div>
            <img src="{qr_url}" class="qr" onerror="this.style.display='none'">
            <div class="footer">Authorized Signatory</div>
        </div>
        <script>window.print();</script>
    </body>
    </html>
    """
    return html

@app.route('/admin/id_cards/bulk')
@login_required
def generate_bulk_id_cards():
    if current_user.role != 'admin': return "Denied", 403
    users = User.query.filter_by(role='student').all()
    cards = ""
    for u in users:
        qr_url = url_for('static', filename=f'qr_codes/qr_{u.admission_number}.png')
        cards += f"""
        <div class="id-card" style="display: inline-block; margin: 10px;">
            <div class="header" style="background:#1173d4;color:white;text-align:center;padding:5px;font-weight:bold;">CST INSTITUTE</div>
            <div style="padding:10px; position:relative; height:130px;">
                <img src="{u.profile_photo_url or 'https://placehold.co/80'}" style="width:70px;height:90px;border:1px solid #ccc;float:left;margin-right:10px;">
                <div style="font-size:12px;line-height:1.4;">
                    <b>{u.name}</b><br>
                    Adm: {u.admission_number}<br>
                    DOB: {u.dob}<br>
                    Phone: {u.phone_number}
                </div>
                <img src="{qr_url}" style="width:50px;height:50px;position:absolute;right:0;bottom:0;" onerror="this.style.display='none'">
            </div>
        </div>
        """
    return f"<html><body style='font-family:Arial;'>{cards}<button onclick='window.print()' style='position:fixed;top:10px;right:10px;'>Print All</button></body></html>"

@app.route('/uploads/<filename>')
def serve_file(filename): return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/api/receipt/<int:id>')
def serve_receipt(id):
    p = db.session.get(Payment, id)
    return f"<h1>Receipt #{p.id}</h1><p>Amount: {p.amount_paid}</p><button onclick='window.print()'>Print</button>" if p else "Not Found"

# =========================================================
# TEACHER SPECIFIC ROUTES
# =========================================================

@app.route('/api/teacher/students', methods=['GET'])
@login_required
def teacher_students():
    session_id = request.args.get('session_id')
    course_id = request.args.get('course_id')
    
    # 1. Get courses taught by this teacher
    query = Course.query.filter_by(teacher_id=current_user.id)
    if course_id: 
        query = query.filter_by(id=int(course_id))
    courses = query.all()
    
    students_list = []
    seen_ids = set()
    
    for c in courses:
        # 2. Filter students in those courses
        # If session_id is provided, only show students from that session
        valid_students = [s for s in c.students if (not session_id or str(s.session_id) == str(session_id))]
        
        for s in valid_students:
            if s.id not in seen_ids:
                students_list.append({
                    "id": s.id,
                    "name": s.name,  # REAL NAME FROM DB
                    "admission_number": s.admission_number,
                    "profile_photo_url": s.profile_photo_url, # PHOTO URL
                    "session_name": s.to_dict().get('session_name', 'N/A'),
                    "course_name": c.name
                })
                seen_ids.add(s.id)
                
    return jsonify(students_list)

# 2. ANNOUNCEMENTS (GET & POST)
@app.route('/api/teacher/announcements', methods=['GET', 'POST'])
@login_required
def teacher_announcements():
    if current_user.role != 'teacher': return jsonify({"msg": "Denied"}), 403
    
    if request.method == 'POST':
        d = request.json
        # Save Announcement
        a = Announcement(
            title=d['title'], 
            content=d['content'], 
            category=d.get('category', 'Class Update'), 
            target_group=d.get('target_group', 'students'), 
            teacher_id=current_user.id
        )
        db.session.add(a)
        db.session.commit()
        
        # NOTIFICATION LOGIC
        courses = Course.query.filter_by(teacher_id=current_user.id).all()
        notified_ids = set()
        
        for c in courses:
            for s in c.students:
                if s.id not in notified_ids:
                    # Send Push
                    send_push_notification(s.id, f"Class Update: {d['title']}", d['content'])
                    notified_ids.add(s.id)
        
        return jsonify(a.to_dict()), 201

    # GET
    anns = Announcement.query.filter(
        (Announcement.teacher_id == current_user.id) | (Announcement.target_group == 'teachers')
    ).order_by(Announcement.created_at.desc()).all()
    return jsonify([a.to_dict() for a in anns])


# 3. NOTES (Sharing Material)
@app.route('/api/teacher/notes', methods=['GET', 'POST'])
@login_required
def teacher_notes():
    if current_user.role != 'teacher': return jsonify({"msg": "Denied"}), 403

    if request.method == 'POST':
        if 'file' not in request.files: return jsonify({"msg": "No file"}), 400
        f = request.files['file']
        if f.filename == '': return jsonify({"msg": "No file selected"}), 400
        
        fn = secure_filename(f.filename)
        uid = f"{uuid.uuid4()}{os.path.splitext(fn)[1]}"
        f.save(os.path.join(app.config['UPLOAD_FOLDER'], uid))
        
        note = SharedNote(
            title=request.form['title'],
            description=request.form.get('description', ''),
            filename=uid,
            original_filename=fn,
            course_id=int(request.form['course_id']),
            teacher_id=current_user.id
        )
        db.session.add(note)
        db.session.commit()
        
        # Notify Students
        course = db.session.get(Course, int(request.form['course_id']))
        for s in course.students:
            send_push_notification(s.id, "New Study Material", f"{current_user.name} posted: {request.form['title']}")
            
        return jsonify({"msg": "Uploaded Successfully"}), 201

    # GET
    notes = SharedNote.query.filter_by(teacher_id=current_user.id).order_by(SharedNote.created_at.desc()).all()
    return jsonify([n.to_dict() for n in notes])

# 4. DAILY TOPIC / SYLLABUS UPDATE (New Feature)
@app.route('/api/teacher/daily_topic', methods=['POST'])
@login_required
def daily_topic():
    d = request.json
    c = db.session.get(Course, d['course_id'])
    
    # Save Record
    db.session.add(Announcement(title=f"Topic: {c.name}", content=d['topic'], category="Syllabus", target_group="students", teacher_id=current_user.id))
    db.session.commit()
    
    # Notify Parents
    for s in c.students:
        send_push_notification(s.id, f"Daily Topic: {c.name}", d['topic'])
        
        if s.parent_id:
            parent = db.session.get(User, s.parent_id)
            if parent and parent.phone_number:
                send_whatsapp_message(parent.phone_number, f"Today in {c.name}, we taught: {d['topic']}")
                
    return jsonify({"msg": "Syllabus Sent to Parents"})

# 5. ATTENDANCE REPORT
@app.route('/api/teacher/reports/attendance', methods=['GET'])
@login_required
def teacher_att_report():
    if current_user.role != 'teacher': return jsonify({"msg": "Denied"}), 403
    
    date_filter = request.args.get('date', date.today().strftime('%Y-%m-%d'))
    session_id = request.args.get('session_id')
    
    # Get attendance records for the date
    atts = Attendance.query.filter(db.func.date(Attendance.check_in_time) == date_filter).all()
    
    report = []
    for a in atts:
        # Fetch the REAL User to get Name and Photo
        u = db.session.get(User, a.student_id)
        
        if u:
            # Apply Batch Filter if selected
            if session_id and str(u.session_id) != str(session_id):
                continue
                
            report.append({
                "student_name": u.name,  # REAL NAME
                "photo_url": u.profile_photo_url, # REAL PHOTO
                "admission_number": u.admission_number,
                "status": a.status,
                "date": a.check_in_time.strftime('%Y-%m-%d')
            })
            
    return jsonify(report)

# 6. GET TEACHER COURSES
@app.route('/api/teacher/courses', methods=['GET'])
@login_required
def t_courses(): 
    if current_user.role != 'teacher': return jsonify({"msg": "Denied"}), 403
    return jsonify([c.to_dict() for c in Course.query.filter_by(teacher_id=current_user.id).all()])

@app.route('/api/teacher/attendance', methods=['POST'])
@login_required
def save_attendance():
    d = request.json
    dt = datetime.strptime(d['date'], '%Y-%m-%d')
    
    for r in d['attendance_data']:
        sid = r['student_id']
        stat = r['status']
        
        # Save to DB
        exist = Attendance.query.filter(Attendance.student_id==sid, db.func.date(Attendance.check_in_time)==dt.date()).first()
        if exist: 
            exist.status = stat
        else: 
            db.session.add(Attendance(student_id=sid, check_in_time=dt, status=stat))
        
        # NOTIFICATION LOGIC (New)
        if stat == 'Absent':
            # 1. Notify Student App
            send_push_notification(sid, "Attendance Alert", f"You were marked ABSENT on {d['date']}")
            
            # 2. Notify Parent (App + WhatsApp)
            student = db.session.get(User, sid)
            if student.parent_id:
                # App Push
                send_push_notification(student.parent_id, "Absent Alert", f"Your child {student.name} is marked ABSENT today.")
                
                # WhatsApp/SMS
                parent = db.session.get(User, student.parent_id)
                if parent and parent.phone_number:
                    msg = f"Alert: Your child {student.name} is marked ABSENT on {d['date']}. Please contact CST Institute."
                    send_whatsapp_message(parent.phone_number, msg)
            
    db.session.commit()
    return jsonify({"msg": "Attendance Saved & Parents Notified"})

# --- STUDENT/PARENT ROUTES ---
@app.route('/api/student/fees', methods=['GET'])
@login_required
def s_fees(): return jsonify(calculate_fee_status(current_user.id))

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

def initialize_database():
    with app.app_context():
        db.create_all()
        check_and_upgrade_db()
        init_firebase()

if __name__ == '__main__':
    initialize_database()
    app.run(debug=True, host='0.0.0.0', port=5000)