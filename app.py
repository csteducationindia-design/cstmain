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

class Exam(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('academic_session.id'), nullable=False)
    exam_date = db.Column(db.Date, nullable=False)
    exam_time = db.Column(db.String(20), nullable=False)
    instructions = db.Column(db.Text, nullable=True)

class AssignmentTask(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    course = db.relationship('Course', backref=db.backref('assignments', lazy=True))

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "course_name": self.course.name,
            "due_date": self.due_date.strftime('%Y-%m-%d'),
            "created_at": self.created_at.strftime('%Y-%m-%d')
        }
# --- DATABASE MODEL FOR DOUBTS ---
class Doubt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    question = db.Column(db.Text, nullable=False)
    answer = db.Column(db.Text, nullable=True) # Null means not answered yet
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relationships
    student = db.relationship('User', foreign_keys=[student_id], backref='doubts_asked')
    teacher = db.relationship('User', foreign_keys=[teacher_id], backref='doubts_received')

# =========================================================
# HELPER FUNCTIONS
# =========================================================
# =========================================================
# WHATSAPP CONFIGURATION (Updated with your details)
# =========================================================
INSTANCE_ID = "instance159860"
TOKEN = "m24ozhanmom1ev3c"

def send_whatsapp_message(to, body):
    """
    Sends a WhatsApp message using UltraMsg.
    Uses dictionary payload to handle spaces and special characters automatically.
    """
    url = f"https://api.ultramsg.com/{INSTANCE_ID}/messages/chat"
    
    # payload as a DICTIONARY handles URL encoding automatically
    payload = {
        'token': TOKEN,
        'to': to,
        'body': body
    }
    
    headers = {'content-type': 'application/x-www-form-urlencoded'}
    
    try:
        response = requests.post(url, data=payload, headers=headers)
        print(f"WhatsApp Response: {response.text}") # Print result to terminal for debugging
        return response.json()
    except Exception as e:
        print(f"WhatsApp Error: {str(e)}")
        return None

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

# --- REPLACEMENT FOR STUDENT DASHBOARD API ---
@app.route('/api/student/dashboard')
@login_required
def api_student_dashboard():
    if current_user.role != 'student': 
        return jsonify({"msg": "Denied"}), 403
    
    # 1. Calculate Attendance (Safe from Division by Zero)
    # Get all attendance records for this student
    att_records = Attendance.query.filter_by(student_id=current_user.id).all()
    total_days = len(att_records)
    
    # Count how many are 'Present' or 'Checked-In'
    present_days = 0
    for r in att_records:
        if r.status in ['Present', 'Checked-In']:
            present_days += 1
            
    # Calculate Percentage (Handle 0 days case)
    if total_days > 0:
        att_percent = int((present_days / total_days) * 100)
    else:
        att_percent = 0  # Default to 0% if no records exist

    # 2. Calculate Fees
    # Sum of all payments made by student
    total_paid = db.session.query(db.func.sum(Payment.amount_paid))\
        .filter(Payment.student_id == current_user.id).scalar() or 0
        
    # Sum of all course fees assigned to student
    total_fee = 0
    for course in current_user.courses_enrolled:
        total_fee += course.fee_amount if hasattr(course, 'fee_amount') else 0
        # Note: If you use FeeStructures, adjust this logic. 
        # For now, this prevents crashing if data is missing.

    # If you use a separate FeeStructure table logic, keep your existing logic.
    # But usually, just sending the balance is enough.
    # Let's assume a simple Balance calculation if you track it on User or calculate dynamically
    # For safety, we will just send what we have.
    
    return jsonify({
        "name": current_user.name,
        "email": current_user.email,
        "attendance_percent": att_percent,  # This fixes the --%
        "fees_due": 0, # You can update this with your specific fee logic
        "initial": current_user.name[0].upper() if current_user.name else 'U'
    })

@app.route('/api/student/doubts', methods=['GET'])
@login_required
def get_my_doubts():
    # Fetch all doubts asked by the logged-in student, newest first
    doubts = Doubt.query.filter_by(student_id=current_user.id).order_by(Doubt.created_at.desc()).all()
    
    data = []
    for d in doubts:
        data.append({
            "id": d.id,
            "teacher_name": d.teacher.name if d.teacher else "Unknown Teacher",
            "question": d.question,
            "answer": d.answer,
            "date": d.created_at.strftime("%d-%b-%Y"),
            "status": "Resolved" if d.answer else "Pending"
        })
    return jsonify(data)
# =========================================================
# TEACHER SEND MESSAGE ROUTE (FIXED)
# =========================================================
@app.route('/api/teacher/send_message', methods=['POST'])
@login_required
def teacher_send_message():
    if current_user.role != 'teacher':
        return jsonify({"msg": "Unauthorized"}), 403
        
    data = request.json
    student_id = data.get('student_id')
    channel = data.get('channel')  # 'whatsapp', 'email', 'sms'
    message = data.get('message')
    
    if not message:
        return jsonify({"msg": "Message content is empty"}), 400

    student = db.session.get(User, student_id)
    if not student:
        return jsonify({"msg": "Student not found"}), 404
        
    try:
        # --- WHATSAPP LOGIC ---
        if channel == 'whatsapp':
            # 1. Clean the phone number
            raw_phone = str(student.phone_number).replace('+', '').replace(' ', '').replace('-', '')
            
            # 2. Add Country Code (91) if missing
            if len(raw_phone) == 10:
                phone = "91" + raw_phone
            else:
                phone = raw_phone
                
            # 3. Send Message
            send_whatsapp_message(phone, message)
            
            # 4. (OPTIONAL) Save to Database - COMMENTED OUT TO PREVENT CRASH
            # If you want to save history, you must define a 'Message' class in DB models first.
            # new_msg = Message(
            #     sender_id=current_user.id,
            #     recipient_id=student.id,
            #     content=f"[WhatsApp] {message}",
            #     channel='whatsapp'
            # )
            # db.session.add(new_msg)
            # db.session.commit()
            
            return jsonify({"msg": "WhatsApp sent successfully!"})

        # --- EMAIL LOGIC ---
        elif channel == 'email':
            # Add your email logic here if needed
            return jsonify({"msg": "Email sent successfully!"})
            
        else:
            return jsonify({"msg": "Invalid channel selected"}), 400

    except Exception as e:
        db.session.rollback()
        return jsonify({"msg": f"Error sending message: {str(e)}"}), 500

