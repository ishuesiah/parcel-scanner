# web_scanner.py
import os
from flask import Flask, request, redirect, url_for, render_template_string, flash
import mysql.connector
from mysql.connector import pooling
from datetime import datetime
from config import MYSQL_CONFIG

app = Flask(__name__)
app.secret_key = os.urandom(24)  # for flashing messages

db_pool = mysql.connector.pooling.MySQLConnectionPool(
    pool_name         = "flask_pool",
    pool_size         = 5,
    pool_reset_session= True,
    host              = os.environ['MYSQL_HOST'],
    port              = int(os.environ.get('MYSQL_PORT', 30603)),
    user              = os.environ['MYSQL_USER'],
    password          = os.environ['MYSQL_PASSWORD'],
    database          = os.environ['MYSQL_DATABASE']
)

# ── Minimal in-memory template ──
PAGE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Parcel Scanner (Web)</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; }
    input[type=text] { width: 300px; padding: 8px; font-size: 16px; }
    table { border-collapse: collapse; width: 100%; margin-top: 20px; }
    th, td { border: 1px solid #ccc; padding: 8px; text-align: left; }
    .flash { color: green; }
  </style>
</head>
<body>
  <h1>Parcel Scanner (Web)</h1>

  {% with messages = get_flashed_messages() %}
    {% if messages %}
      <div class="flash">{{ messages[0] }}</div>
    {% endif %}
  {% endwith %}

  <form action="{{ url_for('scan') }}" method="post" autocomplete="off">
    <label for="code">Scan Tracking Number:</label><br>
    <input type="text" name="code" id="code" autofocus required>
    <button type="submit">Submit</button>
  </form>

  <h2>Most Recent 10 Scans</h2>
  <table>
    <thead>
      <tr>
        <th>Tracking</th><th>Order #</th><th>Customer</th><th>Scan Time</th><th>Status</th><th>Order ID</th>
      </tr>
    </thead>
    <tbody>
      {% for row in scans %}
      <tr>
        <td>{{ row.tracking }}</td>
        <td>{{ row.order_number }}</td>
        <td>{{ row.customer_name }}</td>
        <td>{{ row.scan_time }}</td>
        <td>{{ row.status }}</td>
        <td>{{ row.order_id }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</body>
</html>
"""

def get_mysql_connection():
    """Helper to pull from pool."""
    return db_pool.get_connection()

@app.route('/', methods=['GET'])
def index():
    """Render the scan form and show the last 10 scans."""
    conn = get_mysql_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
      SELECT tracking, order_number, customer_name, scan_time, status, order_id
      FROM scans
      ORDER BY scan_time DESC
      LIMIT 10
    """)
    scans = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template_string(PAGE_TEMPLATE, scans=scans)

@app.route('/scan', methods=['POST'])
def scan():
    """Handle a new scan (INSERT into MySQL)."""
    code = request.form.get('code', '').strip()
    if not code:
        flash("No code received.", "error")
        return redirect(url_for('index'))

    # ── Here you’d do any “cleaning” or call your Shopify API to get order info ──
    # For brevity, let’s insert dummy placeholders for order_number, customer_name, order_id, status:
    order_number  = 'N/A'
    customer_name = 'No Shopify'
    order_id      = ''
    status        = 'Original'
    scan_time     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Insert into MySQL
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        sql = """
          INSERT INTO scans (tracking, order_number, customer_name, scan_time, status, order_id)
          VALUES (%s, %s, %s, %s, %s, %s)
        """
        cursor.execute(sql, (code, order_number, customer_name, scan_time, status, order_id))
        conn.commit()
        cursor.close()
        conn.close()
        flash(f"Recorded scan: {code}", "success")
    except mysql.connector.Error as e:
        flash(f"MySQL Error: {e}", "error")

    return redirect(url_for('index'))
    
if __name__ == "__main__":
    # For local testing:
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
