from app import app, db
from sqlalchemy import text

print("Attempting to fix database...")

with app.app_context():
    try:
        # This raw SQL command adds the missing column to your existing database
        db.session.execute(text("ALTER TABLE user ADD COLUMN hall_ticket_blocked BOOLEAN DEFAULT 0"))
        db.session.commit()
        print("✅ SUCCESS: Column 'hall_ticket_blocked' was added to your database.")
        print("You can now start your server!")
    except Exception as e:
        print(f"⚠️  Note: {e}")
        print("If the error says 'duplicate column', it means you already fixed it.")