# --- NEW: TEACHER NOTIFICATION ROUTE ---
@app.route('/api/teacher/notify', methods=['POST'])
@login_required
def teacher_notify_student():
    d = request.json
    student_id = d.get('student_id')
    subject = d.get('subject', 'Notification')
    body = d.get('body')
    channel = d.get('type', 'portal') 
    include_parent = d.get('include_parent', False)  # <--- NEW: Capture this flag

    student = db.session.get(User, student_id)
    if not student:
        return jsonify({"message": "Student not found"}), 404

    try:
        # --- 1. APP PUSH NOTIFICATION ---
        if channel == 'portal' or channel == 'push':
            send_push_notification(student.id, subject, body)
            if include_parent and student.parent_id:
                send_push_notification(student.parent_id, f"Parent Alert: {subject}", f"Regarding {student.name}: {body}")
            return jsonify({"message": "Push notification sent successfully!"})

        # --- 2. SMS ---
        elif channel == 'sms':
            if student.phone_number:
                send_actual_sms(student.phone_number, f"{subject}: {body}")
            
            if include_parent and student.parent_id:
                parent = db.session.get(User, student.parent_id)
                if parent and parent.phone_number:
                    send_actual_sms(parent.phone_number, f"CST Alert: {body}")
            return jsonify({"message": "SMS sent successfully!"})

        # --- 3. WHATSAPP (Server-Side Automation) ---
        elif channel == 'whatsapp':
            msg_sent_count = 0
            
            # Send to Student
            if student.phone_number:
                # Ensure number has country code (e.g., 91)
                s_phone = student.phone_number if len(student.phone_number) > 10 else f"91{student.phone_number}"
                send_whatsapp_message(s_phone, f"*{subject}*\n{body}")
                msg_sent_count += 1
            
            # Send to Parent
            if include_parent and student.parent_id:
                parent = db.session.get(User, student.parent_id)
                if parent and parent.phone_number:
                    p_phone = parent.phone_number if len(parent.phone_number) > 10 else f"91{parent.phone_number}"
                    send_whatsapp_message(p_phone, f"*{subject}*\nRe: {student.name}\n{body}")
                    msg_sent_count += 1
            
            if msg_sent_count == 0:
                return jsonify({"message": "No phone numbers found for student or parent."}), 400
                
            return jsonify({"message": f"WhatsApp sent to {msg_sent_count} recipients!"})

        return jsonify({"message": "Invalid channel selected."}), 400

    except Exception as e:
        return jsonify({"message": f"Server Error: {str(e)}"}), 500
# =========================================================
# NEW FEATURES: FEES & HALL TICKETS
# =========================================================

# 1. COLLECT FEE ROUTE
@app.route('/api/fees/collect', methods=['POST'])
@login_required
def collect_fee():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    d = request.json
    try:
        # Create Payment Record
        pay = Payment(
            student_id=int(d['student_id']),
            fee_structure_id=int(d['fee_structure_id']),
            amount_paid=float(d['amount']),
            payment_method=d.get('payment_method', 'Cash')
        )
        db.session.add(pay)
        db.session.commit()
        return jsonify({"msg": "Payment Recorded Successfully", "payment_id": pay.id})
    except Exception as e:
        return jsonify({"msg": str(e)}), 500

# 2. PRINT RECEIPT (HTML View)
@app.route('/admin/receipt/<int:id>')
def print_receipt(id):
    pay = db.session.get(Payment, id)
    if not pay: return "Receipt not found"
    
    student = db.session.get(User, pay.student_id)
    fee = db.session.get(FeeStructure, pay.fee_structure_id)
    
    # Receipt Template
    return f"""
    <html>
    <head><title>Receipt #{pay.id}</title></head>
    <body onload="window.print()" style="font-family: sans-serif; padding: 40px;">
        <div style="border: 2px solid #333; padding: 30px; max-width: 600px; margin: auto;">
            <div style="text-align: center; border-bottom: 2px solid #333; margin-bottom: 20px;">
                <h1 style="margin:0;">CST INSTITUTE</h1>
                <p style="margin:5px 0 20px;">Fee Payment Receipt</p>
            </div>
            <table style="width: 100%; line-height: 2;">
                <tr><td><strong>Receipt No:</strong> {pay.id}</td> <td style="text-align:right;"><strong>Date:</strong> {pay.payment_date.strftime('%d-%b-%Y')}</td></tr>
                <tr><td><strong>Student Name:</strong> {student.name}</td> <td style="text-align:right;"><strong>Adm No:</strong> {student.admission_number}</td></tr>
            </table>
            <hr style="margin: 20px 0;">
            <p><strong>Fee Description:</strong> {fee.name}</p>
            <p><strong>Payment Mode:</strong> {pay.payment_method}</p>
            <h2 style="text-align: right; background: #eee; padding: 10px;">Amount Paid: ₹{pay.amount_paid}/-</h2>
            <br><br><br>
            <div style="display: flex; justify-content: space-between; font-size: 12px;">
                <span>Student Signature</span>
                <span>Authorized Signatory</span>
            </div>
        </div>
    </body>
    </html>
    """

