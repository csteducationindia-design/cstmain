from flask import Flask, request, jsonify, render_template, redirect, url_for, send_from_directory, send_file
from sqlalchemy import or_, inspect, text
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
import os
import json
from datetime import datetime, date
import csv
from io import StringIO
import uuid 
from werkzeug.utils import secure_filename 
import firebase_admin
from firebase_admin import credentials, messaging
import logging
import requests

# =========================================================
# CONFIGURATION
# =========================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'fallback_secret_key_change_this')
CORS(app, supports_credentials=True)

basedir = os.path.abspath(os.path.dirname(__file__))
data_dir = os.path.join(basedir, 'data')
if not os.path.exists(data_dir): os.makedirs(data_dir, exist_ok=True)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(data_dir, 'institute.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(basedir, 'uploads')
if not os.path.exists(app.config['UPLOAD_FOLDER']): os.makedirs(app.config['UPLOAD_FOLDER'])

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'serve_login_page'

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# =========================================================
# FIREBASE, SMS & WHATSAPP
# =========================================================
def init_firebase():
    if firebase_admin._apps: return True
    # Ensure you have your firebase-credentials.json in the root folder or set via ENV
    cred_path = os.environ.get('FIREBASE_CREDENTIALS_PATH', 'firebase-credentials.json')
    if os.path.exists(cred_path):
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)
        return True
    return False

def send_push_notification(user_id, title, body):
    try:
        if not init_firebase(): return "Firebase not configured"
        
        user = db.session.get(User, user_id)
        if not user or not user.fcm_token: return "No Token"
        
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            token=user.fcm_token,
        )
        messaging.send(message)
        return "Sent"
    except Exception as e:
        logger.error(f"Push Error: {e}")
        return str(e)

def send_actual_sms(phone_number, message_body, template_id=None):
    # Log SMS instead of sending if no API configured
    logger.info(f"SMS TO {phone_number}: {message_body}")
    return True

def send_whatsapp_message(phone_number, message_body):
    # INTEGRATE YOUR WHATSAPP API HERE (e.g., Twilio, Meta Cloud API, or 3rd Party)
    # Example logic:
    # requests.post("https://api.whatsappprovider.com/send", json={"phone": phone_number, "text": message_body})
    logger.info(f"WHATSAPP TO {phone_number}: {message_body}")
    return True

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
    dob = db.Column(db.String(20)); gender = db.Column(db.String(20)); profile_photo_url = db.Column(db.String(300))
    father_name = db.Column(db.String(100)); mother_name = db.Column(db.String(100))
    address_line1 = db.Column(db.String(200)); city = db.Column(db.String(100)); state = db.Column(db.String(100)); pincode = db.Column(db.String(20))
    fcm_token = db.Column(db.String(500), nullable=True)
    session_id = db.Column(db.Integer, db.ForeignKey('academic_session.id'), nullable=True)
    
    courses_enrolled = db.relationship('Course', secondary=student_course_association, lazy='subquery', backref=db.backref('students', lazy=True))

    def to_dict(self):
        sess_name = "Unassigned"
        if self.session_id:
            sess = db.session.get(AcademicSession, self.session_id)
            if sess: sess_name = sess.name
        
        parent_name = "None"
        if self.parent_id:
            p = db.session.get(User, self.parent_id)
            if p: parent_name = p.name

        return {
            "id": self.id, "name": self.name, "email": self.email, "role": self.role,
            "created_at": self.created_at.strftime('%Y-%m-%d'),
            "phone_number": self.phone_number, "parent_id": self.parent_id, "parent_name": parent_name,
            "dob": self.dob, "profile_photo_url": self.profile_photo_url,
            "gender": self.gender, "father_name": self.father_name, "mother_name": self.mother_name,
            "address_line1": self.address_line1, "city": self.city, "state": self.state, "pincode": self.pincode,
            "session_id": self.session_id, "session_name": sess_name, "admission_number": self.admission_number,
            "course_ids": [c.id for c in self.courses_enrolled] if self.role == 'student' else []
        }

