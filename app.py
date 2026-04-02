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
    with _pool_lock:
        while _pool:
            conn = _pool.pop()
            try:
                conn.cursor().execute("SELECT 1")
                return conn
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
    return _new_conn()

def return_connection(conn):
    with _pool_lock:
        if len(_pool) < POOL_SIZE:
            _pool.append(conn)
            return
    try:
        conn.close()
    except Exception:
        pass

def invalidate_connection(conn):
    try:
        conn.close()
    except Exception:
        pass

def prewarm_pool():
    if _pool:
        return
    print("=== ENV VAR CHECK ===")
    print(f"  SQL_SERVER:   {'SET' if SQL_SERVER   else 'MISSING'} ({SQL_SERVER})")
    print(f"  SQL_DATABASE: {'SET' if SQL_DATABASE else 'MISSING'} ({SQL_DATABASE})")
    print(f"  SQL_USERNAME: {'SET' if SQL_USERNAME else 'MISSING'} ({SQL_USERNAME})")
    print(f"  SQL_PASSWORD: {'SET' if SQL_PASSWORD else 'MISSING'} ({'***' if SQL_PASSWORD else 'MISSING'})")
    print(f"  API_TOKEN:    {'SET' if API_TOKEN    else 'MISSING'}")
    print("=====================")

    if not all([SQL_SERVER, SQL_DATABASE, SQL_USERNAME, SQL_PASSWORD]):
        print("ERROR: One or more required env vars are missing — skipping pool init")
        return

    print("Pre-warming connection pool...")
    for _ in range(POOL_SIZE):
        try:
            with _pool_lock:
                _pool.append(_new_conn())
        except Exception as e:
            print(f"Pool prewarm failed (will connect on demand): {e}")
    print(f"Pool ready with {len(_pool)} connection(s).")


# ===============================
# HELPERS
# ===============================
def verify_token(req):
    return req.headers.get("Authorization") == f"Bearer {API_TOKEN}"