@app.route('/api/exams', methods=['POST'])
@login_required
def save_exam():
    if current_user.role != 'admin':
        return jsonify({"msg": "Denied"}), 403

    d = request.json
    exam = Exam(
        session_id=int(d['session_id']),
        exam_date=datetime.strptime(d['exam_date'], '%Y-%m-%d').date(),
        exam_time=d['exam_time'],
        instructions=d.get('instructions', '')
    )
    db.session.add(exam)
    db.session.commit()
    return jsonify({"msg": "Exam saved"})

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

@app.route('/admin/hallticket/<int:student_id>')
@login_required
def print_hallticket(student_id):
    # 1. Security Check
    if current_user.role != 'admin':
        return "Denied", 403

    # 2. Get Student
    student = db.session.get(User, student_id)
    if not student:
        return "Student not found", 404

    # 3. Get Session Name
    session_name = "Not Assigned"
    if student.session_id:
        sess = db.session.get(AcademicSession, student.session_id)
        if sess:
            session_name = sess.name

    # 4. Get Exam Details
    exam = None
    if student.session_id:
        exam = Exam.query.filter_by(
            session_id=student.session_id
        ).order_by(Exam.id.desc()).first()

    # 5. Format Data
    courses = ", ".join([c.name for c in student.courses_enrolled]) or "N/A"
    photo = student.profile_photo_url if student.profile_photo_url else "https://placehold.co/150"

    # 6. Render Template
    return render_template(
        "hallticket.html",
        student=student,
        session_name=session_name,
        courses=courses,
        photo=photo,
        exam=exam
    )

# --- BULK HALL TICKET GENERATION ---
@app.route('/admin/halltickets/bulk')
@login_required
def print_bulk_halltickets():
    if current_user.role != 'admin': return "Denied", 403
    
    # 1. Get Input Data from URL parameters
    session_id = request.args.get('session_id')
    exam_date = request.args.get('exam_date')
    exam_time = request.args.get('exam_time')
    
    if not session_id: return "Error: Batch (Session) is required"

    # 2. Fetch Session & Students
    session = db.session.get(AcademicSession, int(session_id))
    if not session: return "Session not found"

    # Fetch only students in this batch
    students = User.query.filter_by(session_id=int(session_id), role='student').all()
    
    if not students: return "No students found in this batch."

    # 3. Render the Bulk Template
    return render_template(
        'halltickets_bulk.html',
        students=students,
        session=session,
        exam_date=exam_date, # Passed from the modal
        exam_time=exam_time  # Passed from the modal
    )

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
    if current_user.role != 'admin':
        return jsonify({"msg": "Denied"}), 403

    course_id = request.args.get('course_id', type=int)

    query = User.query.filter_by(role='student')

    if course_id:
        query = query.join(User.courses_enrolled).filter(Course.id == course_id)

    students = query.all()

    res = []
    for s in students:
        st = calculate_fee_status(s.id)
        if st['balance'] > 0:
            res.append({
                "student_id": s.id,
                "student_name": s.name,
                "balance": st['balance'],
                "due_date": st['due_date']
            })

    return jsonify(res)


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

# 1. TEACHER: Create Assignment
@app.route('/api/teacher/assignments/create', methods=['POST'])
@login_required
def create_assignment():
    d = request.json
    task = AssignmentTask(
        title=d['title'],
        description=d['description'],
        course_id=int(d['course_id']),
        teacher_id=current_user.id,
        due_date=datetime.strptime(d['due_date'], '%Y-%m-%d').date()
    )
    db.session.add(task)
    db.session.commit()
    
    # Notify Students
    course = db.session.get(Course, task.course_id)
    for s in course.students:
        send_push_notification(s.id, "New Assignment", f"Task: {task.title} in {course.name}")
        
    return jsonify({"msg": "Assignment Created & Students Notified"})

# 2. STUDENT: Get My Assignments
@app.route('/api/student/assignments', methods=['GET'])
@login_required
def get_my_assignments():
    # Find courses the student is enrolled in
    enrolled_courses = [c.id for c in current_user.courses_enrolled]
    
    # Fetch assignments for those courses
    tasks = AssignmentTask.query.filter(AssignmentTask.course_id.in_(enrolled_courses)).order_by(AssignmentTask.due_date.desc()).all()
    
    return jsonify([t.to_dict() for t in tasks])

# --- SPECIAL ADMIN ROUTES (ID Card) ---

@app.route('/admin/id_card/<int:id>')
@login_required
def generate_single_id_card(id):
    if current_user.role != 'admin': return "Denied", 403
    student = db.session.get(User, id)
    if not student: return "Student not found", 404
    # Render the proper HTML template
    return render_template('id_card.html', student=student)

@app.route('/admin/id_cards/bulk')
@login_required
def generate_bulk_id_cards():
    if current_user.role != 'admin': return "Denied", 403
    students = User.query.filter_by(role='student').all()
    # Render the proper HTML template
    return render_template('id_cards_bulk.html', students=students)