class Course(db.Model):
    id = db.Column(db.Integer, primary_key=True); name = db.Column(db.String(150), nullable=False)
    subjects = db.Column(db.String(300), nullable=False); teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    teacher = db.relationship('User', backref=db.backref('courses_teaching', lazy=True))
    def to_dict(self): return {"id": self.id, "name": self.name, "subjects": [s.strip() for s in self.subjects.split(',')], "teacher_id": self.teacher_id, "teacher_name": self.teacher.name if self.teacher else "Unassigned"}

class AcademicSession(db.Model):
    id = db.Column(db.Integer, primary_key=True); name = db.Column(db.String(100)); start_date = db.Column(db.String(20)); end_date = db.Column(db.String(20)); status = db.Column(db.String(20))
    def to_dict(self): return {"id": self.id, "name": self.name, "start_date": self.start_date, "end_date": self.end_date, "status": self.status}

class Announcement(db.Model):
    id = db.Column(db.Integer, primary_key=True); title = db.Column(db.String(150)); content = db.Column(db.Text)
    category = db.Column(db.String(50), default='General'); target_group = db.Column(db.String(50))
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id')); created_at = db.Column(db.DateTime, default=datetime.utcnow)
    def to_dict(self): return {"id": self.id, "title": self.title, "content": self.content, "category": self.category, "target_group": self.target_group, "created_at": self.created_at.strftime('%Y-%m-%d %H:%M')}

class FeeStructure(db.Model):
    id = db.Column(db.Integer, primary_key=True); name = db.Column(db.String(150))
    academic_session_id = db.Column(db.Integer, db.ForeignKey('academic_session.id')); course_id = db.Column(db.Integer, db.ForeignKey('course.id'))
    total_amount = db.Column(db.Float); due_date = db.Column(db.Date, default=date.today)
    def to_dict(self): return {"id": self.id, "name": self.name, "total_amount": self.total_amount, "due_date": self.due_date.strftime('%Y-%m-%d')}

class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True); student_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    fee_structure_id = db.Column(db.Integer, db.ForeignKey('fee_structure.id')); amount_paid = db.Column(db.Float)
    payment_date = db.Column(db.DateTime, default=datetime.utcnow); payment_method = db.Column(db.String(50))

class Attendance(db.Model):
    id = db.Column(db.Integer, primary_key=True); student_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    check_in_time = db.Column(db.DateTime, default=datetime.utcnow); status = db.Column(db.String(10), nullable=False)

class SharedNote(db.Model):
    id = db.Column(db.Integer, primary_key=True); filename = db.Column(db.String(300)); original_filename = db.Column(db.String(300))
    title = db.Column(db.String(150)); description = db.Column(db.Text); course_id = db.Column(db.Integer, db.ForeignKey('course.id'))
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id')); created_at = db.Column(db.DateTime, default=datetime.utcnow)
    course = db.relationship('Course', backref=db.backref('notes', lazy=True))
    def to_dict(self): return {"id": self.id, "title": self.title, "course_name": self.course.name if self.course else "N/A", "filename": self.filename, "created_at": self.created_at.strftime('%Y-%m-%d')}

