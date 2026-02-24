from flask import Flask, request, jsonify
import pyodbc
import os
from datetime import datetime

app = Flask(__name__)

# Environment variables
SQL_SERVER = os.environ.get("SQL_SERVER")
SQL_DATABASE = os.environ.get("SQL_DATABASE")
SQL_USERNAME = os.environ.get("SQL_USERNAME")
SQL_PASSWORD = os.environ.get("SQL_PASSWORD")
API_TOKEN = os.environ.get("API_TOKEN")  # Matches Render secret

BATCH_SIZE = 5000  # Optimized for 300k+ rows

# Database connection
def get_sql_connection():
    connection_string = (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={SQL_SERVER};"
        f"DATABASE={SQL_DATABASE};"
        f"UID={SQL_USERNAME};"
        f"PWD={SQL_PASSWORD};"
        "Encrypt=yes;"
        "TrustServerCertificate=yes;"
    )
    conn = pyodbc.connect(connection_string)
    conn.autocommit = False
    return conn

# Auth
def verify_token(req):
    token = req.headers.get("Authorization")
    return token == f"Bearer {API_TOKEN}"

# Health check
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat()
    }), 200

# Insert endpoint
@app.route("/insert-data", methods=["POST"])
def insert_data():
    if not verify_token(request):
        return jsonify({"statusCode": 401, "body": "Unauthorized"}), 401

    try:
        payload = request.get_json()
        if not payload or "data" not in payload:
            return jsonify({"statusCode": 400, "body": "Invalid payload format"}), 400

        rows_data = payload["data"]
        target_table = request.args.get("table")
        columns_param = request.args.get("columns")
        if not target_table or not columns_param:
            return jsonify({"statusCode": 400, "body": "Missing table or columns parameter"}), 400

        if not rows_data:
            return jsonify({"data": []}), 200

        columns = [col.strip() for col in columns_param.split(",")]
        columns_str = ", ".join(f"[{col}]" for col in columns)
        placeholders = ", ".join("?" for _ in columns)
        insert_sql = f"INSERT INTO {target_table} ({columns_str}) VALUES ({placeholders})"

        conn = get_sql_connection()
        cursor = conn.cursor()
        cursor.fast_executemany = True

        total_inserted = 0
        results = []
        batch_data = []

        for row in rows_data:
            row_num = row[0]
            data_values = row[1:]
            if len(data_values) != len(columns):
                results.append([row_num, "ERROR", "Column mismatch"])
                continue

            batch_data.append(tuple(data_values))
            results.append([row_num, "SUCCESS", None])

            if len(batch_data) >= BATCH_SIZE:
                cursor.executemany(insert_sql, batch_data)
                conn.commit()
                total_inserted += len(batch_data)
                batch_data = []

        if batch_data:
            cursor.executemany(insert_sql, batch_data)
            conn.commit()
            total_inserted += len(batch_data)

        cursor.close()
        conn.close()

        return jsonify({"data": results}), 200

    except Exception as e:
        return jsonify({"statusCode": 500, "body": f"Server error: {str(e)}"}), 500

# App start
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
