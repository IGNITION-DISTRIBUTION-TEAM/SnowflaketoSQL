from flask import Flask, request, jsonify
import pymssql
import os
import re
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

# Optional comma-separated allowlist, e.g. "Upload.TempUpload,Upload.Batches"
# If unset, any syntactically valid table name is accepted (previous behaviour).
ALLOWED_TABLES = {
    t.strip().lower()
    for t in (os.environ.get("ALLOWED_TABLES") or "").split(",")
    if t.strip()
}

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
    print(f"  ALLOWED_TABLES: {sorted(ALLOWED_TABLES) if ALLOWED_TABLES else 'UNRESTRICTED'}")
    print("=====================")

    if not all([SQL_SERVER, SQL_DATABASE, SQL_USERNAME, SQL_PASSWORD]):
        print("ERROR: One or more required env vars are missing - skipping pool init")
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
        return v
    except (TypeError, ValueError):
        return None


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

def safe_table(raw):
    """
    Validate and re-bracket a table reference.

    Accepts  'TempUpload' | 'Upload.TempUpload' | 'MyDb.Upload.TempUpload'
    Returns  '[Upload].[TempUpload]'
    Raises   ValueError on anything else.

    A table name cannot be parameterised, so it is validated rather than bound.
    """
    if not raw:
        raise ValueError("Missing 'table' query param")

    parts = [p.strip().strip("[]") for p in raw.split(".")]
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"Invalid table reference: {raw}")
    for p in parts:
        if not _IDENT_RE.match(p):
            raise ValueError(f"Invalid identifier in table reference: {p}")

    if ALLOWED_TABLES and raw.strip().lower() not in ALLOWED_TABLES:
        raise ValueError(f"Table '{raw}' is not in ALLOWED_TABLES")

    return ".".join(f"[{p}]" for p in parts)


def safe_column(raw):
    """Validate a bare column name and return it bracketed."""
    col = (raw or "").strip().strip("[]")
    if not _IDENT_RE.match(col):
        raise ValueError(f"Invalid column name: {raw}")
    return f"[{col}]"


_ORDER_BY_RE = re.compile(r"^\s*\[?([A-Za-z_][A-Za-z0-9_]*)\]?\s*(ASC|DESC)?\s*$", re.I)

def safe_order_by(raw):
    """
    Validate an ORDER BY of the form '<column> [ASC|DESC]'.
    Returns '[COLUMN] DESC'. Raises ValueError otherwise.
    """
    m = _ORDER_BY_RE.match(raw or "")
    if not m:
        raise ValueError(
            f"Invalid order_by: {raw!r}. Expected '<column> [ASC|DESC]'."
        )
    return f"[{m.group(1)}] {(m.group(2) or 'DESC').upper()}"


# Only aggregate predicates of the form COUNT(1) > 1 are permitted.
_HAVING_RE = re.compile(
    r"^\s*(COUNT)\s*\(\s*(1|\*)\s*\)\s*(=|!=|<>|>=|<=|>|<)\s*(\d+)\s*$", re.I
)

def safe_having(raw):
    """Validate an optional HAVING clause. Returns '' or 'HAVING COUNT(1) > 1'."""
    if not raw:
        return ""
    m = _HAVING_RE.match(raw)
    if not m:
        raise ValueError(
            f"Invalid having: {raw!r}. Only 'COUNT(1) <op> <number>' is supported."
        )
    return f"HAVING COUNT({m.group(2)}) {m.group(3)} {m.group(4)}"


# Allowlist of valid SQL comparison operators to prevent injection
ALLOWED_OPERATORS = {"=", "!=", "<>", ">", "<", ">=", "<=", "LIKE", "IN", "NOT IN", "IS NULL", "IS NOT NULL"}

