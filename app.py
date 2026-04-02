CREATE OR REPLACE PROCEDURE DATAWAREHOUSE.DISTRIBUTION_AUTOMATION.SP_SYNC_FROM_SQLSERVER(
    "SQL_SERVER_TABLE"  VARCHAR,   -- e.g. 'dbo.Orders'
    "SNOWFLAKE_TARGET"  VARCHAR,   -- e.g. 'DATAWAREHOUSE.DISTRIBUTION_AUTOMATION.ORDERS_MIRROR'
    "FILTERS_JSON"      VARCHAR    -- '[{"column":"status","operator":"=","value":"active"}]' or '[]'
)
RETURNS VARCHAR(16777216)
LANGUAGE PYTHON
RUNTIME_VERSION = '3.9'
PACKAGES = ('snowflake-snowpark-python','requests','pandas')
HANDLER = 'main'
EXTERNAL_ACCESS_INTEGRATIONS = (INT_SNOWFLAKETOSQL_INTERGRATION)
SECRETS = ('api_token'=DATAWAREHOUSE.DISTRIBUTION_AUTOMATION.SNOWFLAKETOSQL_SECRET)
EXECUTE AS OWNER
AS '
import requests
import _snowflake
import pandas as pd
import json
from snowflake.snowpark import Session
from datetime import datetime
import time

RENDER_URL = "https://snowflaketosql-1.onrender.com"


# ── FETCH FROM RENDER ─────────────────────────────────────────────────────────

def fetch_from_render(api_token: str, sql_table: str, filters: list) -> tuple:
    url     = f"{RENDER_URL}/query-data?table={sql_table}"
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    payload = {"filters": filters}

    for attempt in range(3):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=600)
            if r.status_code == 200:
                raw  = r.json().get("data", [])
                rows = [entry[1] for entry in raw]
                return rows, None
            error = f"HTTP {r.status_code}: {r.text[:300]}"
        except Exception as e:
            error = str(e)
        if attempt < 2:
            time.sleep(2 ** attempt)

    return [], error


# ── PARSE FULLY QUALIFIED TABLE NAME ─────────────────────────────────────────
# Handles:
#   3-part: DATABASE.SCHEMA.TABLE
#   2-part: SCHEMA.TABLE          (uses session default database)
#   1-part: TABLE                 (uses session defaults)

def parse_table_ref(fully_qualified: str) -> tuple:
    parts = [p.strip().strip(''"`\'''') for p in fully_qualified.split(".")]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    elif len(parts) == 2:
        return None, parts[0], parts[1]
    else:
        return None, None, parts[0]


# ── WRITE TO SNOWFLAKE ────────────────────────────────────────────────────────

def write_to_snowflake(session: Session, rows: list, target_table: str,
                       results: list) -> tuple:
    if not rows:
        results.append("  No rows returned from SQL Server.")
        return 0, 0

    df = pd.DataFrame(rows)

    # Uppercase column names to match Snowflake convention
    df.columns = [c.upper() for c in df.columns]

    results.append(f"  Columns ({len(df.columns)}): {list(df.columns)}")
    results.append(f"  Rows fetched: {len(df):,}")

    database, schema, table = parse_table_ref(target_table)

    try:
        # DROP existing table so schema is always re-inferred cleanly from
        # whatever columns SQL Server returns — no manual DDL needed.
        session.sql(f"DROP TABLE IF EXISTS {target_table}").collect()
        results.append(f"  Dropped existing {target_table} (if any)")

        write_kwargs = dict(
            df             = df,
            table_name     = table,
            overwrite      = True,    # recreate if it somehow still exists
            auto_create_table = True, # infer schema from the DataFrame
            quote_identifiers = False
        )
        if database:
            write_kwargs["database"] = database
        if schema:
            write_kwargs["schema"] = schema

        session.write_pandas(**write_kwargs)

        results.append(f"  ✓ Table {target_table} created and loaded")
        return len(df), 0

    except Exception as e:
        results.append(f"  ERROR writing to Snowflake: {e}")
        return 0, len(df)


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main(session: Session,
         SQL_SERVER_TABLE: str,
         SNOWFLAKE_TARGET: str,
         FILTERS_JSON: str):

    api_token  = _snowflake.get_generic_secret_string("api_token")
    start_time = datetime.now()
    run_id     = start_time.strftime(''%Y%m%d_%H%M%S'')
    results    = []

    results.append("=" * 80)
    results.append("SQL SERVER → SNOWFLAKE SYNC  (auto schema)")
    results.append("=" * 80)
    results.append(f"Run ID:           {run_id}")
    results.append(f"Started:          {start_time.strftime(''%Y-%m-%d %H:%M:%S'')}")
    results.append(f"SQL Server Table: {SQL_SERVER_TABLE}")
    results.append(f"Snowflake Target: {SNOWFLAKE_TARGET}")
    results.append(f"Filters:          {FILTERS_JSON}")
    results.append("")

    # ── 1. PARSE FILTERS ─────────────────────────────────────────────────────
    try:
        filters = json.loads(FILTERS_JSON) if FILTERS_JSON else []
        if not isinstance(filters, list):
            raise ValueError("FILTERS_JSON must be a JSON array")
    except Exception as e:
        return f"Invalid FILTERS_JSON: {e}"

    results.append(f"Parsed {len(filters)} filter(s).")

    # ── 2. FETCH FROM SQL SERVER VIA RENDER ───────────────────────────────────
    results.append("Fetching data from SQL Server...")
    fetch_start = datetime.now()
    rows, error = fetch_from_render(api_token, SQL_SERVER_TABLE, filters)
    fetch_sec   = (datetime.now() - fetch_start).total_seconds()

    if error:
        results.append(f"✗ Fetch failed after {fetch_sec:.1f}s: {error}")
        return "\\n".join(results)

    results.append(f"✓ Fetched {len(rows):,} rows in {fetch_sec:.1f}s")
    results.append("")

    # ── 3. WRITE TO SNOWFLAKE (auto DDL) ─────────────────────────────────────
    results.append(f"Writing to {SNOWFLAKE_TARGET}...")
    written, failed = write_to_snowflake(session, rows, SNOWFLAKE_TARGET, results)

    # ── SUMMARY ───────────────────────────────────────────────────────────────
    duration = (datetime.now() - start_time).total_seconds()
    results.append("")
    results.append("=" * 80)
    results.append("SYNC SUMMARY")
    results.append("=" * 80)
    results.append(f"Rows Fetched:  {len(rows):,}")
    results.append(f"Rows Written:  {written:,}")
    results.append(f"Rows Failed:   {failed:,}")
    results.append(f"Duration:      {duration:.1f}s ({duration/60:.1f} min)")
    results.append(f"Avg Rate:      {written / max(duration, 1):.0f} rows/sec")
    results.append("=" * 80)

    return "\\n".join(results)
';
