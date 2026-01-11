from flask import Flask, request, jsonify, render_template, redirect, url_for, send_from_directory, send_file
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
import os
import json
from datetime import datetime, date
import csv
from io import StringIO, BytesIO
import uuid 
from werkzeug.utils import secure_filename 
from sqlalchemy import or_, inspect, text
import logging
import pandas as pd
import threading

# --- CONFIGURATION ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates')
app.config['SECRET_KEY'] = 'secret_key_change_in_production'
CORS(app, supports_credentials=True)

basedir = os.path.abspath(os.path.dirname(__file__))
data_dir = os.path.join(basedir, 'data')
if not os.path.exists(data_dir): os.makedirs(data_dir)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(data_dir, 'institute.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(basedir, 'uploads')
if not os.path.exists(app.config['UPLOAD_FOLDER']): os.makedirs(app.config['UPLOAD_FOLDER'])

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'serve_login'

@login_manager.user_loader
def load_user(user_id):
    try: return db.session.get(User, int(user_id))
    except: return None

# --- MODELS ---
student_course = db.Table('student_course',
    db.Column('student_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('course_id', db.Integer, db.ForeignKey('course.id'), primary_key=True)
)

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    admission_number = db.Column(db.String(50))
    session_id = db.Column(db.Integer, db.ForeignKey('academic_session.id'))
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(60), nullable=False)
    role = db.Column(db.String(50), nullable=False)
    phone_number = db.Column(db.String(20))
    parent_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    profile_photo_url = db.Column(db.String(300))
    dob = db.Column(db.String(20))
    gender = db.Column(db.String(20))
    father_name = db.Column(db.String(100))
    mother_name = db.Column(db.String(100))
    address_line1 = db.Column(db.String(200))
    city = db.Column(db.String(100))
    state = db.Column(db.String(100))
    pincode = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    fcm_token = db.Column(db.String(500))

    session = db.relationship('AcademicSession', backref='students')
    courses_enrolled = db.relationship('Course', secondary=student_course, backref='students')

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "email": self.email, "role": self.role,
            "admission_number": self.admission_number,
            "session_id": self.session_id,
            "session_name": self.session.name if self.session else "Unassigned",
            "phone_number": self.phone_number, "parent_id": self.parent_id,
            "profile_photo_url": self.profile_photo_url, "dob": self.dob,
            "gender": self.gender, "father_name": self.father_name,
            "mother_name": self.mother_name, "address_line1": self.address_line1,
            "city": self.city, "state": self.state, "pincode": self.pincode,
            "course_ids": [c.id for c in self.courses_enrolled] if self.role == 'student' else []
        }