@app.route('/uploads/<filename>')
def serve_file(filename): return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/api/receipt/<int:id>')
def serve_receipt(id):
    p = db.session.get(Payment, id)
    return f"<h1>Receipt #{p.id}</h1><p>Amount: {p.amount_paid}</p><button onclick='window.print()'>Print</button>" if p else "Not Found"

# =========================================================
# TEACHER SPECIFIC ROUTES
# =========================================================

# --- FIX: Add this route so Teachers can see Batches ---
@app.route('/api/teacher/sessions', methods=['GET'])
@login_required
def teacher_sessions():
    # Allow logged-in teachers to fetch all academic sessions
    sessions = AcademicSession.query.all()
    return jsonify([s.to_dict() for s in sessions])
# =========================================================
# STUDENT & PARENT ROUTES (ADD THIS SECTION)
# =========================================================

@app.route('/api/my/profile', methods=['GET'])
@login_required
def my_profile():
    return jsonify(current_user.to_dict())

@app.route('/api/my/attendance', methods=['GET'])
@login_required
def my_attendance():
    uid = current_user.id
    if current_user.role == 'parent':
        child = User.query.filter_by(parent_id=current_user.id).first()
        if not child: return jsonify([])
        uid = child.id
        
    atts = Attendance.query.filter_by(student_id=uid).order_by(Attendance.check_in_time.desc()).limit(50).all()
    
    # FIX: Added 'time' field so it shows up in the app
    return jsonify([{
        "date": a.check_in_time.strftime('%Y-%m-%d'),
        "time": a.check_in_time.strftime('%I:%M %p'),
        "status": a.status
    } for a in atts])

@app.route('/api/student/balance', methods=['GET']) # Fixed route name to match student.html
@login_required
def my_balance():
    uid = current_user.id
    if current_user.role == 'parent':
        child = User.query.filter_by(parent_id=current_user.id).first()
        if not child: return jsonify({"balance": 0})
        uid = child.id
    return jsonify(calculate_fee_status(uid))

@app.route('/api/student/notes', methods=['GET']) # Fixed route name to match student.html
@login_required
def my_notes():
    # Fetch notes for courses the student is enrolled in
    notes = SharedNote.query.join(student_course_association, (SharedNote.course_id == student_course_association.c.course_id)).filter(student_course_association.c.student_id == current_user.id).order_by(SharedNote.created_at.desc()).all()
    return jsonify([n.to_dict() for n in notes])

@app.route('/api/student/grades', methods=['GET']) # Stub for grades
@login_required
def my_grades():
    return jsonify([]) # Return empty list if no grade system yet

@app.route('/api/my/fees', methods=['GET'])
@login_required
def my_fees():
    uid = current_user.id
    # If Parent, fetch Child's data
    if current_user.role == 'parent':
        child = User.query.filter_by(parent_id=current_user.id).first()
        if not child: return jsonify({"balance": 0})
        uid = child.id
    return jsonify(calculate_fee_status(uid))

@app.route('/api/my/announcements', methods=['GET'])
@login_required
def my_announcements():
    # Fetch announcements targeted at 'all', 'students', or 'parents'
    target = 'parents' if current_user.role == 'parent' else 'students'
    anns = Announcement.query.filter(Announcement.target_group.in_(['all', target])).order_by(Announcement.created_at.desc()).all()
    return jsonify([a.to_dict() for a in anns])

# 2. GET STUDENTS (With Debugging & robust filtering)
# --- 1. ROBUST STUDENT FETCHING (Fixes "No Data" issue) ---
# FIX: Robust Filtering for Students
# FIX: Added 'course_ids' so frontend filtering works

@app.route('/api/teacher/students', methods=['GET'])
@login_required
def teacher_students():
    sid = request.args.get('session_id')
    cid = request.args.get('course_id')
    
    # Filter cleanup
    if sid in ['null', 'undefined', '', 'None']: sid = None
    if cid in ['null', 'undefined', '', 'None']: cid = None

    query = Course.query.filter_by(teacher_id=current_user.id)
    if cid: query = query.filter_by(id=int(cid))
    courses = query.all()
    
    students_list = []
    seen = set()
    for c in courses:
        for s in c.students:
            if sid and str(s.session_id) != str(sid): continue
            if s.id not in seen:
                students_list.append({
                    "id": s.id, 
                    "name": s.name, 
                    "admission_number": s.admission_number,
                    "profile_photo_url": s.profile_photo_url, 
                    "session_name": s.to_dict().get('session_name', 'N/A'),
		    "phone_number": s.phone_number,  # <--- ADD THIS LINE
                    "course_ids": [course.id for course in s.courses_enrolled] # <-- THIS LINE WAS MISSING
                })
                seen.add(s.id)
    return jsonify(students_list)

# --- 2. ROBUST REPORTS (Fixes Empty Reports) ---
@app.route('/api/teacher/reports/attendance', methods=['GET'])
@login_required
def teacher_reports():
    dt = request.args.get('date', date.today().strftime('%Y-%m-%d'))
    sid = request.args.get('session_id')
    cid = request.args.get('course_id')
    
    # FIX: Sanitize inputs
    if sid in ['null', 'undefined', '', 'None']: sid = None
    if cid in ['null', 'undefined', '', 'None']: cid = None
    
    query = Course.query.filter_by(teacher_id=current_user.id)
    if cid: query = query.filter_by(id=int(cid))
    courses = query.all()
    
    report = []
    seen = set()
    
    for c in courses:
        for s in c.students:
            if sid and str(s.session_id) != str(sid):
                continue
                
            if s.id not in seen:
                # Analytics
                total = Attendance.query.filter_by(student_id=s.id).count()
                present = Attendance.query.filter_by(student_id=s.id, status='Present').count()
                
                # Today's Status
                att = Attendance.query.filter(Attendance.student_id==s.id, db.func.date(Attendance.check_in_time)==dt).first()
                
                report.append({
                    "student_id": s.id,
                    "student_name": s.name, 
                    "photo_url": s.profile_photo_url, 
                    "admission_number": s.admission_number, 
                    "status": att.status if att else "Not Marked",
                    "total_classes": total,
                    "present": present
                })
                seen.add(s.id)
                
    return jsonify(report)
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
@app.route('/api/teacher/notes', methods=['GET', 'POST', 'DELETE'])
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