def build_where_clause(filters, keyword="WHERE"):
    """
    Build a parameterised WHERE clause from a list of filter dicts.

    Each filter dict must have:
        column   (str)  - column name
        operator (str)  - one of ALLOWED_OPERATORS
        value    (any)  - the value to compare against
                          (ignored for IS NULL / IS NOT NULL)

    keyword lets the caller ask for 'AND' instead of 'WHERE' when the clause
    is being appended to an existing predicate.

    Returns (clause_str, params_tuple) or raises ValueError on bad input.
    """
    if not filters:
        return "", ()

    clauses = []
    params  = []

    for f in filters:
        col_expr = safe_column(f.get("column"))
        op       = f.get("operator", "").strip().upper()

        if op not in ALLOWED_OPERATORS:
            raise ValueError(f"Operator '{op}' is not allowed. Use one of: {sorted(ALLOWED_OPERATORS)}")

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

    return f"{keyword} " + " AND ".join(clauses), tuple(params)


def serialise(v):
    if isinstance(v, datetime):
        return v.isoformat()
    return v


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
#
# POST /query-data?table=<table>
#
# {
#   "filters": [
#     {"column": "status",     "operator": "=",    "value": "active"},
#     {"column": "region",     "operator": "IN",   "value": ["ZA", "NG"]},
#     {"column": "deleted_at", "operator": "IS NULL"}
#   ]
# }
#
# Response: {"data": [[0, {...row...}], [1, {...row...}], ...]}
# ===============================
@app.route("/query-data", methods=["POST"])
def query_data():
    if not verify_token(request):
        return jsonify({"statusCode": 401, "body": "Unauthorized"}), 401

    start_time = datetime.now()

    try:
        payload = request.get_json() or {}

        try:
            target_table = safe_table(request.args.get("table"))
            where_clause, params = build_where_clause(payload.get("filters", []))
        except ValueError as ve:
            return jsonify({"statusCode": 400, "body": str(ve)}), 400

        sql = f"SELECT * FROM {target_table}"
        if where_clause:
            sql += f" {where_clause}"

        print(f"QUERY | sql={sql} | params={params}")

        conn   = get_connection()
        cursor = conn.cursor(as_dict=True)

        try:
            cursor.execute(sql, params) if params else cursor.execute(sql)
            rows = cursor.fetchall()
            cursor.close()
            return_connection(conn)
        except Exception:
            invalidate_connection(conn)
            raise

        clean_rows = [{k: serialise(v) for k, v in row.items()} for row in rows]
        result = [[i, row] for i, row in enumerate(clean_rows)]

        duration = (datetime.now() - start_time).total_seconds()
        print(f"QUERY DONE | {len(rows)} rows | {duration:.2f}s")

        return jsonify({"data": result}), 200

    except Exception as e:
        duration = (datetime.now() - start_time).total_seconds()
        print(f"QUERY ERROR after {duration:.2f}s: {e}")
        return jsonify({"statusCode": 500, "body": f"Server error: {str(e)}"}), 500