# =========================================================
# HELPER FUNCTIONS
# =========================================================
def calculate_fee_status(student_id):
    try:
        student = db.session.get(User, student_id)
        if not student: return {"balance": 0, "due_date": "N/A"}
        
        payments = Payment.query.filter_by(student_id=student_id).all()
        total_paid = sum(p.amount_paid for p in payments)
        total_due = 0.0
        due_dates = []

        if student.courses_enrolled:
            for course in student.courses_enrolled:
                for fee in FeeStructure.query.filter_by(course_id=course.id).all():
                    total_due += fee.total_amount
                    if fee.due_date: due_dates.append(fee.due_date)
        
        if student.session_id:
            for fee in FeeStructure.query.filter(FeeStructure.course_id==None, FeeStructure.academic_session_id==student.session_id).all():
                total_due += fee.total_amount
                if fee.due_date: due_dates.append(fee.due_date)

        final_due_date = min(due_dates) if due_dates else date.today()
        return {"total_due": total_due, "total_paid": total_paid, "balance": total_due - total_paid, "due_date": final_due_date.strftime('%Y-%m-%d'), "pending_days": (date.today() - final_due_date).days}
    except: return {"balance": 0, "due_date": "N/A"}

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

@app.route('/<role>')
@login_required
def serve_role_page(role):
    if role != current_user.role: return redirect(f"/{current_user.role}")
    return render_template(f'{role}.html')

@app.route('/uploads/<filename>')
def serve_file(filename): return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# =========================================================
# TEACHER ROUTES (Updated for Syllabus)
# =========================================================

# 1. GET SESSIONS (Allow Teachers to see Batches)
@app.route('/api/teacher/sessions', methods=['GET'])
@login_required
def teacher_sessions():
    if current_user.role != 'teacher': return jsonify({"msg": "Denied"}), 403
    return jsonify([s.to_dict() for s in AcademicSession.query.all()])

# 2. GET STUDENTS (With Batch Filter & Photo)
@app.route('/api/teacher/students', methods=['GET'])
@login_required
def teacher_students():
    if current_user.role != 'teacher': return jsonify({"msg": "Denied"}), 403
    
    session_id = request.args.get('session_id')
    courses = Course.query.filter_by(teacher_id=current_user.id).all()
    student_list = []
    seen_ids = set()
    
    for c in courses:
        students = [s for s in c.students if (not session_id or str(s.session_id) == str(session_id))]
        for s in students:
            if s.id not in seen_ids:
                student_list.append({
                    "id": s.id, 
                    "name": s.name, 
                    "admission_number": s.admission_number,
                    "profile_photo_url": s.profile_photo_url,
                    "session_name": s.to_dict()['session_name'],
                    "course_name": c.name
                })
                seen_ids.add(s.id)
    return jsonify(student_list)

# 3. DAILY TOPIC / SYLLABUS (NO SMS, USE PUSH/WHATSAPP)
@app.route('/api/teacher/daily_topic', methods=['POST'])
@login_required
def daily_topic():
    if current_user.role != 'teacher': return jsonify({"msg": "Denied"}), 403
    d = request.json
    
    course_id = d.get('course_id')
    topic = d.get('topic')
    
    if not course_id or not topic: return jsonify({"msg": "Missing Data"}), 400
    
    course = db.session.get(Course, course_id)
    if not course: return jsonify({"msg": "Course not found"}), 404
    
    # 1. Log it as a "Syllabus" Announcement
    a = Announcement(
        title=f"Topic: {course.name}",
        content=f"Today we covered: {topic}",
        category="Syllabus",
        target_group="students",
        teacher_id=current_user.id
    )
    db.session.add(a)
    db.session.commit()
    
    # 2. Send Notifications (App & WhatsApp)
    for s in course.students:
        # PUSH to Student
        send_push_notification(s.id, f"Daily Topic: {course.name}", f"{topic}")
        
        # PUSH & WHATSAPP to Parent
        if s.parent_id:
            send_push_notification(s.parent_id, f"Syllabus Update: {course.name}", f"Your child learned: {topic}")
            
            parent = db.session.get(User, s.parent_id)
            if parent and parent.phone_number:
                msg = f"Today in {course.name}, your child learned: {topic}. - CST Institute"
                send_whatsapp_message(parent.phone_number, msg)
                
    return jsonify({"msg": "Syllabus updated (App + WhatsApp)"}), 200