class Course(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    subjects = db.Column(db.String(300), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'))
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

class FeeStructure(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    academic_session_id = db.Column(db.Integer, db.ForeignKey('academic_session.id'), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('course.id'), nullable=True) 
    total_amount = db.Column(db.Float, nullable=False)
    due_date = db.Column(db.Date, default=date.today)
    def to_dict(self): return {"id": self.id, "name": self.name, "total_amount": self.total_amount, "due_date": self.due_date.strftime('%Y-%m-%d') if self.due_date else None}

class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    amount_paid = db.Column(db.Float, nullable=False)
    payment_date = db.Column(db.DateTime, default=datetime.utcnow)
    payment_method = db.Column(db.String(50))
    fee_structure_id = db.Column(db.Integer, db.ForeignKey('fee_structure.id'), nullable=True)

class Announcement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150)); content = db.Column(db.Text); target_group = db.Column(db.String(50)); created_at = db.Column(db.DateTime, default=datetime.utcnow)
    def to_dict(self): return {"id": self.id, "title": self.title, "content": self.content, "target_group": self.target_group, "created_at": self.created_at.strftime('%Y-%m-%d')}

# --- LOGIC ---
def calculate_fee_status(student_id):
    student = db.session.get(User, student_id)
    if not student: return {"balance": 0, "due_date": "N/A", "pending_days": 0}
    
    payments = Payment.query.filter_by(student_id=student_id).all()
    total_paid = sum(p.amount_paid for p in payments)
    total_due = 0.0
    due_dates = []

    if student.session_id:
        # 1. General Session Fees
        session_fees = FeeStructure.query.filter_by(academic_session_id=student.session_id, course_id=None).all()
        for f in session_fees:
            total_due += f.total_amount
            if f.due_date: due_dates.append(f.due_date)
            
        # 2. Course Specific Fees
        for course in student.courses_enrolled:
            c_fees = FeeStructure.query.filter_by(academic_session_id=student.session_id, course_id=course.id).all()
            for f in c_fees:
                total_due += f.total_amount
                if f.due_date: due_dates.append(f.due_date)
    else:
        logger.warning(f"Student {student.name} (ID: {student.id}) has NO Session assigned. Fees will be 0.")

    balance = max(0, total_due - total_paid)
    final_due = min(due_dates) if due_dates else date.today()
    pending_days = (date.today() - final_due).days if balance > 0 else 0
    
    return {"total_due": total_due, "total_paid": total_paid, "balance": balance, "due_date": final_due.strftime('%Y-%m-%d'), "pending_days": pending_days}

def allowed_file(filename, extensions): return '.' in filename and filename.rsplit('.', 1)[1].lower() in extensions

# --- ROUTES ---
@app.route('/')
def serve_login(): return render_template('login.html')

@app.route('/<role>')
@login_required
def serve_role(role):
    if current_user.role == role: return render_template(f'{role}.html')
    return redirect('/')

@app.route('/api/login', methods=['POST'])
def login():
    d = request.json
    u = User.query.filter_by(email=d.get('email')).first()
    if u and bcrypt.check_password_hash(u.password, d.get('password')):
        login_user(u)
        return jsonify({"message": "OK", "user": u.to_dict()})
    return jsonify({"message": "Invalid"}), 401

@app.route('/api/logout', methods=['POST'])
@login_required
def logout(): logout_user(); return jsonify({"msg": "Out"})

@app.route('/api/check_session')
def check_session():
    if current_user.is_authenticated: return jsonify({"logged_in": True, "user": current_user.to_dict()})
    return jsonify({"logged_in": False}), 401

@app.route('/api/users', methods=['GET', 'POST', 'PUT'])
@login_required
def api_users():
    if current_user.role != 'admin': return jsonify({"msg": "Denied"}), 403

    if request.method == 'GET':
        q = User.query
        sid = request.args.get('session_id')
        if sid and sid != 'null' and sid != '': q = q.filter_by(session_id=int(sid))
        search = request.args.get('search')
        if search: q = q.filter(or_(User.name.ilike(f'%{search}%'), User.email.ilike(f'%{search}%')))
        return jsonify([u.to_dict() for u in q.all()])

    d = request.form
    if request.method == 'POST':
        if User.query.filter_by(email=d['email']).first(): return jsonify({"msg": "Email exists"}), 400
        u = User(name=d['name'], email=d['email'], password=bcrypt.generate_password_hash(d['password']).decode('utf-8'), role=d['role'])
        db.session.add(u)
    else: # PUT
        u = db.session.get(User, int(d['id']))
        if not u: return jsonify({"msg": "Not found"}), 404
        u.name = d['name']; u.email = d['email']
        if d.get('password'): u.password = bcrypt.generate_password_hash(d['password']).decode('utf-8')

    # Update Fields
    u.phone_number = d.get('phone_number')
    u.admission_number = d.get('admission_number')
    u.dob = d.get('dob')
    u.gender = d.get('gender')
    u.father_name = d.get('father_name')
    u.mother_name = d.get('mother_name')
    u.address_line1 = d.get('address_line1')
    u.city = d.get('city'); u.state = d.get('state'); u.pincode = d.get('pincode')
    if d.get('parent_id'): u.parent_id = int(d['parent_id'])
    
    # Update Session (CRITICAL FIX: Don't overwrite with None unless explicit)
    if 'session_id' in d and d['session_id']:
        u.session_id = int(d['session_id'])

    # Update Courses
    if u.role == 'student' and 'course_ids' in d:
        u.courses_enrolled = []
        if d['course_ids']:
            ids = [int(x) for x in d['course_ids'].split(',') if x]
            for cid in ids:
                c = db.session.get(Course, cid)
                if c: u.courses_enrolled.append(c)

    if 'profile_photo_file' in request.files:
        f = request.files['profile_photo_file']
        if allowed_file(f.filename, {'png','jpg','jpeg'}):
            uid = f"{uuid.uuid4()}{os.path.splitext(f.filename)[1]}"
            f.save(os.path.join(app.config['UPLOAD_FOLDER'], uid))
            u.profile_photo_url = f"/uploads/{uid}"

    db.session.commit()
    return jsonify(u.to_dict())

@app.route('/api/users/<int:id>', methods=['DELETE'])
@login_required
def del_user(id):
    u = db.session.get(User, id)
    if u: 
        if u.role == 'student':
            Payment.query.filter_by(student_id=id).delete()
            u.courses_enrolled = []
        db.session.delete(u)
        db.session.commit()
    return jsonify({"msg": "Deleted"})

# --- REPORT API ---
@app.route('/api/reports/fee_pending', methods=['GET'])
@login_required
def fee_report_api():
    cid = request.args.get('course_id')
    students = User.query.filter_by(role='student').all()
    res = []
    for s in students:
        if cid:
            s_c_ids = [c.id for c in s.courses_enrolled]
            if int(cid) not in s_c_ids: continue
        
        st = calculate_fee_status(s.id)
        if st['balance'] > 0:
            res.append({"student_id": s.id, "student_name": s.name, "phone_number": s.phone_number, "balance": st['balance'], "due_date": st['due_date'], "pending_days": st['pending_days']})
    return jsonify(res)

@app.route('/api/export/<type>')
@login_required
def export(type):
    output = BytesIO()
    df = pd.DataFrame()
    sid = request.args.get('session_id')
    q = User.query.filter_by(role='student')
    if sid and sid != 'null' and sid != '': q = q.filter_by(session_id=int(sid))
    students = q.all()

    if type == 'fee_pending':
        data = []
        for s in students:
            st = calculate_fee_status(s.id)
            if st['balance'] > 0:
                data.append({"Name": s.name, "Batch": s.session.name if s.session else "-", "Phone": s.phone_number, "Balance": st['balance']})
        df = pd.DataFrame(data)
    elif type == 'students':
        data = []
        for s in students:
            data.append({"Admission No": s.admission_number, "Name": s.name, "Batch": s.session.name if s.session else "-", "Phone": s.phone_number, "Email": s.email})
        df = pd.DataFrame(data)

    if df.empty:
        df = pd.DataFrame(columns=["Message"])
        df.loc[0] = ["No data found for selected filters"]

    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    output.seek(0)
    return send_file(output, download_name=f"{type}.xlsx", as_attachment=True, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

# --- OTHER CRUD ---
@app.route('/api/sessions', methods=['GET', 'POST', 'PUT', 'DELETE'])
@login_required
def sessions():
    if request.method == 'GET': return jsonify([s.to_dict() for s in AcademicSession.query.all()])
    if request.method == 'DELETE': 
        db.session.delete(db.session.get(AcademicSession, int(request.path.split('/')[-1])))
        db.session.commit(); return jsonify({"msg": "Del"})
    d = request.json
    if request.method == 'POST': db.session.add(AcademicSession(name=d['name'], start_date=d['start_date'], end_date=d['end_date'], status=d['status']))
    else: 
        s = db.session.get(AcademicSession, int(d['id']))
        s.name = d['name']; s.start_date = d['start_date']; s.end_date = d['end_date']; s.status = d['status']
    db.session.commit()
    return jsonify({"msg": "Saved"})

@app.route('/api/courses', methods=['GET', 'POST', 'PUT', 'DELETE'])
@login_required
def courses():
    if request.method == 'GET': return jsonify([c.to_dict() for c in Course.query.all()])
    if request.method == 'DELETE':
        db.session.delete(db.session.get(Course, int(request.path.split('/')[-1])))
        db.session.commit(); return jsonify({"msg": "Del"})
    d = request.json
    if request.method == 'POST': db.session.add(Course(name=d['name'], subjects=d['subjects'], teacher_id=d.get('teacher_id')))
    else: 
        c = db.session.get(Course, int(d['id']))
        c.name = d['name']; c.subjects = d['subjects']; c.teacher_id = d.get('teacher_id')
    db.session.commit()
    return jsonify({"msg": "Saved"})

@app.route('/api/fee_structures', methods=['GET', 'POST', 'PUT', 'DELETE'])
@login_required
def fees():
    if request.method == 'GET': return jsonify([f.to_dict() for f in FeeStructure.query.all()])
    if request.method == 'DELETE':
        db.session.delete(db.session.get(FeeStructure, int(request.path.split('/')[-1])))
        db.session.commit(); return jsonify({"msg": "Del"})
    d = request.json
    dt = datetime.strptime(d['due_date'], '%Y-%m-%d').date()
    if request.method == 'POST': db.session.add(FeeStructure(name=d['name'], academic_session_id=d['academic_session_id'], total_amount=d['total_amount'], due_date=dt))
    else:
        f = db.session.get(FeeStructure, int(d['id']))
        f.name = d['name']; f.total_amount = d['total_amount']; f.due_date = dt; f.academic_session_id = d['academic_session_id']
    db.session.commit()
    return jsonify({"msg": "Saved"})

@app.route('/api/payments', methods=['POST'])
@login_required
def pay():
    d = request.json
    p = Payment(student_id=d['student_id'], fee_structure_id=d.get('fee_structure_id'), amount_paid=d['amount_paid'], payment_method=d['payment_method'])
    db.session.add(p); db.session.commit()
    return jsonify({"message": "Recorded", "payment_id": p.id})

@app.route('/api/fee_status', methods=['GET'])
@login_required
def fee_status_api():
    st = User.query.filter_by(role='student').all()
    res = []
    for s in st:
        res.append({"student_id": s.id, "student_name": s.name, **calculate_fee_status(s.id)})
    return jsonify(res)

@app.route('/api/receipt/<int:id>')
def receipt(id):
    p = db.session.get(Payment, id)
    return f"<h1>Receipt #{p.id}</h1><p>Amount: {p.amount_paid}</p><button onclick='window.print()'>Print</button>"

@app.route('/api/announcements', methods=['GET', 'POST'])
def announcements():
    if request.method == 'POST':
        d = request.json
        db.session.add(Announcement(title=d['title'], content=d['content'], target_group=d['target_group']))
        db.session.commit()
        return jsonify({"msg": "Saved"})
    return jsonify([a.to_dict() for a in Announcement.query.all()])

@app.route('/api/reports/admissions', methods=['GET'])
def report_admin():
    d = datetime.utcnow() - timedelta(days=30)
    u = User.query.filter(User.role=='student', User.created_at >= d).all()
    return jsonify([x.to_dict() for x in u])

@app.route('/api/reports/attendance', methods=['GET'])
def report_att_stats():
    s = User.query.filter_by(role='student').all()
    res = []
    for u in s:
        tot = Attendance.query.filter_by(student_id=u.id).count()
        pres = Attendance.query.filter_by(student_id=u.id, status='Present').count()
        pct = round((pres/tot)*100) if tot > 0 else 0
        res.append({"student_name": u.name, "total_classes": tot, "present": pres, "percentage": pct})
    return jsonify(res)

@app.route('/api/reports/performance', methods=['GET'])
def report_perf_stats():
    s = User.query.filter_by(role='student').all()
    res = []
    for u in s:
        g = Grade.query.filter_by(student_id=u.id).all()
        ob = sum(x.marks_obtained for x in g); tot = sum(x.total_marks for x in g)
        pct = round((ob/tot)*100) if tot > 0 else 0
        res.append({"student_name": u.name, "assessments_taken": len(g), "total_score": ob, "overall_percentage": pct})
    return jsonify(res)

@app.route('/uploads/<path:filename>')
def serve_uploads(filename): return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# --- INIT ---
def init_db():
    with app.app_context():
        db.create_all()
        insp = inspect(db.engine)
        cols = [c['name'] for c in insp.get_columns('user')]
        with db.engine.connect() as conn:
            if 'session_id' not in cols: conn.execute(text("ALTER TABLE user ADD COLUMN session_id INTEGER REFERENCES academic_session(id)"))
            if 'admission_number' not in cols: conn.execute(text("ALTER TABLE user ADD COLUMN admission_number VARCHAR(50)"))
            if 'fcm_token' not in cols: conn.execute(text("ALTER TABLE user ADD COLUMN fcm_token VARCHAR(500)"))
            
            fcols = [c['name'] for c in insp.get_columns('fee_structure')]
            if 'course_id' not in fcols: conn.execute(text("ALTER TABLE fee_structure ADD COLUMN course_id INTEGER REFERENCES course(id)"))
            conn.commit()

init_db()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)