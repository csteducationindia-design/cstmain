from app import app, db

print("Creating ExamResult table...")
with app.app_context():
    db.create_all() # This creates ONLY the missing tables
    print("✅ Success! ExamResult table created.")