def get_batch_size(num_columns):
    return max(1, 2000 // num_columns)

def clean_value(v):
    try:
        if v is None:
            return None
        if isinstance(v, float) and pd.isna(v):
            return None
        if isinstance(v, pd.Timestamp):
            return v.strftime('%Y-%m-%d %H:%M:%S')
        if isinstance(v, str):
            stripped = v.strip()
            if stripped == '':
                return None          # empty string → NULL
            try:
                return int(stripped)
            except ValueError:
                pass
            try:
                return float(stripped)
            except ValueError:
                pass
            return stripped          # leave as string if not numeric
        return v
    except (TypeError, ValueError):
        return None

# Allowlist of valid SQL comparison operators to prevent injection
ALLOWED_OPERATORS = {"=", "!=", "<>", ">", "<", ">=", "<=", "LIKE", "IN", "NOT IN", "IS NULL", "IS NOT NULL"}

def build_where_clause(filters):
    if not filters:
        return "", ()

    clauses = []
    params  = []

    for f in filters:
        col = f.get("column", "").strip()
        op  = f.get("operator", "").strip().upper()

        if not col:
            raise ValueError("Each filter must include a 'column' field.")
        if op not in ALLOWED_OPERATORS:
            raise ValueError(f"Operator '{op}' is not allowed. Use one of: {ALLOWED_OPERATORS}")

        col_expr = f"[{col}]"

        if op in ("IS NULL", "IS NOT NULL"):
            clauses.append(f"{col_expr} {op}")

        elif op in ("IN", "NOT IN"):
            values = f.get("value")
            if not isinstance(values, list) or not values:
                raise ValueError(f"'value' for {op} must be a non-empty list.")
            placeholders = ", ".join(["%s"] * len(values))
            clauses.append(f"{col_expr} {op} ({placeholders})")
            params.extend(values)

        else:
            clauses.append(f"{col_expr} {op} %s")
            params.append(f.get("value"))

    return "WHERE " + " AND ".join(clauses), tuple(params)


@app.before_request
def initialize():
    prewarm_pool()


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
# QUERY ENDPOINT
# ===============================
@app.route("/query-data", methods=["POST"])
def query_data():
    if not verify_token(request):
        return jsonify({"statusCode": 401, "body": "Unauthorized"}), 401

    start_time = datetime.now()

    try:
        payload      = request.get_json() or {}
        target_table = request.args.get("table")

        if not target_table:
            return jsonify({"statusCode": 400, "body": "Missing 'table' query param"}), 400

        filters = payload.get("filters", [])

        try:
            where_clause, params = build_where_clause(filters)
        except ValueError as ve:
            return jsonify({"statusCode": 400, "body": str(ve)}), 400

        sql = f"SELECT * FROM {target_table}"
        if where_clause:
            sql += f" {where_clause}"

        print(f"QUERY | table={target_table} | filters={len(filters)} | sql={sql}")

        conn   = get_connection()
        cursor = conn.cursor(as_dict=True)

        try:
            cursor.execute(sql, params) if params else cursor.execute(sql)
            rows = cursor.fetchall()
            cursor.close()
            return_connection(conn)
        except Exception as e:
            invalidate_connection(conn)
            raise

        def serialise(v):
            if isinstance(v, datetime):
                return v.isoformat()
            return v

        clean_rows = [
            {k: serialise(v) for k, v in row.items()}
            for row in rows
        ]

        result = [[i, row] for i, row in enumerate(clean_rows)]

        duration = (datetime.now() - start_time).total_seconds()
        print(f"QUERY DONE | {len(rows)} rows | {duration:.2f}s")

        return jsonify({"data": result}), 200

    except Exception as e:
        duration = (datetime.now() - start_time).total_seconds()
        print(f"QUERY ERROR after {duration:.2f}s: {e}")
        return jsonify({"statusCode": 500, "body": f"Server error: {str(e)}"}), 500


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

        rows_data     = payload["data"]
        target_table  = request.args.get("table")
        columns_param = request.args.get("columns")
        do_truncate   = request.args.get("truncate", "false").lower() == "true"
        do_nocheck    = request.args.get("nocheck",  "true" ).lower() == "true"

        if not target_table or not columns_param:
            return jsonify({"statusCode": 400, "body": "Missing table or columns"}), 400

        if not rows_data:
            return jsonify({"data": []}), 200

        columns     = [c.strip() for c in columns_param.split(",")]
        num_columns = len(columns)
        BATCH_SIZE  = get_batch_size(num_columns)
        columns_str = ", ".join(f"[{c}]" for c in columns)
        total_rows  = len(rows_data)

        print(f"INSERT {target_table} | {total_rows} rows | {num_columns} cols | "
              f"batch={BATCH_SIZE} | truncate={do_truncate} | nocheck={do_nocheck}")

        batches      = []
        bad_rows     = []
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

        conn    = get_connection()
        cursor  = conn.cursor()
        results = []
        total_inserted = 0
        total_errors   = 0

        try:
            if do_truncate:
                print(f"  Truncating {target_table}...")
                cursor.execute(f"TRUNCATE TABLE {target_table}")
                conn.commit()

            if do_nocheck:
                print(f"  Disabling constraints and indexes...")
                cursor.execute(f"ALTER TABLE {target_table} NOCHECK CONSTRAINT ALL")
                conn.commit()
                cursor.execute(f"""
                    DECLARE @sql NVARCHAR(MAX) = '';
                    SELECT @sql += 'ALTER INDEX [' + i.name + '] ON {target_table} DISABLE;'
                    FROM sys.indexes i
                    WHERE i.object_id = OBJECT_ID('{target_table}')
                      AND i.type_desc = 'NONCLUSTERED'
                      AND i.is_disabled = 0;
                    EXEC sp_executesql @sql;
                """)
                conn.commit()

            for batch_nums, flat_vals in batches:
                n = len(batch_nums)
                row_placeholders = ", ".join(
                    "(" + ", ".join(["%s"] * num_columns) + ")"
                    for _ in range(n)
                )
                sql = (
                    f"INSERT INTO {target_table} WITH (TABLOCK) "
                    f"({columns_str}) VALUES {row_placeholders}"
                )
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

                    # ── DIAGNOSTIC: log column names + first row values ──────
                    print(f"  Columns ({num_columns}): {columns}")
                    first_row = list(flat_vals[:num_columns])
                    for i, (col, val) in enumerate(zip(columns, first_row)):
                        print(f"    [{i:02d}] {col} = {repr(val)} ({type(val).__name__})")
                    # ──────────────────────────────────────────────────────────

                    total_errors += n
                    results.extend([rn, "ERROR", err] for rn in batch_nums)

            if do_nocheck:
                print(f"  Re-enabling constraints and rebuilding indexes...")
                cursor.execute(f"ALTER TABLE {target_table} WITH CHECK CHECK CONSTRAINT ALL")
                conn.commit()
                cursor.execute(f"""
                    DECLARE @sql NVARCHAR(MAX) = '';
                    SELECT @sql += 'ALTER INDEX [' + i.name + '] ON {target_table} REBUILD;'
                    FROM sys.indexes i
                    WHERE i.object_id = OBJECT_ID('{target_table}')
                      AND i.type_desc = 'NONCLUSTERED'
                      AND i.is_disabled = 1;
                    EXEC sp_executesql @sql;
                """)
                conn.commit()

            cursor.close()
            return_connection(conn)

        except Exception as outer_ex:
            print(f"  OUTER EXCEPTION: {outer_ex}")
            import traceback
            traceback.print_exc()
            try:
                cursor.execute(f"ALTER TABLE {target_table} WITH CHECK CHECK CONSTRAINT ALL")
                conn.commit()
            except Exception as re_ex:
                print(f"  RE-ENABLE CONSTRAINTS FAILED: {re_ex}")
            invalidate_connection(conn)
            raise

        for rn, err in bad_rows:
            results.append([rn, "ERROR", err])
            total_errors += 1

        duration = (datetime.now() - start_time).total_seconds()
        print(f"DONE | {total_inserted} inserted | {total_errors} errors | "
              f"{duration:.2f}s | {total_rows / max(duration, 0.01):.0f} rows/s")

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
