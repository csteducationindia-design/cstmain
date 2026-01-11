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

# Copy the rest of your application code
COPY . .

# Create data directory (DB initialization happens in app.py now)
RUN mkdir -p /app/data

# Expose the port
EXPOSE 8080

# Start the application using Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "3", "--threads", "4", "app:app"]