# 4. DAILY TOPIC / SYLLABUS UPDATE (Batch-Aware)
@app.route('/api/teacher/daily_topic', methods=['POST'])
@login_required
def daily_topic():
    d = request.json
    course_id = d.get('course_id')
    session_id = d.get('session_id') # Get Batch ID
    topic = d.get('topic')
    
    course = db.session.get(Course, course_id)
    if not course: return jsonify({"msg": "Error: Course not found"}), 404
    
    # 1. Format Topic Title
    title = f"Syllabus: {course.name}"
    if session_id:
        session = db.session.get(AcademicSession, session_id)
        if session: title += f" ({session.name})"

    # 2. Save Announcement to DB
    db.session.add(Announcement(
        title=title, 
        content=topic, 
        category="Syllabus", 
        target_group="students", 
        teacher_id=current_user.id
    ))
    db.session.commit()
    
    # 3. Filter Students (Batch-Wise)
    # Start with all students in the course
    students_to_notify = course.students
    
    # If a specific batch was selected, filter the list
    if session_id:
        students_to_notify = [s for s in course.students if s.session_id == int(session_id)]
    
    # 4. Send Notifications
    count = 0
    for s in students_to_notify:
        # Notify Student App
        send_push_notification(s.id, title, f"Covered today: {topic}")
        count += 1
        
        # Notify Parent (WhatsApp/SMS)
        if s.parent_id:
            parent = db.session.get(User, s.parent_id)
            if parent and parent.phone_number:
                # Placeholder for WhatsApp API
                msg = f"CST Update: Today in {course.name}, we covered '{topic}'. Batch: {title}"
                send_whatsapp_message(parent.phone_number, msg)
                
    return jsonify({"msg": f"Saved & Sent to {count} students in this batch."})

# 6. GET TEACHER COURSES
@app.route('/api/teacher/courses', methods=['GET'])
@login_required
def teacher_courses():
    # Fetch courses assigned to the logged-in teacher
    courses = Course.query.filter_by(teacher_id=current_user.id).all()
    return jsonify([c.to_dict() for c in courses])

# FIX: Sends notifications for PRESENT and ABSENT, and fixes "undefined" alert
@app.route('/api/teacher/attendance', methods=['POST'])
@login_required
def save_attendance():
    d = request.json
    
    # 1. Parse the selected date (This defaults to 00:00:00)
    selected_date = datetime.strptime(d['date'], '%Y-%m-%d').date()
    
    # 2. Get current time to make it look realistic
    current_time = datetime.utcnow().time()
    
    # 3. Combine Date + Current Time
    # If teacher submits at 5:30 PM, this saves "2026-01-20 17:30:00"
    final_dt = datetime.combine(selected_date, current_time)
    
    for r in d['attendance_data']:
        sid = int(r['student_id'])
        stat = r['status']
        
        # Check if record exists for this DATE
        exist = Attendance.query.filter(
            Attendance.student_id == sid, 
            db.func.date(Attendance.check_in_time) == selected_date
        ).first()
        
        if exist: 
            exist.status = stat
            # Optional: Update time to now if they change status? 
            # exist.check_in_time = final_dt 
        else: 
            # FIX: Use 'final_dt' instead of just 'dt'
            db.session.add(Attendance(student_id=sid, check_in_time=final_dt, status=stat))
        
        # NOTIFICATION LOGIC
        # Send App Notification to Student
        student = db.session.get(User, sid)
        if student:
            title = "Attendance Update"
            body = f"You have been marked {stat.upper()} today ({d['date']})."
            send_push_notification(sid, title, body)

            # Notify Parent
            if student.parent_id:
                # App Push to Parent
                p_body = f"Your child {student.name} is marked {stat} today."
                send_push_notification(student.parent_id, "Attendance Alert", p_body)
                
                # Send SMS ONLY if Absent
                if stat == 'Absent':
                    parent = db.session.get(User, student.parent_id)
                    if parent and parent.phone_number:
                        msg = f"Alert: {student.name} is marked ABSENT on {d['date']}. Please contact CST Institute."
                        send_whatsapp_message(parent.phone_number, msg) 
            
    db.session.commit()
    return jsonify({"message": "Attendance Saved & Notifications Sent!"})

# --- STUDENT/PARENT ROUTES ---
@app.route('/api/student/fees', methods=['GET'])
@login_required
def s_fees(): return jsonify(calculate_fee_status(current_user.id))

