# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV FLASK_APP app.py # Set FLASK_APP for flask commands

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code into the container
COPY . .

# New: Create persistent data folder and run the database initialization/migration script.
# This ensures the institute.db file and tables exist before Gunicorn starts.
RUN mkdir -p /app/data
RUN python -c 'from app import initialize_database; initialize_database()'

# Expose the port the app runs on
EXPOSE 8080

# Command to run the application using Gunicorn (production server)
# MODIFIED: Added --workers 3 and --threads 4 to handle blocking I/O (like SMS calls) concurrently, preventing hanging.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "3", "--threads", "4", "app:app"]