# 4. MARK ATTENDANCE
@app.route('/api/teacher/attendance', methods=['POST'])
@login_required
def save_attendance():
    if current_user.role != 'teacher': return jsonify({"msg": "Denied"}), 403
    data = request.json
    dt = datetime.strptime(data.get('date'), '%Y-%m-%d')
    
    for r in data.get('attendance_data', []):
        student_id = r['student_id']
        status = r['status']
        
        exists = Attendance.query.filter(Attendance.student_id == student_id, db.func.date(Attendance.check_in_time) == dt.date()).first()
        
        if exists: exists.status = status
        else:
            db.session.add(Attendance(student_id=student_id, check_in_time=dt, status=status))
            if status == 'Absent':
                send_push_notification(student_id, "Attendance Alert", f"Marked ABSENT on {data.get('date')}")
    
    db.session.commit()
    return jsonify({"msg": "Attendance Saved"})

# 5. TEACHER ANNOUNCEMENTS
@app.route('/api/teacher/announcements', methods=['GET', 'POST'])
@login_required
def teacher_announcements():
    if current_user.role != 'teacher': return jsonify({"msg": "Denied"}), 403
    if request.method == 'POST':
        d = request.json
        a = Announcement(title=d['title'], content=d['content'], category=d.get('category', 'Update'), target_group='students', teacher_id=current_user.id)
        db.session.add(a); db.session.commit()
        # Broadcast
        courses = Course.query.filter_by(teacher_id=current_user.id).all()
        notified = set()
        for c in courses:
            for s in c.students:
                if s.id not in notified:
                    send_push_notification(s.id, d['title'], d['content'])
                    notified.add(s.id)
        return jsonify(a.to_dict()), 201
    anns = Announcement.query.filter_by(teacher_id=current_user.id).order_by(Announcement.created_at.desc()).all()
    return jsonify([a.to_dict() for a in anns])

# 6. TEACHER NOTES
@app.route('/api/teacher/notes', methods=['GET', 'POST'])
@login_required
def teacher_notes():
    if current_user.role != 'teacher': return jsonify({"msg": "Denied"}), 403
    if request.method == 'POST':
        f = request.files['file']
        fn = secure_filename(f.filename)
        uid = f"{uuid.uuid4()}{os.path.splitext(fn)[1]}"
        f.save(os.path.join(app.config['UPLOAD_FOLDER'], uid))
        db.session.add(SharedNote(title=request.form['title'], description=request.form['description'], filename=uid, course_id=int(request.form['course_id']), teacher_id=current_user.id))
        db.session.commit()
        return jsonify({"msg": "Uploaded"}), 201
    return jsonify([n.to_dict() for n in SharedNote.query.filter_by(teacher_id=current_user.id).all()])

# 7. TEACHER REPORTS
@app.route('/api/teacher/reports/attendance', methods=['GET'])
@login_required
def teacher_att_report():
    if current_user.role != 'teacher': return jsonify({"msg": "Denied"}), 403
    date_filter = request.args.get('date', date.today().strftime('%Y-%m-%d'))
    session_id = request.args.get('session_id')
    
    atts = Attendance.query.filter(db.func.date(Attendance.check_in_time) == date_filter).all()
    report = []
    for a in atts:
        u = db.session.get(User, a.student_id)
        if u and (not session_id or str(u.session_id) == str(session_id)):
            report.append({ "student_name": u.name, "photo_url": u.profile_photo_url, "admission_number": u.admission_number, "status": a.status })
    return jsonify(report)

@app.route('/api/teacher/courses', methods=['GET'])
@login_required
def teacher_courses(): 
    return jsonify([c.to_dict() for c in Course.query.filter_by(teacher_id=current_user.id).all()])