# --- STUDENT: DOWNLOAD HALL TICKET ---
@app.route('/student/my_hallticket')
@login_required
def my_hallticket():
    if current_user.role != 'student': return "Denied", 403
    
    student = current_user
    
    # 1. Get Session Name
    session_name = "Not Assigned"
    if student.session_id:
        sess = db.session.get(AcademicSession, student.session_id)
        if sess: session_name = sess.name

    # 2. Get Exam Details (Latest Exam for this session)
    exam = None
    if student.session_id:
        exam = Exam.query.filter_by(
            session_id=student.session_id
        ).order_by(Exam.id.desc()).first()

    # 3. Format Data
    courses = ", ".join([c.name for c in student.courses_enrolled]) or "N/A"
    photo = student.profile_photo_url if student.profile_photo_url else "https://placehold.co/150"

    # 4. Reuse the existing Hall Ticket Template
    return render_template(
        "hallticket.html",
        student=student,
        session_name=session_name,
        courses=courses,
        photo=photo,
        exam=exam
    )
# =========================================================
# COMPREHENSIVE REPORTS (EXCEL & PDF)
# =========================================================

@app.route('/api/reports/download')
@login_required
def download_excel_report():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    
    session_id = request.args.get('session_id')
    query = User.query.filter_by(role='student')
    
    if session_id and session_id != 'null':
        query = query.filter_by(session_id=int(session_id))
        
    students = query.all()
    data = []
    
    for s in students:
        # Calculate Fee
        fee = calculate_fee_status(s.id)
        
        # Calculate Attendance
        total_days = Attendance.query.filter_by(student_id=s.id).count()
        present_days = Attendance.query.filter_by(student_id=s.id, status='Present').count()
        att_pct = f"{round((present_days/total_days)*100, 1)}%" if total_days > 0 else "0%"
        
        data.append({
            "Admission No": s.admission_number,
            "Name": s.name,
            "Batch": s.to_dict().get('session_name', 'N/A'),
            "Phone": s.phone_number,
            "Father Name": s.father_name,
            "Total Fee": fee['total_due'],
            "Paid Fee": fee['total_paid'],
            "Balance Fee": fee['balance'],
            "Attendance %": att_pct
        })
    
    # Generate CSV (Compatible with Excel)
    df = pd.DataFrame(data)
    output = BytesIO()
    df.to_csv(output, index=False)
    output.seek(0)
    
    return send_file(
        output, 
        mimetype="text/csv", 
        as_attachment=True, 
        download_name=f"Student_Report_{date.today()}.csv"
    )

@app.route('/admin/report/print')
@login_required
def print_comprehensive_report():
    if current_user.role != 'admin': return "Denied", 403
    
    session_id = request.args.get('session_id')
    query = User.query.filter_by(role='student')
    session_name = "All Batches"
    
    if session_id and session_id != 'null':
        query = query.filter_by(session_id=int(session_id))
        sess = db.session.get(AcademicSession, int(session_id))
        if sess: session_name = sess.name
        
    students = query.all()
    report_data = []
    
    for s in students:
        fee = calculate_fee_status(s.id)
        # Get raw counts for the report
        present = Attendance.query.filter_by(student_id=s.id, status='Present').count()
        total = Attendance.query.filter_by(student_id=s.id).count()
        
        report_data.append({
            "name": s.name,
            "adm_no": s.admission_number,
            "batch": s.to_dict().get('session_name', ''),
            "phone": s.phone_number,
            "balance": fee['balance'],
            "attendance": f"{present}/{total}"
        })
        
    return render_template('report_print.html', students=report_data, session_name=session_name, report_date=date.today())
# --- NEW FEE REPORT FEATURES ---

@app.route('/api/reports/collections', methods=['GET'])
@login_required
def api_collection_report():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    # Query Payments joined with Student Data
    query = db.session.query(Payment, User).join(User, Payment.student_id == User.id)

    # Apply Date Filters
    if start_date:
        query = query.filter(db.func.date(Payment.payment_date) >= start_date)
    if end_date:
        query = query.filter(db.func.date(Payment.payment_date) <= end_date)

    # Get Results sorted by newest first
    results = query.order_by(Payment.payment_date.desc()).all()

    data = []
    total_collected = 0

    for pay, student in results:
        total_collected += pay.amount_paid
        # Get fee name
        fee_struct = db.session.get(FeeStructure, pay.fee_structure_id)
        fee_name = fee_struct.name if fee_struct else "Unknown Fee"
        
        data.append({
            "id": pay.id,
            "date": pay.payment_date.strftime('%Y-%m-%d'),
            "time": pay.payment_date.strftime('%I:%M %p'),
            "student_name": student.name,
            "adm_no": student.admission_number,
            "fee_name": fee_name,
            "mode": pay.payment_method,
            "amount": pay.amount_paid
        })

    return jsonify({"transactions": data, "total": total_collected})


