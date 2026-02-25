from flask import Flask, request, jsonify
import pymssql
import os
import pandas as pd
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

CONNECTION_TIMEOUT = 60

# ===============================
# DATABASE CONNECTION
# ===============================
def get_sql_connection():
    conn = pymssql.connect(
        server=SQL_SERVER,
        user=SQL_USERNAME,
        password=SQL_PASSWORD,
        database=SQL_DATABASE,
        charset='UTF-8',
        timeout=CONNECTION_TIMEOUT,
        login_timeout=30
    )
    return conn

# ===============================
# AUTH
# ===============================
def verify_token(req):
    token = req.headers.get("Authorization")
    return token == f"Bearer {API_TOKEN}"

# ===============================
# SAFE BATCH SIZE CALCULATOR
# SQL Server 2012 has a 2100 parameter limit per query
# ===============================
def get_batch_size(num_columns):
    max_params = 2000  # stay under 2100 limit with some buffer
    return max(1, max_params // num_columns)

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
# INSERT ENDPOINT - OPTIMIZED FOR SQL SERVER 2012
# ===============================
@app.route("/insert-data", methods=["POST"])
def insert_data():
    if not verify_token(request):
        return jsonify({"statusCode": 401, "body": "Unauthorized"}), 401

    start_time = datetime.now()

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
        num_columns = len(columns)
        BATCH_SIZE = get_batch_size(num_columns)

        total_rows = len(rows_data)
        print(f"=== BATCH INSERT START ===")
        print(f"Table: {target_table}")
        print(f"Total rows: {total_rows}")
        print(f"Columns: {num_columns}")
        print(f"Batch size (auto-calculated): {BATCH_SIZE}")

        columns_str = ", ".join(f"[{col}]" for col in columns)

        conn = get_sql_connection()
        cursor = conn.cursor()

        total_inserted = 0
        total_errors = 0
        results = []

        for batch_start in range(0, total_rows, BATCH_SIZE):
            batch_end = min(batch_start + BATCH_SIZE, total_rows)
            batch_rows = rows_data[batch_start:batch_end]

            batch_data = []
            batch_row_nums = []

            for row in batch_rows:
                row_num = row[0]
                data_values = row[1:]

                if len(data_values) != num_columns:
                    results.append([row_num, "ERROR", f"Column mismatch: expected {num_columns}, got {len(data_values)}"])
                    total_errors += 1
                    continue

                clean_values = []
                for v in data_values:
                    try:
                        if v is None:
                            clean_values.append(None)
                        elif isinstance(v, float) and pd.isna(v):
                            clean_values.append(None)
                        elif isinstance(v, pd.Timestamp):
                            clean_values.append(v.strftime('%Y-%m-%d %H:%M:%S'))
                        else:
                            clean_values.append(v)
                    except (TypeError, ValueError):
                        clean_values.append(None)

                batch_data.append(tuple(clean_values))
                batch_row_nums.append(row_num)

            if batch_data:
                try:
                    # Build one multi-row INSERT per batch — much faster than executemany with pymssql
                    row_placeholders = ", ".join(
                        "(" + ", ".join("%s" for _ in columns) + ")"
                        for _ in batch_data
                    )
                    sql = f"INSERT INTO {target_table} ({columns_str}) VALUES {row_placeholders}"
                    flat_values = [v for row in batch_data for v in row]

                    cursor.execute(sql, flat_values)
                    conn.commit()

                    batch_count = len(batch_data)
                    total_inserted += batch_count

                    for rn in batch_row_nums:
                        results.append([rn, "SUCCESS", None])

                    print(f"Batch {batch_start}-{batch_end}: Inserted {batch_count} rows. Total: {total_inserted}/{total_rows}")

                except Exception as e:
                    conn.rollback()
                    error_msg = str(e)[:200]
                    print(f"Batch {batch_start}-{batch_end} FAILED: {error_msg}")

                    for rn in batch_row_nums:
                        results.append([rn, "ERROR", error_msg])
                    total_errors += len(batch_data)

        cursor.close()
        conn.close()

        duration = (datetime.now() - start_time).total_seconds()

        print(f"=== BATCH INSERT COMPLETE ===")
        print(f"Total rows: {total_rows}")
        print(f"Inserted: {total_inserted}")
        print(f"Errors: {total_errors}")
        print(f"Duration: {duration:.2f}s")
        print(f"Rate: {total_rows/duration:.0f} rows/sec")

        return jsonify({"data": results}), 200

    except Exception as e:
        duration = (datetime.now() - start_time).total_seconds()
        error_msg = str(e)
        print(f"=== CRITICAL ERROR after {duration:.2f}s ===")
        print(f"Error: {error_msg}")
        return jsonify({
            "statusCode": 500,
            "body": f"Server error: {error_msg}"
        }), 500

# ===============================
# APP START
# ===============================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
