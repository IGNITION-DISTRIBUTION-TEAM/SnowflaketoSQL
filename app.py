from flask import Flask, request, jsonify
import pyodbc
import json
import os
from datetime import datetime

app = Flask(__name__)

# SQL Server connection configuration
SQL_SERVER = os.environ.get('SQL_SERVER')
SQL_DATABASE = os.environ.get('SQL_DATABASE')
SQL_USERNAME = os.environ.get('SQL_USERNAME')
SQL_PASSWORD = os.environ.get('SQL_PASSWORD')
API_SECRET = os.environ.get('API_SECRET')
SQL_DRIVER = '{ODBC Driver 17 for SQL Server}'

# Authentication token for security
API_TOKEN = os.environ.get('API_TOKEN', API_SECRET)

def get_sql_connection():
    """Create and return SQL Server connection"""
    conn_string = f'DRIVER={SQL_DRIVER};SERVER={SQL_SERVER};DATABASE={SQL_DATABASE};UID={SQL_USERNAME};PWD={SQL_PASSWORD}'
    return pyodbc.connect(conn_string)

def verify_token(request):
    """Verify the API token from request headers"""
    token = request.headers.get('Authorization')
    if not token or token != f'Bearer {API_TOKEN}':
        return False
    return True

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({'status': 'healthy', 'timestamp': datetime.utcnow().isoformat()}), 200

@app.route('/insert-data', methods=['POST'])
def insert_data():
    """
    Endpoint to receive data from Snowflake and insert into SQL Server
    Expected JSON format from Snowflake:
    {
        "data": [
            [0, "row1_col1", "row1_col2", ...],
            [1, "row2_col1", "row2_col2", ...]
        ]
    }
    
    URL Parameters:
    - table: Target table name (required)
    
    Example: /insert-data?table=MyTable
    """
    # Verify authentication
    if not verify_token(request):
        return jsonify({'statusCode': 401, 'body': 'Unauthorized'}), 401
    
    try:
        payload = request.get_json()
        
        if not payload or 'data' not in payload:
            return jsonify({'statusCode': 400, 'body': 'Invalid payload format'}), 400
        
        rows_data = payload['data']
        target_table = request.args.get('table')
        
        if not target_table:
            return jsonify({'statusCode': 400, 'body': 'Missing table parameter'}), 400
        
        # Connect to SQL Server
        conn = get_sql_connection()
        cursor = conn.cursor()
        
        results = []
        success_count = 0
        error_count = 0
        
        for row in rows_data:
            try:
                # row[0] is the row number from Snowflake
                # row[1:] contains the actual data
                row_num = row[0]
                data_values = row[1:]
                
                # Build dynamic INSERT statement
                # Adjust the number of placeholders based on your columns
                placeholders = ', '.join(['?' for _ in data_values])
                insert_sql = f"INSERT INTO {target_table} VALUES ({placeholders})"
                
                cursor.execute(insert_sql, data_values)
                success_count += 1
                results.append([row_num, 'SUCCESS', None])
                
            except Exception as e:
                error_count += 1
                error_msg = str(e)
                results.append([row_num, 'ERROR', error_msg])
                print(f"Error inserting row {row_num}: {error_msg}")
        
        # Commit all successful inserts
        conn.commit()
        cursor.close()
        conn.close()
        
        print(f"Insert complete: {success_count} success, {error_count} errors")
        
        # Return results in Snowflake external function format
        response = {
            'data': results
        }
        
        return jsonify(response), 200
        
    except Exception as e:
        error_msg = str(e)
        print(f"Critical error: {error_msg}")
        return jsonify({'statusCode': 500, 'body': f'Server error: {error_msg}'}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