# --- ADMIN API ---
@app.route('/api/users', methods=['GET', 'POST', 'DELETE'])
@login_required
def api_users():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    if request.method == 'GET':
        session_id = request.args.get('session_id')
        search = request.args.get('search', '').lower()
        q = User.query
        if session_id and str(session_id).isdigit(): q = q.filter_by(session_id=int(session_id))
        if search: q = q.filter(or_(User.name.ilike(f'%{search}%'), User.email.ilike(f'%{search}%')))
        return jsonify([u.to_dict() for u in q.all()])

    if request.method == 'DELETE':
        u = db.session.get(User, int(request.args.get('id')))
        if u: db.session.delete(u); db.session.commit()
        return jsonify({"msg": "Deleted"})

    d = request.form
    if d.get('id'): u = db.session.get(User, int(d.get('id')))
    else:
        u = User(name=d['name'], email=d['email'], password="", role=d.get('role', 'student'))
        db.session.add(u)

    u.name = d['name']; u.email = d['email']; u.phone_number = d.get('phone_number')
    u.admission_number = d.get('admission_number')
    u.dob = d.get('dob'); u.gender = d.get('gender')
    u.address_line1 = d.get('address_line1'); u.city = d.get('city')
    u.state = d.get('state'); u.pincode = d.get('pincode')
    
    if d.get('password'): u.password = bcrypt.generate_password_hash(d['password']).decode('utf-8')
    if d.get('session_id') and str(d['session_id']).isdigit(): u.session_id = int(d['session_id'])
    if d.get('parent_id') and str(d['parent_id']).isdigit(): u.parent_id = int(d['parent_id'])

    if u.role == 'student':
        c_ids = request.form.getlist('course_ids')
        if c_ids: u.courses_enrolled = Course.query.filter(Course.id.in_([int(x) for x in c_ids if str(x).isdigit()])).all()

    if 'profile_photo_file' in request.files:
        f = request.files['profile_photo_file']
        if f and '.' in f.filename:
            uid = f"{uuid.uuid4()}{os.path.splitext(f.filename)[1]}"
            f.save(os.path.join(app.config['UPLOAD_FOLDER'], uid))
            u.profile_photo_url = f"/uploads/{uid}"

    db.session.commit()
    return jsonify(u.to_dict())

@app.route('/api/bulk_upload', methods=['POST'])
@login_required
def bulk_upload():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403
    file = request.files['file']
    try:
        stream = StringIO(file.stream.read().decode("UTF8"), newline=None)
        reader = csv.DictReader(stream)
        count = 0
        for row in reader:
            if not row.get('email') or User.query.filter_by(email=row['email']).first(): continue
            u = User(name=row['name'], email=row['email'], role='student', password=bcrypt.generate_password_hash('123456').decode('utf-8'), phone_number=row.get('phone_number'), admission_number=row.get('admission_number'))
            db.session.add(u)
            count += 1
        db.session.commit()
        return jsonify({"msg": f"Imported {count} users"})
    except Exception as e: return jsonify({"msg": str(e)}), 500

@app.route('/api/admin/student/<int:id>', methods=['GET'])
@login_required
def get_single_student(id): return jsonify(db.session.get(User, id).to_dict())

@app.route('/api/fee_status', methods=['GET'])
@login_required
def fee_status():
    course_id = request.args.get('course_id')
    query = User.query.filter_by(role='student')
    if course_id and str(course_id).isdigit(): query = query.filter(User.courses_enrolled.any(id=int(course_id)))
    return jsonify([{"student_id": s.id, "student_name": s.name, **calculate_fee_status(s.id)} for s in query.all()])

@app.route('/api/send_sms_alert', methods=['POST'])
@login_required
def send_sms():
    try:
        d = request.json
        u = db.session.get(User, d.get('student_id'))
        fee = calculate_fee_status(u.id)
        msg = f"Dear {u.name}, your fee of Rs {fee['balance']} is pending. Due: {fee['due_date']}. CST Institute 7083021167"
        send_actual_sms(u.phone_number, msg, "1707176388002841408")
        return jsonify({"message": "Sent"})
    except: return jsonify({"message": "Error"}), 500