# ===============================
# AGGREGATE ENDPOINT (GROUP BY [+ HAVING])
#
# POST /aggregate-data?table=<table>
#
# {
#   "select_columns": ["COUNT(1) AS DUP_COUNT", "CELLNUMBER", "CAMPAIGNID", "IDNUMBER"],
#   "group_by":       ["CELLNUMBER", "CAMPAIGNID", "IDNUMBER"],
#   "having":         "COUNT(1) > 1",
#   "filters":        [{"column": "PROCESSEDFAILED", "operator": "=", "value": 0}]
# }
#
# 'having' is optional and only accepts COUNT(1) <op> <number>.
# ===============================
@app.route("/aggregate-data", methods=["POST"])
def aggregate_data():
    if not verify_token(request):
        return jsonify({"statusCode": 401, "body": "Unauthorized"}), 401

    start_time = datetime.now()

    try:
        payload = request.get_json() or {}

        select_columns = payload.get("select_columns", [])
        group_by_cols  = payload.get("group_by", [])

        if not select_columns:
            return jsonify({"statusCode": 400, "body": "Missing 'select_columns' for aggregate query"}), 400
        if not group_by_cols:
            return jsonify({"statusCode": 400, "body": "Missing 'group_by' for aggregate query"}), 400

        try:
            target_table  = safe_table(request.args.get("table"))
            where_clause, params = build_where_clause(payload.get("filters", []))
            having_clause = safe_having(payload.get("having"))
            group_by_expr = ", ".join(safe_column(c) for c in group_by_cols)
        except ValueError as ve:
            return jsonify({"statusCode": 400, "body": str(ve)}), 400

        sql = f"SELECT {', '.join(select_columns)} FROM {target_table}"
        if where_clause:
            sql += f" {where_clause}"
        sql += f" GROUP BY {group_by_expr}"
        if having_clause:
            sql += f" {having_clause}"

        print(f"AGGREGATE | sql={sql} | params={params}")

        conn   = get_connection()
        cursor = conn.cursor(as_dict=True)

        try:
            cursor.execute(sql, params) if params else cursor.execute(sql)
            rows = cursor.fetchall()
            cursor.close()
            return_connection(conn)
        except Exception:
            invalidate_connection(conn)
            raise

        clean_rows = [{k: serialise(v) for k, v in row.items()} for row in rows]
        result = [[i, row] for i, row in enumerate(clean_rows)]

        duration = (datetime.now() - start_time).total_seconds()
        print(f"AGGREGATE DONE | {len(rows)} result rows | {duration:.2f}s")

        return jsonify({"data": result}), 200

    except Exception as e:
        duration = (datetime.now() - start_time).total_seconds()
        print(f"AGGREGATE ERROR after {duration:.2f}s: {e}")
        return jsonify({"statusCode": 500, "body": f"Server error: {str(e)}"}), 500


