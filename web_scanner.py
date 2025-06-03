# web_scanner.py
import json
import os
from flask import Flask, request, redirect, url_for, render_template_string, flash
import mysql.connector
from mysql.connector import pooling
from datetime import datetime
from config import MYSQL_CONFIG


# Import your new ShopifyAPI class
from shopify_api import ShopifyAPI

app = Flask(__name__)
app.secret_key = os.urandom(24)  # for flashing messages

# ── MySQL connection pool ──
db_pool = mysql.connector.pooling.MySQLConnectionPool(
    pool_name="flask_pool",
    pool_size=5,
    pool_reset_session=True,
    host=os.environ['MYSQL_HOST'],
    port=int(os.environ.get('MYSQL_PORT', 30603)),
    user=os.environ['MYSQL_USER'],
    password=os.environ['MYSQL_PASSWORD'],
    database=os.environ['MYSQL_DATABASE']
)

def get_mysql_connection():
    """Helper to pull a connection from the pool."""
    return db_pool.get_connection()

# ── HTML template, unchanged ──
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
    .flash { color: green; margin-bottom: 10px; }
    .duplicate-row { background-color: #fdd; }
    .btn { padding: 8px 12px; font-size: 14px; margin-right: 8px; cursor: pointer; }
    .btn-delete { background: #c00; color: #fff; border: none; }
    .btn-batch  { background: #28a745; color: #fff; border: none; }
  </style>
</head>
<body>
  <h1>Parcel Scanner (Web)</h1>

  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for category, msg in messages %}
      <div class="flash">{{ msg }}</div>
    {% endfor %}
  {% endwith %}

  <form action="{{ url_for('scan') }}" method="post" autocomplete="off">
    <label for="code"><strong>Scan Tracking Number:</strong></label><br>
    <input type="text" name="code" id="code" autofocus required>
    <button type="submit" class="btn">Submit</button>
  </form>

  <h2>Most Recent 10 Scans</h2>

  <!-- Delete‐selected form -->
  <form action="{{ url_for('delete_scans') }}" method="post">
    <table>
      <thead>
        <tr>
          <th>Select</th>
          <th>Tracking</th><th>Order #</th><th>Customer</th>
          <th>Scan Time</th><th>Status</th><th>Order ID</th>
        </tr>
      </thead>
      <tbody>
        {% for row in scans %}
        <tr class="{{ 'duplicate-row' if row.status == 'Duplicate' else '' }}">
          <td><input type="checkbox" name="delete_orders" value="{{ row.order_number }}"></td>
          <td>{{ row.tracking_number }}</td>
          <td>{{ row.order_number }}</td>
          <td>{{ row.customer_name }}</td>
          <td>{{ row.scan_date }}</td>
          <td>{{ row.status }}</td>
          <td>{{ row.order_id }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    <br>
    <button type="submit" class="btn btn-delete">Delete Selected</button>
  </form>

  <br>
  <!-- Record Carrier Pick-up button -->
  <form action="{{ url_for('record_batch') }}" method="post">
    <button type="submit" class="btn btn-batch">Record Carrier Pick-up</button>
  </form>
</body>
</html>
"""

@app.route('/', methods=['GET'])
def index():
    """Render the scan form and show the last 10 scans, with duplicates highlighted."""
    conn = get_mysql_connection()
    cursor = conn.cursor(dictionary=True)

    # Select by order_number (not id). We still need order_number in the template.
    cursor.execute("""
      SELECT tracking_number,
             order_number,
             customer_name,
             scan_date,
             status,
             order_id
        FROM scans
       ORDER BY scan_date DESC
       LIMIT 10
    """)
    scans = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template_string(PAGE_TEMPLATE, scans=scans)


@app.route('/scan', methods=['POST'])
def scan():
    code = request.form.get('code','').strip()
    if not code:
        flash(("error","No code received."))
        return redirect(url_for('index'))

    order_number  = "N/A"
    customer_name = "No Shopify"
    order_id      = ""
    status        = "Original"
    now_str       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1) Duplicate check
    conn = get_mysql_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM scans WHERE tracking_number=%s", (code,))
    if cursor.fetchone()[0] > 0:
        status = "Duplicate"

    # 2) Shopify lookup
    try:
        shopify_api = ShopifyAPI()  # no args needed now
    except RuntimeError as e:
        # If env vars are missing, you’ll see this flash message
        flash(("error", f"Shopify config error: {e}"))
        return redirect(url_for('index'))

    info = shopify_api.get_order_by_tracking(code)
    if info.get("order_number") and info["order_number"] != "N/A":
        order_number  = info["order_number"]
        customer_name = info["customer_name"]
        order_id      = info["order_id"] or ""
        if status != "Duplicate":
            status = "Original"

    # 3) Insert into scans
    insert_sql = """
      INSERT INTO scans
        (tracking_number, order_number, customer_name, scan_date, status, order_id)
      VALUES (%s, %s, %s, %s, %s, %s)
    """
    cursor.execute(insert_sql, (code, order_number, customer_name, now_str, status, order_id))
    conn.commit()
    cursor.close()
    conn.close()

    flash(("success", f"Recorded scan: {code} (Status: {status})"))
    return redirect(url_for("index"))

@app.route('/delete_scans', methods=['POST'])
def delete_scans():
    """Delete any scans whose checkboxes (by order_number) were selected."""
    orders_to_delete = request.form.getlist('delete_orders')
    if not orders_to_delete:
        flash(("error", "No orders selected for deletion."))
        return redirect(url_for('index'))

    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        placeholders = ','.join(['%s'] * len(orders_to_delete))
        sql = f"DELETE FROM scans WHERE order_number IN ({placeholders})"
        cursor.execute(sql, orders_to_delete)
        conn.commit()
        cursor.close()
        conn.close()
        flash(("success", f"Deleted {len(orders_to_delete)} scan(s) by order number."))
    except mysql.connector.Error as e:
        flash(("error", f"MySQL Error: {e}"))
    return redirect(url_for('index'))

@app.route('/record_batch', methods=['POST'])
def record_batch():
    """
    Gather all existing scans, count them, concatenate the tracking numbers,
    and insert a new row into 'batches' (id, created_at, pkg_count, tracking_numbers).
    """
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT tracking_number FROM scans")
        rows = cursor.fetchall()
        tracking_list = [row['tracking_number'] for row in rows]
        pkg_count = len(tracking_list)
        tracking_csv = ','.join(tracking_list)

        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        insert_sql = """
          INSERT INTO batches (created_at, pkg_count, tracking_numbers)
          VALUES (%s, %s, %s)
        """
        cursor.execute(insert_sql, (created_at, pkg_count, tracking_csv))
        conn.commit()
        cursor.close()
        conn.close()
        flash(("success", f"Batch recorded: {pkg_count} parcel(s)."))
    except mysql.connector.Error as e:
        flash(("error", f"MySQL Error: {e}"))
    except Exception as ex:
        flash(("error", f"Unexpected error: {ex}"))
    return redirect(url_for('index'))

if __name__ == "__main__":
    # For local testing:
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
