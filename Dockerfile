FROM python:3.11-slim

# Install dependencies for ODBC driver
RUN apt-get update && apt-get install -y \
    curl gnupg2 unixodbc unixodbc-dev gcc g++ build-essential libssl1.1 libkrb5-3 \
    && rm -rf /var/lib/apt/lists/*

# Add Microsoft repository
RUN curl https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /usr/share/keyrings/microsoft.gpg \
    && curl https://packages.microsoft.com/config/debian/11/prod.list > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 10000

CMD ["gunicorn", "app:app", "--workers", "2", "--timeout", "300", "--bind", "0.0.0.0:10000"]
