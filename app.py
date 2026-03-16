from flask import Flask, request, jsonify
import pymssql
import os
import pandas as pd
from datetime import datetime
import threading

app = Flask(__name__)

# ===============================
# ENVIRONMENT VARIABLES
# ===============================
SQL_SERVER   = os.environ.get("SQL_SERVER")
SQL_DATABASE = os.environ.get("SQL_DATABASE")
SQL_USERNAME = os.environ.get("SQL_USERNAME")
SQL_PASSWORD = os.environ.get("SQL_PASSWORD")
API_TOKEN    = os.environ.get("API_TOKEN")

CONNECTION_TIMEOUT = 60

# ===============================
# CONNECTION POOL
# Keeps a small pool of persistent connections so we don't pay the
# TCP + auth handshake cost on every /insert-data request.
# ===============================
_pool      = []
_pool_lock = threading.Lock()
POOL_SIZE  = 3

def _new_conn():
    return pymssql.connect(
        server=SQL_SERVER,
        user=SQL_USERNAME,
        password=SQL_PASSWORD,
        database=SQL_DATABASE,
        charset='UTF-8',
        timeout=CONNECTION_TIMEOUT,
        login_timeout=30
    )

def get_connection():
    """Return a pooled connection, creating one if the pool is empty."""
    with _pool_lock:
        if _pool:
            return _pool.pop()
    return _new_conn()

def return_connection(conn):
    """Return a healthy connection back to the pool."""
    with _pool_lock:
        if len(_pool) < POOL_SIZE:
            _pool.append(conn)
            return
    try:
        conn.close()
    except Exception:
        pass

def invalidate_connection(conn):
    """Discard a broken connection — do not return it to the pool."""
    try:
        conn.close()
    except Exception:
        pass

# Pre-warm the pool on startup
def _prewarm_pool():
    for _ in range(POOL_SIZE):
        try:
            with _pool_lock:
                _pool.append(_new_conn())
        except Exception as e:
            print(f"Pool prewarm failed: {e}")

_prewarm_pool()


# ===============================
# HELPERS
# ===============================
def verify_token(req):
    return req.headers.get("Authorization") == f"Bearer {API_TOKEN}"

def get_batch_size(num_columns):
    """Stay under SQL Server 2012's 2100 parameter limit."""
    return max(1, 2000 // num_columns)

def clean_value(v):
    try:
        if v is None:
            return None
        if isinstance(v, float) and pd.isna(v):
            return None
        if isinstance(v, pd.Timestamp):
            return v.strftime('%Y-%m-%d %H:%M:%S')
        return v
    except (TypeError, ValueError):
        return None


# ===============================
# HEALTH CHECK
# ===============================
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "pool_size": len(_pool),
        "timestamp": datetime.utcnow().isoformat()
    }), 200


# ===============================
# INSERT ENDPOINT
# ===============================
@app.route("/insert-data", methods=["POST"])
def insert_data():
    if not verify_token(request):
        return jsonify({"statusCode": 401, "body": "Unauthorized"}), 401

    start_time = datetime.now()

    try:
        payload = request.get_json()
        if not payload or "data" not in payload:
            return jsonify({"statusCode": 400, "body": "Invalid payload"}), 400

        rows_data      = payload["data"]
        target_table   = request.args.get("table")
        columns_param  = request.args.get("columns")

        if not target_table or not columns_param:
            return jsonify({"statusCode": 400, "body": "Missing table or columns"}), 400

        if not rows_data:
            return jsonify({"data": []}), 200

        columns      = [c.strip() for c in columns_param.split(",")]
        num_columns  = len(columns)
        BATCH_SIZE   = get_batch_size(num_columns)
        columns_str  = ", ".join(f"[{c}]" for c in columns)
        total_rows   = len(rows_data)

        print(f"INSERT {target_table} | {total_rows} rows | {num_columns} cols | batch={BATCH_SIZE}")

        # ── BUILD ALL BATCHES UPFRONT ────────────────────────────────────────
        # Validate and clean every row before touching the DB connection.
        # This way we hold the connection for the shortest possible time.
        batches  = []  # list of (row_nums, values_tuple)
        bad_rows = []  # (row_num, error_msg) for column-mismatch rows

        current_nums = []
        current_vals = []

        for row in rows_data:
            row_num     = row[0]
            data_values = row[1:]

            if len(data_values) != num_columns:
                bad_rows.append((row_num, f"Column mismatch: expected {num_columns}, got {len(data_values)}"))
                continue

            current_nums.append(row_num)
            current_vals.extend(clean_value(v) for v in data_values)

            if len(current_nums) == BATCH_SIZE:
                batches.append((list(current_nums), tuple(current_vals)))
                current_nums = []
                current_vals = []

        if current_nums:
            batches.append((current_nums, tuple(current_vals)))

        # ── SINGLE CONNECTION FOR ALL BATCHES ────────────────────────────────
        # One connection, one transaction per batch, connection returned to pool.
        conn    = get_connection()
        cursor  = conn.cursor()
        results = []

        total_inserted = 0
        total_errors   = 0

        try:
            for batch_nums, flat_vals in batches:
                n = len(batch_nums)
                row_placeholders = ", ".join(
                    "(" + ", ".join(["%s"] * num_columns) + ")"
                    for _ in range(n)
                )
                sql = f"INSERT INTO {target_table} ({columns_str}) VALUES {row_placeholders}"

                t0 = datetime.now()
                try:
                    cursor.execute(sql, flat_vals)
                    conn.commit()
                    exec_ms = (datetime.now() - t0).total_seconds() * 1000

                    total_inserted += n
                    results.extend([rn, "SUCCESS", None] for rn in batch_nums)
                    print(f"  Batch {n} rows | {exec_ms:.0f}ms | total {total_inserted}/{total_rows}")

                except Exception as e:
                    conn.rollback()
                    err = str(e)[:200]
                    print(f"  Batch FAILED: {err}")
                    total_errors += n
                    results.extend([rn, "ERROR", err] for rn in batch_nums)

            cursor.close()
            return_connection(conn)   # ← return healthy connection to pool

        except Exception:
            invalidate_connection(conn)   # ← discard broken connection
            raise

        # Add any pre-validation errors
        for rn, err in bad_rows:
            results.append([rn, "ERROR", err])
            total_errors += 1

        duration = (datetime.now() - start_time).total_seconds()
        print(f"DONE | {total_inserted} inserted | {total_errors} errors | "
              f"{duration:.2f}s | {total_rows/max(duration,0.01):.0f} rows/s")

        return jsonify({"data": results}), 200

    except Exception as e:
        duration = (datetime.now() - start_time).total_seconds()
        print(f"CRITICAL ERROR after {duration:.2f}s: {e}")
        return jsonify({"statusCode": 500, "body": f"Server error: {str(e)}"}), 500


# ===============================
# APP START
# ===============================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
