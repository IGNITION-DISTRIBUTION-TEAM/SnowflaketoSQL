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

@app.route('/test-connection', methods=['GET'])
def test_connection():
    """Test SQL Server connection with detailed diagnostics"""
    if not verify_token(request):
        return jsonify({'error': 'Unauthorized'}), 401
    
    import socket
    
    result = {
        'timestamp': datetime.utcnow().isoformat(),
        'config': {
            'sql_server': SQL_SERVER,
            'sql_database': SQL_DATABASE,
            'sql_username': SQL_USERNAME
        }
    }
    
    # Test 1: DNS Resolution
    try:
        server_host = SQL_SERVER.split(':')[0].split('\\')[0]
        ip = socket.gethostbyname(server_host)
        result['dns_resolution'] = f"✓ Resolved to {ip}"
    except Exception as e:
        result['dns_resolution'] = f"✗ DNS Error: {str(e)}"
    
    # Test 2: Port Connectivity
    try:
        server_parts = SQL_SERVER.split(':')
        host = server_parts[0].split('\\')[0]
        port = int(server_parts[1]) if len(server_parts) > 1 else 1433
        
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        conn_result = sock.connect_ex((host, port))
        sock.close()
        
        if conn_result == 0:
            result['port_check'] = f"✓ Port {port} is OPEN"
        else:
            result['port_check'] = f"✗ Port {port} is CLOSED (Error code: {conn_result})"
    except Exception as e:
        result['port_check'] = f"✗ Port test error: {str(e)}"
    
    # Test 3: SQL Server Connection
    try:
        conn = get_sql_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT @@VERSION')
        version = cursor.fetchone()[0]
        cursor.close()
        conn.close()
        
        result['sql_connection'] = "✓ Connected successfully"
        result['sql_version'] = version[:100]  # First 100 chars
        result['status'] = 'SUCCESS'
    except Exception as e:
        result['sql_connection'] = f"✗ Connection failed: {str(e)}"
        result['status'] = 'FAILED'
    
    return jsonify(result), 200 if result['status'] == 'SUCCESS' else 500

@app.route('/my-ip', methods=['GET'])
def my_ip():
    """Show Render's outbound IP address - use this IP in Azure firewall"""
    import requests
    try:
        response = requests.get('https://api.ipify.org?format=json', timeout=10)
        ip_data = response.json()
        return jsonify({
            'render_outbound_ip': ip_data['ip'],
            'message': 'Add this IP to Azure SQL firewall rules',
            'instructions': [
                '1. Go to Azure Portal',
                '2. Navigate to ig-sa-silversurfer-prd SQL Server',
                '3. Click Networking',
                '4. Add firewall rule with this IP',
                '5. Wait 2 minutes',
                '6. Try /test-connection again'
            ]
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
                
                # Convert to tuple for pymssql (it doesn't accept lists)
                data_tuple = tuple(data_values)
                
                # Build dynamic INSERT statement
                # Adjust the number of placeholders based on your columns
                placeholders = ', '.join(['%s' for _ in data_values])
                insert_sql = f"INSERT INTO {target_table} VALUES ({placeholders})"
                
                cursor.execute(insert_sql, data_tuple)
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

# =============================================================================
# UPSERT/UPDATE/DELETE/TRUNCATE ENDPOINTS REMOVED
# This application only supports INSERT operations for security
# =============================================================================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)





