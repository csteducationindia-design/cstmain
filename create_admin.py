from app import app, db, User, bcrypt

def create_admin_user():
    with app.app_context():
        # Add this line to create all tables if they don't exist yet
        db.create_all()

        # Check if an admin already exists
        if User.query.filter_by(role='admin').first():
            print("An admin user already exists.")
            return

        # Get details from user
        email = input("Enter admin email: ")
        password = input("Enter admin password: ")
        name = input("Enter admin name: ")

        # Hash the password
        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')

        # Create and save the new admin user
        admin = User(email=email, password=hashed_password, name=name, role='admin')
        db.session.add(admin)
        db.session.commit()
        print("Admin user created successfully!")

if __name__ == '__main__':
    create_admin_user()