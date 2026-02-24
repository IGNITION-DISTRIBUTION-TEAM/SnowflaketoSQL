# Use slim Python image
FROM python:3.11-slim

# Install system dependencies for pymssql
RUN apt-get update && apt-get install -y \
    freetds-dev \
    gcc \
    g++ \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy app files
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port
EXPOSE 10000

# Run Flask with gunicorn
CMD ["gunicorn", "app:app", "--workers", "2", "--timeout", "300", "--bind", "0.0.0.0:10000"]
