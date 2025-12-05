# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV FLASK_APP app.py 

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code into the container
COPY . .

# CRITICAL FIX 1: Create data directory and run initialization script to create DB/tables.
RUN mkdir -p /app/data
RUN python -c 'from app import initialize_database; initialize_database()'

# Expose the port the app runs on
EXPOSE 8080

# CRITICAL FIX 2: Increase Gunicorn concurrency (workers/threads) to prevent hanging during blocking I/O operations (SMS/API calls).
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "3", "--threads", "4", "app:app"]