# Dockerfile
FROM python:3.11-slim

# Install system dependencies & ODBC driver dependencies
RUN apt-get update && apt-get install -y \
    curl gnupg2 unixodbc unixodbc-dev gcc g++ build-essential libssl1.1 libkrb5-3 \
    && rm -rf /var/lib/apt/lists/*

# Add Microsoft repo and install ODBC Driver 18
RUN curl https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /usr/share/keyrings/microsoft.gpg \
    && curl https://packages.microsoft.com/config/debian/11/prod.list > /etc/apt/sources.list.d/mssql-release.list

RUN apt-get update && ACCEPT_EULA=Y apt-get install -y msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port for Render
EXPOSE 10000

# Run the app
CMD ["gunicorn", "app:app", "--workers", "2", "--timeout", "300", "--bind", "0.0.0.0:10000"]