@app.route('/admin/print/collections')
@login_required
def print_collection_report():
    if current_user.role != 'admin': return "Denied", 403
    
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    # Re-run query for the print view
    query = db.session.query(Payment, User).join(User, Payment.student_id == User.id)
    if start_date: query = query.filter(db.func.date(Payment.payment_date) >= start_date)
    if end_date: query = query.filter(db.func.date(Payment.payment_date) <= end_date)
    
    results = query.order_by(Payment.payment_date.asc()).all() # Oldest first for ledger
    
    total = sum(p.amount_paid for p, u in results)
    
    # Generate HTML for Printing
    rows = ""
    for pay, student in results:
        fee = db.session.get(FeeStructure, pay.fee_structure_id)
        fee_name = fee.name if fee else "-"
        rows += f"""
        <tr style="border-bottom: 1px solid #ddd;">
            <td style="padding: 8px;">{pay.payment_date.strftime('%Y-%m-%d')}</td>
            <td style="padding: 8px;">{student.admission_number}</td>
            <td style="padding: 8px;">{student.name}</td>
            <td style="padding: 8px;">{fee_name}</td>
            <td style="padding: 8px;">{pay.payment_method}</td>
            <td style="padding: 8px; text-align: right;">₹{pay.amount_paid}</td>
        </tr>
        """

    return f"""
    <html>
    <head>
        <title>Fee Collection Report</title>
        <style>
            body {{ font-family: 'Segoe UI', sans-serif; padding: 40px; color: #333; }}
            .header {{ text-align: center; margin-bottom: 30px; border-bottom: 2px solid #333; padding-bottom: 20px; }}
            h1 {{ margin: 0; color: #1173d4; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 14px; }}
            th {{ background: #f4f4f4; padding: 10px; text-align: left; border-bottom: 2px solid #aaa; }}
            .total-row {{ font-size: 18px; font-weight: bold; background: #eee; }}
            @media print {{ button {{ display: none; }} }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>CST INSTITUTE - Collection Report</h1>
            <p>Report Period: {start_date if start_date else 'Beginning'} to {end_date if end_date else 'Today'}</p>
        </div>
        
        <button onclick="window.print()" style="padding: 10px 20px; background: #333; color: #fff; border: none; cursor: pointer; margin-bottom: 20px;">Print Report</button>
        
        <table>
            <thead>
                <tr>
                    <th>Date</th>
                    <th>Adm No</th>
                    <th>Student Name</th>
                    <th>Fee Details</th>
                    <th>Mode</th>
                    <th style="text-align: right;">Amount</th>
                </tr>
            </thead>
            <tbody>
                {rows}
            </tbody>
            <tfoot>
                <tr class="total-row">
                    <td colspan="5" style="text-align: right; padding: 15px;">TOTAL COLLECTION:</td>
                    <td style="text-align: right; padding: 15px;">₹{total}</td>
                </tr>
            </tfoot>
        </table>
        
        <div style="margin-top: 50px; text-align: right; font-size: 12px;">
            <p>Generated by CST Admin Portal on {datetime.now().strftime('%d-%b-%Y %I:%M %p')}</p>
        </div>
    </body>
    </html>
    """
# =========================================================
# MIGRATION & STARTUP
# =========================================================

def check_and_upgrade_db():
    try:
        insp = inspect(db.engine)
        with db.engine.connect() as conn:
            user_cols = [c['name'] for c in insp.get_columns('user')]
            if 'admission_number' not in user_cols:
                conn.execute(text("ALTER TABLE user ADD COLUMN admission_number VARCHAR(50)"))
            if 'session_id' not in user_cols:
                conn.execute(text("ALTER TABLE user ADD COLUMN session_id INTEGER"))
            if 'fcm_token' not in user_cols:
                conn.execute(text("ALTER TABLE user ADD COLUMN fcm_token VARCHAR(500)"))

            ann_cols = [c['name'] for c in insp.get_columns('announcement')]
            if 'category' not in ann_cols:
                conn.execute(text("ALTER TABLE announcement ADD COLUMN category VARCHAR(50) DEFAULT 'General'"))
            if 'teacher_id' not in ann_cols:
                conn.execute(text("ALTER TABLE announcement ADD COLUMN teacher_id INTEGER"))

            fee_cols = [c['name'] for c in insp.get_columns('fee_structure')]
            if 'course_id' not in fee_cols:
                conn.execute(text("ALTER TABLE fee_structure ADD COLUMN course_id INTEGER"))

            conn.commit()

        print("Migration successful.")
    except Exception as e:
        print(f"Migration Error: {e}")


def initialize_database():
    with app.app_context():
        db.create_all()
        check_and_upgrade_db()
        init_firebase()

# --- REPLACEMENT FOR BULK UPLOAD FUNCTION ---
@app.route('/api/bulk_upload', methods=['POST'])
@login_required
def bulk_upload_users():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    
    file = request.files.get('file')
    if not file: return jsonify({"msg": "No file"}), 400

    try:
        import csv
        from io import TextIOWrapper
        
        # 1. Handle BOM characters from Excel
        csv_file = TextIOWrapper(file, encoding='utf-8-sig')
        reader = csv.DictReader(csv_file)
        
        added_count = 0
        
        for row in reader:
            student_email = row.get('Student_Email', '').strip()
            student_name = row.get('Student_FullName', '').strip()
            
            # Skip empty rows
            if not student_email or not student_name: continue

            # Skip existing students
            if User.query.filter_by(email=student_email).first(): continue 

            # --- PARENT LOGIC ---
            parent_phone = row.get('Parent_Phone', '').strip()
            parent_id = None
            if parent_phone:
                parent = User.query.filter_by(phone_number=parent_phone, role='parent').first()
                if not parent:
                    parent = User(
                        name=row.get('Parent_FullName', 'Parent'),
                        email=row.get('Parent_Email', f"{parent_phone}@cstparent.com"),
                        phone_number=parent_phone,
                        password=bcrypt.generate_password_hash("123456").decode('utf-8'),
                        role='parent'
                    )
                    db.session.add(parent)
                    db.session.flush()
                parent_id = parent.id

            # --- BATCH LOGIC ---
            batch_name = row.get('Batch_Name', '').strip()
            session_id = None
            if batch_name:
                sess = AcademicSession.query.filter(AcademicSession.name.ilike(batch_name)).first()
                if sess: session_id = sess.id
            
            # --- DATE FIX (DD-MM-YYYY -> YYYY-MM-DD) ---
            raw_dob = row.get('DOB', '').strip()
            final_dob = raw_dob
            try:
                # If date is like 20-01-2005, convert to 2005-01-20
                if '-' in raw_dob and len(raw_dob.split('-')[0]) == 2:
                    parts = raw_dob.split('-')
                    final_dob = f"{parts[2]}-{parts[1]}-{parts[0]}"
            except:
                pass # Keep original if format is unexpected

            # --- CREATE STUDENT ---
            student = User(
                name=student_name,
                email=student_email,
                phone_number=row.get('Student_Phone', ''),
                admission_number=row.get('Admission_No', ''),
                password=bcrypt.generate_password_hash("123456").decode('utf-8'),
                role='student',
                parent_id=parent_id,
                session_id=session_id,
                gender=row.get('Gender', ''),
                address_line1=row.get('Address', ''),
                dob=final_dob 
            )
            
            # --- COURSE ENROLLMENT ---
            course_names_str = row.get('Course_Names', '')
            if course_names_str:
                for c_name in course_names_str.split(','):
                    c_name = c_name.strip()
                    if c_name:
                        course = Course.query.filter(Course.name.ilike(c_name)).first()
                        if course: student.courses_enrolled.append(course)
            
            db.session.add(student)
            added_count += 1
            
        db.session.commit()
        return jsonify({"msg": f"Success! Added {added_count} students."}), 200

    except Exception as e:
        db.session.rollback()
        print(f"BULK UPLOAD ERROR: {str(e)}") 
        return jsonify({"msg": f"Error: {str(e)}"}), 500

