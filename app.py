from flask import Flask, request, jsonify, render_template, redirect, url_for, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from flask_mail import Mail, Message as MailMessage
import os
from datetime import datetime, timedelta, date
import csv
from io import StringIO
import requests
import uuid 
from werkzeug.utils import secure_filename 
import urllib.parse 
from sqlalchemy import or_ # For search queries

# --- Basic Setup ---
app = Flask(__name__, template_folder='templates')
app.config['SECRET_KEY'] = 'a_very_secret_key_that_should_be_changed'
CORS(app, supports_credentials=True)

# --- Email Configuration ---
# !! IMPORTANT: Use environment variables or a secure config method in production !!
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', 'your_email@gmail.com')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', 'your_google_app_password')
mail = Mail(app)

# --- Database Configuration ---
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'institute.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

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

# --- Third-Party SMS API Configuration ---
SMS_API_URL = os.environ.get('SMS_API_URL', 'YOUR_THIRD_PARTY_SMS_API_ENDPOINT_URL')
SMS_API_USER_ID = os.environ.get('SMS_API_USER_ID', 'YOUR_API_USER_ID')
SMS_API_PASSWORD = os.environ.get('SMS_API_PASSWORD', 'YOUR_API_PASSWORD')
# SMS_API_SENDER_ID = os.environ.get('SMS_API_SENDER_ID', 'YOUR_SENDER_ID')


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
    
    # *** FIX IS HERE: Added db.ForeignKey('user.id') ***
    parent_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True) 
    
    can_edit = db.Column(db.Boolean, default=True) # Admin permission flag
    
    # --- EXPANDED STUDENT FIELDS ---
    dob = db.Column(db.String(20), nullable=True) # Date of Birth (YYYY-MM-DD)
    profile_photo_url = db.Column(db.String(300), nullable=True) # Will store path like /uploads/filename.png
    gender = db.Column(db.String(20), nullable=True)
    father_name = db.Column(db.String(100), nullable=True)
    mother_name = db.Column(db.String(100), nullable=True)
    address_line1 = db.Column(db.String(200), nullable=True)
    city = db.Column(db.String(100), nullable=True)
    state = db.Column(db.String(100), nullable=True)
    pincode = db.Column(db.String(20), nullable=True)
    # --- END EXPANDED FIELDS ---
    
    # This relationship now works because parent_id is a ForeignKey
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
    total_amount = db.Column(db.Float, nullable=False)
    due_date = db.Column(db.Date, nullable=False, default=date.today)

    def to_dict(self):
        """Serializes FeeStructure object to dictionary."""
        session = db.session.get(AcademicSession, self.academic_session_id)
        return {
            "id": self.id, "name": self.name,
            "academic_session_id": self.academic_session_id,
            "session_name": session.name if session else "N/A",
            "total_amount": self.total_amount,
            "due_date": self.due_date.strftime('%Y-%m-%d')
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
            "filename": self.filename, # The safe, unique name for downloading
            "original_filename": self.original_filename, # The display name
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
    """Sends an actual SMS notification for fee pending using a third-party API."""
    if user and user.phone_number:
        message = f"ALERT: Dear {user.name}, your tuition fee of INR {balance:.2f} is pending. Due date: {due_date}. Please pay immediately."
        return send_actual_sms(user.phone_number, message)
    return False

def send_actual_sms(phone_number, message_body):
    """Sends an SMS using the configured third-party API."""
    if not phone_number: # Basic check
        print("--- [SMS FAILED] No phone number provided. ---")
        return False

    if not SMS_API_URL or not SMS_API_USER_ID or not SMS_API_PASSWORD or \
       SMS_API_URL == 'YOUR_THIRD_PARTY_SMS_API_ENDPOINT_URL': # Check if placeholders are still used
        print("--- [SMS ERROR] API Credentials/URL not configured or using placeholders. Cannot send actual SMS. ---")
        return False # Indicate failure if not configured

    payload = {
        'userid': SMS_API_USER_ID,
        'password': SMS_API_PASSWORD,
        'mobile': phone_number,
        'msg': message_body,
        # 'senderid': SMS_API_SENDER_ID, 
    }

    try:
        print(f"--- [SMS] Attempting to send to {phone_number} via {SMS_API_URL} ---")
        response = requests.get(SMS_API_URL, params=payload, timeout=10) 
        response.raise_for_status() 

        if response.status_code == 200:
            print(f"--- [SMS SUCCESS] API Response: {response.text[:100]}... ---")
            return True
        else:
            print(f"--- [SMS FAILED] API returned status {response.status_code}: {response.text[:100]}... ---")
            return False
    except requests.exceptions.RequestException as e:
        print(f"--- [SMS FAILED] Network/API Error for {phone_number}: {e} ---")
        return False
    except Exception as e:
        print(f"--- [SMS FAILED] General Error for {phone_number}: {e} ---")
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


def calculate_fee_status(student_id):
    """Calculates fee status including pending days."""
    payments = Payment.query.filter_by(student_id=student_id).all()
    total_paid = sum(p.amount_paid for p in payments)

    active_fee_structure = FeeStructure.query.order_by(FeeStructure.id.desc()).first()

    if active_fee_structure:
        total_due = active_fee_structure.total_amount
        due_date = active_fee_structure.due_date
    else:
        total_due = 0.00
        due_date = date.today() 

    balance = total_due - total_paid

    try:
        if balance > 0 and isinstance(due_date, date):
            today = date.today()
            if today > due_date:
                pending_days = (today - due_date).days * -1 
            else:
                pending_days = (due_date - today).days 
        else:
            pending_days = 0 
    except TypeError: 
        pending_days = 0

    return {
        "total_due": total_due,
        "total_paid": total_paid,
        "balance": balance,
        "due_date": due_date.strftime('%Y-%m-%d'),
        "pending_days": pending_days
    }


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

    # 2. Process Students, linking to parents created above or existing parents
    for row in rows_to_process:
        role = row.get('role', '').strip().lower()
        if role == 'student':
            try:
                name = row['name'].strip()
                email = row['email'].strip()
                password = row['password'].strip()
                phone_number = row.get('phone_number', '').strip() or None
                # --- NEW FIELDS FOR BULK UPLOAD ---
                dob = row.get('dob', '').strip() or None 
                profile_photo_url = row.get('profile_photo_url', '').strip() or None 
                gender = row.get('gender', '').strip() or None
                father_name = row.get('father_name', '').strip() or None
                mother_name = row.get('mother_name', '').strip() or None
                address_line1 = row.get('address_line1', '').strip() or None
                city = row.get('city', '').strip() or None
                state = row.get('state', '').strip() or None
                pincode = row.get('pincode', '').strip() or None
                course_ids_str = row.get('course_ids', '').strip() # NEW: Course IDs for enrollment
                # --- END NEW FIELDS ---

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
                
                # NEW: Enroll student in courses
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
                profile_photo_url = f"/uploads/{unique_filename}" # Store the URL/path

        new_user = User(
            name=data['name'], email=data['email'], password=hashed_password, role=data['role'],
            phone_number=data.get('phone_number'),
            parent_id=int(data.get('parent_id')) if data.get('parent_id') else None,
            can_edit=False if data['role'] == 'admin' else True,
            dob=data.get('dob'),
            profile_photo_url=profile_photo_url, # Save the path
            gender=data.get('gender'),
            father_name=data.get('father_name'),
            mother_name=data.get('mother_name'),
            address_line1=data.get('address_line1'),
            city=data.get('city'),
            state=data.get('state'),
            pincode=data.get('pincode')
        )
        db.session.add(new_user)
        
        # --- START FIX (POST) ---
        # Look for 'course_ids' (plural) from FormData
        if new_user.role == 'student':
            course_id_str = request.form.get('course_ids') 
            if course_id_str:
                course = db.session.get(Course, int(course_id_str))
                if course:
                    new_user.courses_enrolled.append(course)
        # --- END FIX (POST) ---
                
        db.session.commit()
        return jsonify(new_user.to_dict()), 201

    # PUT (Update User)
    if request.method == 'PUT':
        data = request.form
        user_id = data.get('id')
        user = db.session.get(User, user_id)
        if not user:
            return jsonify({"message": "User not found"}), 404

        if 'email' in data and data['email'] != user.email:
            if User.query.filter(User.email == data['email'], User.id != int(user_id)).first():
                return jsonify({"message": "Email address already exists for another user"}), 400

        user.name = data.get('name', user.name)
        user.email = data.get('email', user.email)
        user.phone_number = data.get('phone_number', user.phone_number)
        user.role = data.get('role', user.role)
        if 'parent_id' in data: 
             user.parent_id = int(data.get('parent_id')) if data.get('parent_id') else None
        
        user.dob = data.get('dob', user.dob)
        user.gender = data.get('gender', user.gender)
        user.father_name = data.get('father_name', user.father_name)
        user.mother_name = data.get('mother_name', user.mother_name)
        user.address_line1 = data.get('address_line1', user.address_line1)
        user.city = data.get('city', user.city)
        user.state = data.get('state', user.state)
        user.pincode = data.get('pincode', user.pincode)

        # --- START FIX (PUT) ---
        # Look for 'course_ids' (plural) and use db.session.get
        if user.role == 'student':
            course_id_str = request.form.get('course_ids')
            
            # Clear existing enrollments
            user.courses_enrolled = []
            
            if course_id_str:
                course = db.session.get(Course, int(course_id_str)) # Fixed LegacyAPIWarning
                if course:
                    user.courses_enrolled.append(course)
        # --- END FIX (PUT) ---
            
        new_password = data.get('password')
        if new_password:
            user.password = bcrypt.generate_password_hash(new_password).decode('utf-8')

        db.session.commit()
        return jsonify(user.to_dict()), 200

    # MODIFIED GET: Add search capability
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

# NEW: Endpoint to handle profile photo upload separately
@app.route('/api/user/upload_photo/<int:user_id>', methods=['POST'])
@login_required
def upload_profile_photo(user_id):
    if current_user.role != 'admin':
        return jsonify({"message": "Access denied"}), 403
        
    user = db.session.get_or_404(User, user_id)
    
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

    if user.role == 'student':
        Payment.query.filter_by(student_id=user_id).delete()
        Grade.query.filter_by(student_id=user_id).delete()
        Attendance.query.filter_by(student_id=user_id).delete()
        Message.query.filter_by(recipient_id=user_id).delete() 
        Message.query.filter_by(sender_id=user_id).delete()
        # Remove course associations
        user.courses_enrolled = []
    elif user.role == 'parent':
        User.query.filter_by(parent_id=user_id).update({"parent_id": None})
        Message.query.filter_by(recipient_id=user_id).delete()
        Message.query.filter_by(sender_id=user_id).delete()
    elif user.role == 'teacher':
        Course.query.filter_by(teacher_id=user_id).update({"teacher_id": None})
        Message.query.filter_by(recipient_id=user_id).delete()
        Message.query.filter_by(sender_id=user_id).delete()


    db.session.delete(user)
    db.session.commit()
    return jsonify({'message': 'User deleted'})

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
            if send_actual_sms(user.phone_number, f"{subject}: {body}"):
                notification_sent = True
        elif notify_type == 'whatsapp':
            if send_mock_whatsapp(user, subject, body):
                notification_sent = True

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

@app.route('/api/courses', methods=['GET', 'POST', 'PUT']) # ADDED PUT
@login_required
def manage_courses():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    
    if request.method == 'POST':
        data = request.get_json()
        teacher_id = data.get('teacher_id')
        new_course = Course(
            name=data['name'],
            subjects=data['subjects'],
            teacher_id=int(teacher_id) if teacher_id else None
        )
        db.session.add(new_course)
        db.session.commit()
        return jsonify(new_course.to_dict()), 201
    
    if request.method == 'PUT': # NEW: Update Course
        data = request.get_json()
        course_id = data.get('id')
        course = db.session.get(Course, course_id)
        if not course:
            return jsonify({"message": "Course not found"}), 404

        course.name = data.get('name', course.name)
        course.subjects = data.get('subjects', course.subjects)
        course.teacher_id = int(data.get('teacher_id')) if data.get('teacher_id') else None
        
        db.session.commit()
        return jsonify(course.to_dict()), 200
        
    return jsonify([c.to_dict() for c in Course.query.all()])

@app.route('/api/courses/<int:course_id>', methods=['DELETE'])
@login_required
def delete_course(course_id):
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    course = db.session.get_or_404(Course, course_id)
    Grade.query.filter_by(course_id=course_id).delete()
    db.session.delete(course)
    db.session.commit()
    return jsonify({'message': 'Course deleted successfully'})

@app.route('/api/sessions', methods=['GET', 'POST', 'PUT']) # ADDED PUT
@login_required
def manage_sessions():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    
    if request.method == 'POST':
        data = request.get_json()
        new_session = AcademicSession(
            name=data['name'],
            start_date=data['start_date'],
            end_date=data['end_date'],
            status=data['status']
        )
        db.session.add(new_session)
        db.session.commit()
        return jsonify(new_session.to_dict()), 201

    if request.method == 'PUT': # NEW: Update Session
        data = request.get_json()
        session_id = data.get('id')
        session = db.session.get(AcademicSession, session_id)
        if not session:
            return jsonify({"message": "Session not found"}), 404
            
        session.name = data.get('name', session.name)
        session.start_date = data.get('start_date', session.start_date)
        session.end_date = data.get('end_date', session.end_date)
        session.status = data.get('status', session.status)

        db.session.commit()
        return jsonify(session.to_dict()), 200

    return jsonify([s.to_dict() for s in AcademicSession.query.all()])

@app.route('/api/sessions/<int:session_id>', methods=['DELETE'])
@login_required
def delete_session(session_id):
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    session = db.session.get_or_404(AcademicSession, session_id)
    linked_fees = FeeStructure.query.filter_by(academic_session_id=session_id).count()
    if linked_fees > 0:
        return jsonify({'message': f'Cannot delete session. It is linked to {linked_fees} fee structure(s).'}), 400

    db.session.delete(session)
    db.session.commit()
    return jsonify({'message': 'Academic session deleted successfully'})

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

@app.route('/api/fee_structures', methods=['GET', 'POST', 'PUT']) # ADDED PUT
@login_required
def manage_fee_structures():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    
    if request.method == 'POST':
        data = request.get_json()
        try:
            due_date_obj = datetime.strptime(data['due_date'], '%Y-%m-%d').date()
        except ValueError:
            return jsonify({"message": "Invalid date format. Use YYYY-MM-DD."}), 400

        new_structure = FeeStructure(
            name=data['name'],
            academic_session_id=data['academic_session_id'],
            total_amount=data['total_amount'],
            due_date=due_date_obj
        )
        db.session.add(new_structure)
        db.session.commit()
        return jsonify(new_structure.to_dict()), 201

    if request.method == 'PUT': # NEW: Update Fee Structure
        data = request.get_json()
        fee_id = data.get('id')
        structure = db.session.get(FeeStructure, fee_id)
        if not structure:
            return jsonify({"message": "Fee structure not found"}), 404
            
        try:
            due_date_obj = datetime.strptime(data['due_date'], '%Y-%m-%d').date()
        except ValueError:
            return jsonify({"message": "Invalid date format. Use YYYY-MM-DD."}), 400

        structure.name = data.get('name', structure.name)
        structure.academic_session_id = data.get('academic_session_id', structure.academic_session_id)
        structure.total_amount = data.get('total_amount', structure.total_amount)
        structure.due_date = due_date_obj

        db.session.commit()
        return jsonify(structure.to_dict()), 200
        
    return jsonify([s.to_dict() for s in FeeStructure.query.order_by(FeeStructure.id.desc()).all()])

@app.route('/api/payments', methods=['POST'])
@login_required
def record_payment():
    if current_user.role != 'admin': return jsonify({"message": "Access denied"}), 403
    data = request.get_json()

    if not data.get('student_id') or not data.get('fee_structure_id') or not data.get('amount_paid') or not data.get('payment_method'):
         return jsonify({"message": "Missing required payment fields."}), 400

    new_payment = Payment(
        student_id=data['student_id'],
        fee_structure_id=data['fee_structure_id'],
        amount_paid=data['amount_paid'],
        payment_method=data['payment_method']
    )
    db.session.add(new_payment)
    db.session.commit() 

    receipt_message = "Receipt link available." 

    status = calculate_fee_status(data['student_id'])
    student = db.session.get(User, data['student_id'])

    if student and status['balance'] > 0: 
        parent = User.query.get(student.parent_id) if student.parent_id else None
        send_fee_alert_sms(student, status['balance'], status['due_date'])
        if parent:
            send_fee_alert_sms(parent, status['balance'], status['due_date'])

    return jsonify({"message": f"Payment recorded. {receipt_message}", "payment_id": new_payment.id}), 201


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

    student_alerted = send_fee_alert_sms(student, status['balance'], status['due_date'])

    parent_alerted = False
    parent = db.session.get(User, student.parent_id) if student and student.parent_id else None
    if parent:
        parent_alerted = send_fee_alert_sms(parent, status['balance'], status['due_date'])

    message = f"Alert sent to Student ({'Yes' if student_alerted else 'No Phone/Error'})."
    if parent:
        message += f" Alert sent to Parent ({'Yes' if parent_alerted else 'No Phone/Error'})."

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


# --- Student API Endpoints ---
@app.route('/api/student/fees', methods=['GET'])
@login_required
def get_student_fees():
    if current_user.role != 'student': return jsonify({"message": "Access denied"}), 403

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

@app.route('/api/student/attendance', methods=['GET'])
@login_required
def get_student_attendance():
    if current_user.role != 'student': return jsonify({"message": "Access denied"}), 403
    attendance_records = Attendance.query.filter_by(student_id=current_user.id).order_by(Attendance.check_in_time.desc()).limit(10).all()
    return jsonify([{
        "date": r.check_in_time.strftime('%Y-%m-%d'),
        "time": r.check_in_time.strftime('%I:%M %p'),
        "status": r.status
    } for r in attendance_records])

@app.route('/api/student/grades', methods=['GET'])
@login_required
def get_student_grades():
    if current_user.role != 'student': return jsonify({"message": "Access denied"}), 403
    grades = Grade.query.filter_by(student_id=current_user.id).order_by(Grade.id.desc()).all()
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

@app.route('/api/student/notes', methods=['GET'])
@login_required
def get_student_notes():
    if current_user.role != 'student':
        return jsonify({"message": "Access denied"}), 403
    
    # NEW: Students now see notes for courses they are enrolled in
    student_course_ids = [c.id for c in current_user.courses_enrolled]
    notes = SharedNote.query.filter(SharedNote.course_id.in_(student_course_ids)).order_by(SharedNote.created_at.desc()).all()
    
    return jsonify([note.to_dict() for note in notes])


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
    if current_user.role != 'teacher': return jsonify({"message": "Access denied"}), 403
    data = request.get_json()
    attendance_date = datetime.strptime(data['date'], '%Y-%m-%d').date()
    attendance_data = data['attendance_data']

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

    db.session.commit()
    return jsonify({"message": f"Attendance recorded for {len(attendance_data)} students on {data['date']}."}), 201

@app.route('/api/teacher/notify', methods=['POST'])
@login_required
def send_notification_to_student():
    if current_user.role not in ['teacher', 'admin']: return jsonify({"message": "Access denied"}), 403
    data = request.get_json()
    student_id = data.get('student_id')
    subject = data.get('subject')
    body = data.get('body')
    notify_type = data.get('type')

    student = db.session.get(User, student_id)
    if not student or student.role != 'student':
        return jsonify({"message": "Student not found."}), 404

    parent = db.session.get(User, student.parent_id) if student.parent_id else None

    result_messages = []

    # 1. Store as a Message (Database Record)
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

    # 2. External Notification (Actual/Mock)
    if notify_type == 'email':
        try:
            msg = MailMessage(subject=subject,
                              sender=app.config['MAIL_USERNAME'],
                              recipients=[student.email])
            if parent and parent.email:
                msg.recipients.append(parent.email)
            msg.body = body
            # mail.send(msg) # Uncomment to send real emails
            print(f"--- [MOCK EMAIL SENT] to {student.email} and Parent ---\nSubject: {subject}\nBody:\n{body}\n--------------------------------")
            result_messages.append("Email sent successfully (mocked).")
        except Exception as e:
            print(f"Flask-Mail Error (Ignored for DB entry): {e}")
            result_messages.append(f"Email failed to send: {str(e)}.")

    elif notify_type == 'sms':
        if send_actual_sms(student.phone_number, f"{subject}: {body}"):
             result_messages.append(f"SMS sent to Student ({student.phone_number}).")
        else:
             result_messages.append(f"SMS to Student failed (No Phone/API Error).")
        if parent:
            if send_actual_sms(parent.phone_number, f"Re: {student.name} - {subject}: {body}"):
                 result_messages.append(f"SMS sent to Parent ({parent.phone_number}).")
            else:
                 result_messages.append(f"SMS to Parent failed (No Phone/API Error).")


    elif notify_type == 'whatsapp':
        if send_mock_whatsapp(student, subject, body):
            result_messages.append(f"WhatsApp sent to Student ({student.phone_number}).")
        else:
            result_messages.append(f"WhatsApp to Student failed (No phone).")
        if parent and send_mock_whatsapp(parent, subject, body):
            result_messages.append(f"WhatsApp sent to Parent ({parent.phone_number}).")
        elif parent:
            result_messages.append(f"WhatsApp to Parent failed (No phone).")

    else:
        result_messages.append("No external notification type specified/valid.")


    return jsonify({"message": " | ".join(result_messages)}), 200

@app.route('/api/teacher/email', methods=['POST'])
@login_required
def send_email_wrapper():
    if current_user.role != 'teacher': return jsonify({"message": "Access denied"}), 403
    data = request.get_json()
    data['type'] = 'email' 
    return send_notification_to_student()


@app.route('/api/teacher/reports/attendance', methods=['GET'])
@login_required
def teacher_attendance_report():
    if current_user.role != 'teacher':
        return jsonify({"message": "Access denied"}), 403

    # Filter students by courses taught by the current teacher
    teacher_course_ids = [c.id for c in Course.query.filter_by(teacher_id=current_user.id).all()]
    
    if not teacher_course_ids:
        return jsonify([])

    enrolled_students = User.query.join(student_course_association).join(Course).filter(
        User.role == 'student',
        Course.id.in_(teacher_course_ids)
    ).distinct().all()

    report = []

    for student in enrolled_students:
        total_classes = Attendance.query.filter_by(student_id=student.id).count()
        present_records = Attendance.query.filter_by(student_id=student.id).filter(
            Attendance.status.in_(['Present', 'Checked-In'])
        ).count()
        absent_records = total_classes - present_records

        percentage = round((present_records / total_classes) * 100) if total_classes > 0 else 0

        report.append({
            "student_id": student.id,
            "student_name": student.name,
            "phone_number": student.phone_number,
            "total_classes": total_classes,
            "present": present_records,
            "absent": absent_records,
            "percentage": percentage
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
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))
            
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
            
            return jsonify({"message": "File uploaded and shared successfully!", "note": new_note.to_dict()}), 201
            
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
        
    if '..' in filename or filename.startswith('/'):
        return "Invalid filename", 400

    try:
        # Security check: Ensure file is in a known table (Notes or Users)
        note = SharedNote.query.filter_by(filename=filename).first()
        user = User.query.filter_by(profile_photo_url=f"/uploads/{filename}").first()
        
        if not note and not user:
             # If file isn't tracked in DB, don't serve it
             return "File not found or unauthorized", 404
             
        download_name = note.original_filename if note else filename
        
        return send_from_directory(
            app.config['UPLOAD_FOLDER'],
            filename,
            as_attachment=True if note else False, # Download notes, display photos
            download_name=download_name if note else None
        )
    except FileNotFoundError:
        return "File not found.", 404


# --- Parent API Endpoints ---
@app.route('/api/parent/children', methods=['GET'])
@login_required
def get_parent_children():
    if current_user.role != 'parent': return jsonify({"message": "Access denied"}), 403
    children = User.query.filter(User.parent_id == current_user.id).all() # FIX: Use direct parent_id comparison
    return jsonify([c.to_dict() for c in children])

@app.route('/api/parent/messages', methods=['GET'])
@login_required
def get_parent_messages():
    if current_user.role != 'parent': return jsonify({"message": "Access denied"}), 403
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


@app.route('/api/parent/child_data/<int:student_id>', methods=['GET'])
@login_required
def get_child_data(student_id):
    if current_user.role not in ['parent', 'admin']: return jsonify({"message": "Access denied"}), 403
    student = db.session.get_or_404(User, student_id)

    if student.role != 'student' or (current_user.role == 'parent' and student.parent_id != current_user.id):
        return jsonify({"message": "Authorization error or not a valid child."}), 403

    attendance_records = Attendance.query.filter_by(student_id=student.id).order_by(Attendance.check_in_time.desc()).limit(10).all()
    attendance_data = [{
        "date": r.check_in_time.strftime('%Y-%m-%d'),
        "time": r.check_in_time.strftime('%I:%M %p'),
        "status": r.status
    } for r in attendance_records]

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

    fee_status = calculate_fee_status(student.id)

    return jsonify({
        "profile": student.to_dict(),
        "attendance": attendance_data,
        "grades": grade_data,
        "fees": fee_status
    })

# --- Receipt Route ---
@app.route('/api/receipt/<int:payment_id>', methods=['GET'])
@login_required
def serve_receipt(payment_id):
    payment = db.session.get_or_404(Payment, payment_id)
    student = db.session.get(User, payment.student_id)
    fee_structure = db.session.get(FeeStructure, payment.fee_structure_id)

    if not student:
         return "Student record not found for this payment.", 404

    if current_user.role != 'admin' and (not student or (current_user.id != student.parent_id and current_user.id != student.id)):
         return "Access Denied", 403

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
            .header {{
                text-align: center;
                border-bottom: 2px solid #333;
                margin-bottom: 20px;
                padding-bottom: 10px;
            }}
            .header h1 {{
                margin: 0;
                color: #333;
            }}
            .header p {{
                margin: 5px 0 0;
                color: #666;
            }}
            .receipt-details {{
                margin-bottom: 30px;
            }}
            .receipt-details h2 {{
                color: #333;
                border-bottom: 1px solid #eee;
                padding-bottom: 5px;
                margin-bottom: 15px;
                font-size: 1.4em;
            }}
            .receipt-details table {{
                width: 100%;
                border-collapse: collapse;
            }}
            .receipt-details th, .receipt-details td {{
                padding: 10px;
                text-align: left;
                border-bottom: 1px solid #eee;
                font-size: 0.95em;
            }}
            .receipt-details th {{
                background-color: #f9f9f9;
                width: 30%;
                color: #555;
            }}
            .receipt-details td {{
                color: #333;
            }}
            .total {{
                margin-top: 20px;
                text-align: right;
            }}
            .total strong {{
                font-size: 1.2em;
                color: #000;
            }}
            .footer {{
                margin-top: 40px;
                text-align: center;
                font-size: 0.8em;
                color: #999;
            }}
            @media print {{
              body {{ background-color: #fff; }}
              .container {{ border: none; box-shadow: none; width: 100%; max-width: 100%; margin: 0; padding: 0; }}
              .no-print {{ display: none; }}
            }}
            .print-button {{
                 display: block; width: 100px; margin: 20px auto; padding: 10px; background-color: #007bff; color: white; border: none; border-radius: 5px; cursor: pointer; text-align: center;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>CST Institute</h1>
                <p>Your Institute Address Here, City, State, Pin Code</p>
                <p>Contact: your_phone | Email: your_email@example.com</p>
            </div>

            <div class="receipt-details">
                <h2>Payment Receipt</h2>
                <table>
                    <tr><th>Receipt No:</th><td>#{payment.id}</td></tr>
                    <tr><th>Date:</th><td>{payment.payment_date.strftime('%d-%b-%Y %I:%M %p')}</td></tr>
                    <tr><th>Student Name:</th><td>{student.name}</td></tr>
                    <tr><th>Fee For:</th><td>{fee_structure.name if fee_structure else 'N/A'}</td></tr>
                    <tr><th>Payment Method:</th><td>{payment.payment_method}</td></tr>
                    <tr class="total-row"><th>Amount Paid:</th><td><strong>₹ {payment.amount_paid:.2f}</strong></td></tr>
                </table>
            </div>

            <button class="print-button no-print" onclick="window.print()">Print Receipt</button>

            <div class="footer">
                <p>This is a computer-generated receipt.</p>
                <p>Thank you!</p>
            </div>
        </div>
    </body>
    </html>
    """
    return html_content


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
    if current_user.role == 'parent': return render_template('parent.html')
    else: return redirect(url_for('serve_login_page'))


# --- Run Application ---
if __name__ == '__main__':
    with app.app_context():
        db.create_all() # Ensure tables are created (including new/modified User and SharedNote tables)
    app.run(debug=True, host='0.0.0.0', port=5000)
