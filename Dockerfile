FROM mcr.microsoft.com/mssql-tools:17.10.2.1-ubuntu-22.04

WORKDIR /app
COPY . .

# Install Python 3.11 and pip
RUN apt-get update && \
    apt-get install -y python3.11 python3.11-venv python3-pip && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN python3.11 -m pip install --no-cache-dir -r requirements.txt

EXPOSE 10000

CMD ["gunicorn", "app:app", "--workers", "2", "--timeout", "300", "--bind", "0.0.0.0:10000"]
