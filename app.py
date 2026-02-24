from flask import Flask, request, jsonify
import pymssql
import os
import pandas as pd
import numpy as np
from datetime import datetime

app = Flask(__name__)

# ===============================
# ENVIRONMENT VARIABLES
# ===============================
SQL_SERVER = os.environ.get("SQL_SERVER")
SQL_DATABASE = os.environ.get("SQL_DATABASE")
SQL_USERNAME = os.environ.get("SQL_USERNAME")
SQL_PASSWORD = os.environ.get("SQL_PASSWORD")
API_TOKEN = os.environ.get("API_TOKEN")

BATCH_SIZE = 5000  # Works safely with 300k+ rows

# ===============================
# DATABASE CONNECTION
# ===============================
def get_sql_connection():
    conn = pymssql.connect(
        server=SQL_SERVER,
        user=SQL_USERNAME,
        password=SQL_PASSWORD,
        database=SQL_DATABASE,
        charset='UTF-8'
    )
    return conn

# ===============================
# AUTH
# ===============================
def verify_token(req):
    token = req.headers.get("Authorization")
    return token == f"Bearer {API_TOKEN}"

# ===============================
# HEALTH CHECK
# ===============================
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat()
    }), 200

# ===============================
# INSERT ENDPOINT
# ===============================
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

        print(f"Table: {target_table}")
        print(f"Incoming rows: {len(rows_data)}")

        # Prepare SQL insert
        columns = [col.strip() for col in columns_param.split(",")]
        columns_str = ", ".join(f"[{col}]" for col in columns)
        placeholders = ", ".join("%s" for _ in columns)  # pymssql uses %s

        insert_sql = f"INSERT INTO {target_table} ({columns_str}) VALUES ({placeholders})"

        conn = get_sql_connection()
        cursor = conn.cursor()

        total_inserted = 0
        results = []
        batch_data = []

        for row in rows_data:
            row_num = row[0]
            data_values = row[1:]

            if len(data_values) != len(columns):
                results.append([row_num, "ERROR", "Column mismatch"])
                continue

            # Convert NaN / pd.NaT to None for SQL
            clean_values = [None if pd.isna(v) else v for v in data_values]
            batch_data.append(tuple(clean_values))
            results.append([row_num, "SUCCESS", None])

            # Insert in batches
            if len(batch_data) >= BATCH_SIZE:
                cursor.executemany(insert_sql, batch_data)
                conn.commit()
                total_inserted += len(batch_data)
                print(f"Inserted batch. Total so far: {total_inserted}")
                batch_data = []

        # Insert remaining rows
        if batch_data:
            cursor.executemany(insert_sql, batch_data)
            conn.commit()
            total_inserted += len(batch_data)

        cursor.close()
        conn.close()

        print(f"Insert complete. Total inserted: {total_inserted}")
        return jsonify({"data": results}), 200

    except Exception as e:
        print(f"Critical error: {str(e)}")
        return jsonify({
            "statusCode": 500,
            "body": f"Server error: {str(e)}"
        }), 500

# ===============================
# APP START
# ===============================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
