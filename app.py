from flask import Flask, request, jsonify
import pymssql
import json
import os
from datetime import datetime

app = Flask(__name__)

# SQL Server connection configuration
SQL_SERVER = os.environ.get('SQL_SERVER')
SQL_DATABASE = os.environ.get('SQL_DATABASE')
SQL_USERNAME = os.environ.get('SQL_USERNAME')
SQL_PASSWORD = os.environ.get('SQL_PASSWORD')

# Authentication token for security
API_TOKEN = os.environ.get('API_SECRET')

def get_sql_connection():
    """Create and return SQL Server connection"""
    conn = pymssql.connect(
        server=SQL_SERVER,
        user=SQL_USERNAME,
        password=SQL_PASSWORD,
        database=SQL_DATABASE
    )
    return conn

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
    - columns: Comma-separated column names (optional)
    
    Example: /insert-data?table=MyTable&columns=col1,col2,col3
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
        columns_param = request.args.get('columns')  # Get columns parameter
        
        if not target_table:
            return jsonify({'statusCode': 400, 'body': 'Missing table parameter'}), 400
        
        # Log what we received
        sample_row = rows_data[0] if rows_data else []
        data_column_count = len(sample_row) - 1  # Subtract row number
        print(f"Table: {target_table}")
        print(f"Columns param: {columns_param}")
        print(f"Data column count: {data_column_count}")
        
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
                
                # Convert to tuple for pymssql (it doesn't accept lists)
                data_tuple = tuple(data_values)
                
                # Build dynamic INSERT statement
                if columns_param:
                    # If columns are specified, use them
                    columns = [col.strip() for col in columns_param.split(',')]
                    
                    # Validate column count
                    if len(data_values) != len(columns):
                        raise ValueError(
                            f"Column mismatch: {len(data_values)} values but {len(columns)} columns specified"
                        )
                    
                    columns_str = ', '.join([f'[{col}]' for col in columns])
                    placeholders = ', '.join(['%s' for _ in data_values])
                    insert_sql = f"INSERT INTO {target_table} ({columns_str}) VALUES ({placeholders})"
                else:
                    # If no columns specified, insert into all columns
                    placeholders = ', '.join(['%s' for _ in data_values])
                    insert_sql = f"INSERT INTO {target_table} VALUES ({placeholders})"
                
                cursor.execute(insert_sql, data_tuple)
                success_count += 1
                results.append([row_num, 'SUCCESS', None])
                
            except Exception as e:
                error_count += 1
                error_msg = str(e)
                results.append([row_num, 'ERROR', error_msg])
                if error_count <= 3:  # Only log first 3 errors
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

# =============================================================================
# UPSERT/UPDATE/DELETE/TRUNCATE ENDPOINTS REMOVED
# This application only supports INSERT operations for security
# =============================================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