# ===============================
# DELETE ENDPOINT
#
# POST /delete-data?table=<table>
#
# Option 1 - delete by filter:
# {
#   "filters": [{"column": "TEMPUPLOADID", "operator": "IN", "value": [123]}],
#   "dry_run": true
# }
#
# Option 2 - delete duplicates, keep one row per partition:
# {
#   "delete_duplicates": true,
#   "partition_by": ["CELLNUMBER", "CAMPAIGNID", "IDNUMBER"],
#   "order_by": "TEMPUPLOADID DESC",
#   "filters": [{"column": "PROCESSEDFAILED", "operator": "=", "value": 0}],
#   "dry_run": true
# }
#
# order_by DESC keeps the HIGHEST value (newest row); ASC keeps the lowest.
# The filter is applied INSIDE the de-duplication window, so only rows
# matching it are ever considered or deleted.
#
# dry_run: true returns {"rows_matched": N} and deletes nothing.
#
# Response: {"statusCode": 200, "rows_deleted": 42, "message": "..."}
# ===============================
@app.route("/delete-data", methods=["POST"])
def delete_data():
    if not verify_token(request):
        return jsonify({"statusCode": 401, "body": "Unauthorized"}), 401

    start_time = datetime.now()

    try:
        payload = request.get_json() or {}

        delete_duplicates = bool(payload.get("delete_duplicates", False))
        dry_run           = bool(payload.get("dry_run", False))
        filters           = payload.get("filters", [])

        try:
            target_table = safe_table(request.args.get("table"))
            where_clause, params = build_where_clause(filters)
        except ValueError as ve:
            return jsonify({"statusCode": 400, "body": str(ve)}), 400

        # ---- build the statement --------------------------------------
        if delete_duplicates:
            partition_by = payload.get("partition_by", [])
            if not partition_by:
                return jsonify({"statusCode": 400,
                                "body": "partition_by required for delete_duplicates"}), 400
            try:
                partition_expr = ", ".join(safe_column(c) for c in partition_by)
                order_expr     = safe_order_by(payload.get("order_by", "TEMPUPLOADID DESC"))
            except ValueError as ve:
                return jsonify({"statusCode": 400, "body": str(ve)}), 400

            # The WHERE clause sits inside the CTE. This is the important bit:
            # rows excluded by the filter are neither ranked nor deleted.
            cte = f"""
                WITH dupes AS (
                    SELECT ROW_NUMBER() OVER (
                               PARTITION BY {partition_expr}
                               ORDER BY {order_expr}
                           ) AS rn
                    FROM {target_table}
                    {where_clause}
                )
            """
            count_sql  = f"{cte} SELECT COUNT(*) FROM dupes WHERE rn > 1;"
            delete_sql = f"{cte} DELETE FROM dupes WHERE rn > 1;"
            label      = f"DELETE DUPLICATES | partition_by={partition_by} | order_by={order_expr}"

        else:
            if not where_clause:
                return jsonify({"statusCode": 400,
                                "body": "No filters provided for delete"}), 400
            count_sql  = f"SELECT COUNT(*) FROM {target_table} {where_clause};"
            delete_sql = f"DELETE FROM {target_table} {where_clause};"
            label      = f"DELETE | filters={len(filters)}"

        # ---- execute ---------------------------------------------------
        conn   = get_connection()
        cursor = conn.cursor()

        try:
            if dry_run:
                print(f"{label} | DRY RUN | sql={count_sql} | params={params}")
                cursor.execute(count_sql, params) if params else cursor.execute(count_sql)
                rows_matched = cursor.fetchone()[0]
                cursor.close()
                return_connection(conn)

                duration = (datetime.now() - start_time).total_seconds()
                print(f"DRY RUN DONE | {rows_matched} rows would be deleted | {duration:.2f}s")
                return jsonify({
                    "statusCode": 200,
                    "dry_run": True,
                    "rows_matched": rows_matched,
                    "rows_deleted": 0,
                    "message": f"Dry run: {rows_matched} row(s) would be deleted",
                    "duration_seconds": duration
                }), 200

            print(f"{label} | sql={delete_sql} | params={params}")
            cursor.execute(delete_sql, params) if params else cursor.execute(delete_sql)
            rows_deleted = cursor.rowcount
            conn.commit()

            cursor.close()
            return_connection(conn)

            duration = (datetime.now() - start_time).total_seconds()
            print(f"DELETE DONE | {rows_deleted} rows deleted | {duration:.2f}s")

            return jsonify({
                "statusCode": 200,
                "dry_run": False,
                "rows_deleted": rows_deleted,
                "message": f"Successfully deleted {rows_deleted} rows",
                "duration_seconds": duration
            }), 200

        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            invalidate_connection(conn)
            raise

    except Exception as e:
        duration = (datetime.now() - start_time).total_seconds()
        print(f"DELETE ERROR after {duration:.2f}s: {e}")
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
        columns_param = request.args.get("columns")
        do_truncate   = request.args.get("truncate", "false").lower() == "true"
        do_nocheck    = request.args.get("nocheck",  "true" ).lower() == "true"

        if not columns_param:
            return jsonify({"statusCode": 400, "body": "Missing columns"}), 400

        try:
            target_table = safe_table(request.args.get("table"))
            columns      = [c.strip() for c in columns_param.split(",")]
            columns_str  = ", ".join(safe_column(c) for c in columns)
        except ValueError as ve:
            return jsonify({"statusCode": 400, "body": str(ve)}), 400

        if not rows_data:
            return jsonify({"data": []}), 200

        num_columns = len(columns)
        BATCH_SIZE  = get_batch_size(num_columns)
        total_rows  = len(rows_data)

        print(f"INSERT {target_table} | {total_rows} rows | {num_columns} cols | "
              f"batch={BATCH_SIZE} | truncate={do_truncate} | nocheck={do_nocheck}")

        batches  = []
        bad_rows = []
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
                print("  Disabling constraints and indexes...")
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
                    total_errors += n
                    results.extend([rn, "ERROR", err] for rn in batch_nums)

            if do_nocheck:
                print("  Re-enabling constraints and rebuilding indexes...")
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

        except Exception:
            try:
                cursor.execute(f"ALTER TABLE {target_table} WITH CHECK CHECK CONSTRAINT ALL")
                conn.commit()
            except Exception:
                pass
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