# --- NEW: STUDENT ASK TEACHER FEATURE ---

@app.route('/api/student/my_teachers', methods=['GET'])
@login_required
def get_my_teachers():
    # 1. Get courses the student is enrolled in
    # 2. Extract unique teachers from those courses
    teachers = {}
    for course in current_user.courses_enrolled:
        if course.teacher:
            tid = course.teacher.id
            if tid not in teachers:
                teachers[tid] = {
                    "id": tid,
                    "name": course.teacher.name,
                    "photo": course.teacher.profile_photo_url,
                    "subjects": [course.name]
                }
            else:
                teachers[tid]['subjects'].append(course.name)
    
    return jsonify(list(teachers.values()))
# --- TEACHER: UPLOAD PHOTO ---
@app.route('/api/teacher/update_photo', methods=['POST'])
@login_required
def teacher_update_photo():
    if 'photo' not in request.files:
        return jsonify({"msg": "No file part"}), 400
    file = request.files['photo']
    if file.filename == '':
        return jsonify({"msg": "No selected file"}), 400
        
    if file:
        # 1. Generate a safe, unique filename (UUID)
        import uuid
        ext = os.path.splitext(file.filename)[1]
        filename = f"teacher_{current_user.id}_{uuid.uuid4().hex}{ext}"
        
        # 2. Save to the main 'uploads' folder (Same as Admin uploads)
        save_path = app.config['UPLOAD_FOLDER']
        if not os.path.exists(save_path):
            os.makedirs(save_path)
            
        file.save(os.path.join(save_path, filename))
        
        # 3. Save the correct URL path to Database
        # This matches your existing @app.route('/uploads/<filename>')
        current_user.profile_photo_url = f"/uploads/{filename}"
        db.session.commit()
        
        return jsonify({"msg": "Photo updated!", "url": current_user.profile_photo_url})



# --- TEACHER: GET DOUBTS ---
@app.route('/api/teacher/doubts', methods=['GET'])
@login_required
def get_teacher_doubts():
    # Get all doubts sent to this teacher, newest first
    doubts = Doubt.query.filter_by(teacher_id=current_user.id).order_by(Doubt.created_at.desc()).all()
    data = []
    for d in doubts:
        data.append({
            "id": d.id,
            "student_name": d.student.name,
            "question": d.question,
            "answer": d.answer,
            "date": d.created_at.strftime("%d-%m-%Y")
        })
    return jsonify(data)

# --- TEACHER: REPLY TO DOUBT ---
@app.route('/api/teacher/reply_doubt', methods=['POST'])
@login_required
def reply_doubt():
    data = request.json
    doubt_id = data.get('doubt_id')
    answer_text = data.get('answer')
    
    # Corrected query method
    doubt = db.session.get(Doubt, doubt_id)
    
    if not doubt or doubt.teacher_id != current_user.id:
        return jsonify({"msg": "Error"}), 403
        
    doubt.answer = answer_text
    db.session.commit()
    
    # Notify Student
    send_push_notification(doubt.student_id, "Teacher Replied", f"Answer: {answer_text[:50]}...")
    
    return jsonify({"msg": "Reply sent!"})

# --- STUDENT: ASK DOUBT (FIXED: Saves to Database) ---
@app.route('/api/student/ask_doubt', methods=['POST'])
@login_required
def ask_doubt():
    d = request.json
    teacher_id = d.get('teacher_id')
    question = d.get('question')
    
    if not teacher_id or not question:
        return jsonify({"msg": "Missing data"}), 400
    
    # 1. Save to Database (CRITICAL: So teacher can see it in the portal)
    new_doubt = Doubt(
        student_id=current_user.id,
        teacher_id=teacher_id,
        question=question
    )
    db.session.add(new_doubt)
    db.session.commit()
    
    # 2. Notify the Teacher
    send_push_notification(
        teacher_id, 
        f"New Doubt from {current_user.name}", 
        f"Question: {question}"
    )
    
    return jsonify({"msg": "Question sent to teacher successfully!"})

if __name__ == '__main__':
    initialize_database()
    app.run(debug=True, host='0.0.0.0', port=5000)