# --- GENERIC CRUD ROUTES ---
@app.route('/api/sessions', methods=['GET', 'POST', 'PUT', 'DELETE'])
@login_required
def sessions_api():
    if request.method == 'GET': return jsonify([s.to_dict() for s in AcademicSession.query.all()])
    d = request.json
    if request.method == 'POST': db.session.add(AcademicSession(name=d['name'], start_date=d['start_date'], end_date=d['end_date'], status=d['status'])); db.session.commit()
    elif request.method == 'DELETE': db.session.delete(db.session.get(AcademicSession, request.args.get('id'))); db.session.commit()
    return jsonify({"msg":"OK"})

@app.route('/api/courses', methods=['GET', 'POST', 'DELETE'])
@login_required
def courses_api():
    if request.method == 'GET': return jsonify([c.to_dict() for c in Course.query.all()])
    if request.method == 'POST': db.session.add(Course(name=request.json['name'], subjects=request.json['subjects'], teacher_id=request.json.get('teacher_id'))); db.session.commit()
    elif request.method == 'DELETE': db.session.delete(db.session.get(Course, request.args.get('id'))); db.session.commit()
    return jsonify({"msg":"OK"})

@app.route('/api/fee_structures', methods=['GET', 'POST', 'DELETE'])
@login_required
def fees_api():
    if request.method == 'GET': return jsonify([f.to_dict() for f in FeeStructure.query.all()])
    d = request.json
    if request.method == 'POST': db.session.add(FeeStructure(name=d['name'], academic_session_id=d['academic_session_id'], course_id=d.get('course_id'), total_amount=d['total_amount'], due_date=datetime.strptime(d['due_date'], '%Y-%m-%d').date())); db.session.commit()
    elif request.method == 'DELETE': db.session.delete(db.session.get(FeeStructure, request.args.get('id'))); db.session.commit()
    return jsonify({"msg":"OK"})

@app.route('/api/announcements', methods=['GET', 'POST', 'DELETE'])
@login_required
def ann_api():
    if request.method == 'GET': return jsonify([a.to_dict() for a in Announcement.query.order_by(Announcement.created_at.desc()).all()])
    if request.method == 'POST': db.session.add(Announcement(title=request.json['title'], content=request.json['content'], target_group=request.json['target_group'])); db.session.commit()
    elif request.method == 'DELETE': db.session.delete(db.session.get(Announcement, request.args.get('id'))); db.session.commit()
    return jsonify({"msg":"OK"})

@app.route('/admin/id_card/<int:id>')
@login_required
def id_card(id):
    u = db.session.get(User, id)
    qr = url_for('static', filename=f'qr_codes/qr_{u.admission_number}.png')
    return f"<html><body><div style='border:1px solid #000;width:300px;padding:20px;text-align:center'><h3>CST INSTITUTE</h3><img src='{u.profile_photo_url}' style='width:100px'><br><b>{u.name}</b><br>Adm: {u.admission_number}<br>Phone: {u.phone_number}<br><img src='{qr}' style='width:80px'></div><script>window.print()</script></body></html>"

@app.route('/admin/id_cards/bulk')
@login_required
def bulk_cards():
    users = User.query.filter_by(role='student').all()
    cards = "".join([f"<div style='border:1px solid #000;width:45%;display:inline-block;margin:10px;padding:10px;text-align:center'><h3>CST</h3><b>{u.name}</b><br>Adm: {u.admission_number}<br><img src='{url_for('static', filename=f'qr_codes/qr_{u.admission_number}.png')}' style='width:60px'></div>" for u in users])
    return f"<html><body>{cards}<br><button onclick='window.print()'>Print All</button></body></html>"

def init_db():
    with app.app_context():
        db.create_all()
        try:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE user ADD COLUMN admission_number VARCHAR(50)"))
                conn.commit()
        except: pass

if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=5000)