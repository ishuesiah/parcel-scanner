# web_scanner.py
import os
from flask import (
    Flask,
    request,
    redirect,
    url_for,
    render_template_string,
    flash,
    session
)
import mysql.connector
from mysql.connector import pooling
from datetime import datetime

from shopify_api import ShopifyAPI  # unchanged

app = Flask(__name__)
app.secret_key = os.urandom(24)

# ── MySQL connection pool ──
db_pool = mysql.connector.pooling.MySQLConnectionPool(
    pool_name="flask_pool",
    pool_size=5,
    pool_reset_session=True,
    host=os.environ["MYSQL_HOST"],
    port=int(os.environ.get("MYSQL_PORT", 30603)),
    user=os.environ["MYSQL_USER"],
    password=os.environ["MYSQL_PASSWORD"],
    database=os.environ["MYSQL_DATABASE"],
)

def get_mysql_connection():
    return db_pool.get_connection()

# ── Shared navigation snippet ──
# web_scanner.py (excerpt)

NAVIGATION = """
<p>
  <a href="{{ url_for('index') }}">Home</a> |
  <a href="{{ url_for('all_batches') }}">View All Batches</a> |
  <a href="{{ url_for('all_scans') }}">View All Scans</a>
</p>
<hr>
"""

ALL_BATCHES_TEMPLATE = NAVIGATION + r'''
<style>
  table { width: 100%; border-collapse: collapse; margin-top: 10px; }
  th, td { border: 1px solid #ddd; padding: 8px; }
  th { background-color: #f2f2f2; text-align: left; }
  tr:nth-child(even) { background-color: #fafafa; }
  tr:hover { background-color: #f1f1f1; }
  td { vertical-align: top; }
</style>

<h2>All Batches</h2>
<table>
  <thead>
    <tr>
      <th>Batch ID</th>
      <th>Carrier</th>
      <th>Created At</th>
      <th>Pkg Count</th>
      <th>Tracking Numbers</th>
    </tr>
  </thead>
  <tbody>
    {% for b in batches %}
      <tr>
        <td>{{ b.id }}</td>
        <td>{{ b.carrier }}</td>
        <td>{{ b.created_at }}</td>
        <td>{{ b.pkg_count }}</td>
        <td style="max-width: 400px; word-break: break-word;">
          {{ b.tracking_numbers }}
        </td>
      </tr>
    {% endfor %}
  </tbody>
</table>
'''

ALL_SCANS_TEMPLATE = NAVIGATION + r'''
<style>
  table { width: 100%; border-collapse: collapse; margin-top: 10px; }
  th, td { border: 1px solid #ddd; padding: 8px; }
  th { background-color: #f2f2f2; text-align: left; }
  tr:nth-child(even) { background-color: #fafafa; }
  tr:hover { background-color: #f1f1f1; }
  td { vertical-align: top; }
  .duplicate-row { background-color: #fdecea !important; }
  .search-form { margin-top: 10px; margin-bottom: 5px; }
  .search-form input[type="text"] { padding: 6px; font-size: 14px; width: 200px; }
  .search-form button { padding: 6px 10px; font-size: 14px; }
  .search-form a { margin-left: 8px; font-size: 14px; text-decoration: none; color: #007bff; }
</style>

<h2>All Scans</h2>

<form class="search-form" method="get" action="{{ url_for('all_scans') }}">
  <label for="order_search"><strong>Search by Order #:</strong></label>
  <input type="text" name="order_number" id="order_search" value="{{ request.args.get('order_number','') }}">
  <button type="submit" class="btn">Search</button>
  {% if request.args.get('order_number') %}
    <a href="{{ url_for('all_scans') }}">Clear</a>
  {% endif %}
</form>

<table>
  <thead>
    <tr>
      <th>Tracking</th>
      <th>Order #</th>
      <th>Customer</th>
      <th>Scan Time</th>
      <th>Status</th>
      <th>Order ID</th>
      <th>Batch ID</th>
    </tr>
  </thead>
  <tbody>
    {% for s in scans %}
      <tr class="{{ 'duplicate-row' if s.status == 'Duplicate' else '' }}">
        <td>{{ s.tracking_number }}</td>
        <td>{{ s.order_number }}</td>
        <td>{{ s.customer_name }}</td>
        <td>{{ s.scan_date }}</td>
        <td>{{ s.status }}</td>
        <td>{{ s.order_id }}</td>
        <td>{{ s.batch_id or '' }}</td>
      </tr>
    {% endfor %}
  </tbody>
</table>
'''


# ── “All Scans” template, with search box ──
ALL_SCANS_TEMPLATE = NAVIGATION + """
<h2>All Scans</h2>

<form method="get" action="{{ url_for('all_scans') }}">
  <label for="order_search"><strong>Search by Order #:</strong></label>
  <input type="text" name="order_number" id="order_search" value="{{ request.args.get('order_number','') }}">
  <button type="submit" class="btn">Search</button>
  {% if request.args.get('order_number') %}
    &nbsp;<a href="{{ url_for('all_scans') }}">Clear</a>
  {% endif %}
</form>

<table>
  <thead>
    <tr>
      <th>Tracking</th><th>Order #</th><th>Customer</th>
      <th>Scan Time</th><th>Status</th><th>Order ID</th><th>Batch ID</th>
    </tr>
  </thead>
  <tbody>
    {% for s in scans %}
      <tr class="{{ 'duplicate-row' if s.status == 'Duplicate' else '' }}">
        <td>{{ s.tracking_number }}</td>
        <td>{{ s.order_number }}</td>
        <td>{{ s.customer_name }}</td>
        <td>{{ s.scan_date }}</td>
        <td>{{ s.status }}</td>
        <td>{{ s.order_id }}</td>
        <td>{{ s.batch_id or '' }}</td>
      </tr>
    {% endfor %}
  </tbody>
</table>
"""

@app.route("/", methods=["GET"])
def index():
    batch_id = session.get("batch_id")
    if not batch_id:
        return render_template_string(MAIN_TEMPLATE, current_batch=None, scans=[])

    conn = get_mysql_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
      SELECT id, created_at, carrier
        FROM batches
       WHERE id = %s
    """, (batch_id,))
    batch_row = cursor.fetchone()
    if not batch_row:
        session.pop("batch_id", None)
        cursor.close()
        conn.close()
        flash(("error", "Batch not found. Please start a new batch."))
        return redirect(url_for("index"))

    cursor.execute("""
      SELECT tracking_number, order_number, customer_name, scan_date, status, order_id
        FROM scans
       WHERE batch_id = %s
       ORDER BY scan_date DESC
       LIMIT 10
    """, (batch_id,))
    scans = cursor.fetchall()

    cursor.close()
    conn.close()
    return render_template_string(
        MAIN_TEMPLATE,
        current_batch=batch_row,
        scans=scans
    )

@app.route("/new_batch", methods=["POST"])
def new_batch():
    carrier = request.form.get("carrier", "").strip()
    if carrier not in ("UPS", "Canada Post", "DHL"):
        flash(("error", "Please select a valid carrier."))
        return redirect(url_for("index"))

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_mysql_connection()
    cursor = conn.cursor()
    cursor.execute("""
      INSERT INTO batches (created_at, pkg_count, tracking_numbers, carrier)
      VALUES (%s, %s, %s, %s)
    """, (created_at, 0, "", carrier))
    conn.commit()

    batch_id = cursor.lastrowid
    session["batch_id"] = batch_id

    cursor.close()
    conn.close()

    flash(("success", f"Started new {carrier} batch (ID {batch_id}). Scan parcels below."))
    return redirect(url_for("index"))

@app.route("/cancel_batch", methods=["GET"])
def cancel_batch():
    batch_id = session.pop("batch_id", None)
    if not batch_id:
        return redirect(url_for("index"))

    conn = get_mysql_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM scans WHERE batch_id = %s", (batch_id,))
    cursor.execute("DELETE FROM batches WHERE id = %s", (batch_id,))
    conn.commit()
    cursor.close()
    conn.close()

    flash(("success", f"Batch #{batch_id} canceled."))
    return redirect(url_for("index"))

@app.route("/scan", methods=["POST"])
def scan():
    code = request.form.get("code", "").strip()
    if not code:
        flash(("error", "No code received."))
        return redirect(url_for("index"))

    batch_id = session.get("batch_id")
    if not batch_id:
        flash(("error", "No batch open. Please start a new batch first."))
        return redirect(url_for("index"))

    order_number  = "N/A"
    customer_name = "No Shopify"
    order_id      = ""
    status        = "Original"
    now_str       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_mysql_connection()
    cursor = conn.cursor()

    cursor.execute("""
      SELECT COUNT(*) FROM scans
       WHERE tracking_number = %s AND batch_id = %s
    """, (code, batch_id))
    if cursor.fetchone()[0] > 0:
        status = "Duplicate"

    try:
        shopify_api = ShopifyAPI()
    except RuntimeError as e:
        flash(("error", f"Shopify config error: {e}"))
        cursor.close()
        conn.close()
        return redirect(url_for("index"))

    info = shopify_api.get_order_by_tracking(code)
    if info.get("order_number") and info["order_number"] != "N/A":
        order_number  = info["order_number"]
        customer_name = info["customer_name"]
        order_id      = info["order_id"] or ""
        if status != "Duplicate":
            status = "Found"

    cursor.execute("""
      INSERT INTO scans
        (tracking_number, order_number, customer_name, scan_date, status, order_id, batch_id)
      VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (code, order_number, customer_name, now_str, status, order_id, batch_id))
    conn.commit()

    cursor.close()
    conn.close()

    flash(("success", f"Recorded scan: {code} (Status: {status})"))
    return redirect(url_for("index"))

@app.route("/delete_scans", methods=["POST"])
def delete_scans():
    batch_id = session.get("batch_id")
    if not batch_id:
        flash(("error", "No batch open."))
        return redirect(url_for("index"))

    orders_to_delete = request.form.getlist("delete_orders")
    if not orders_to_delete:
        flash(("error", "No orders selected for deletion."))
        return redirect(url_for("index"))

    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        placeholders = ",".join(["%s"] * len(orders_to_delete))
        sql = f"""
          DELETE FROM scans
           WHERE order_number IN ({placeholders})
             AND batch_id = %s
        """
        params = orders_to_delete + [batch_id]
        cursor.execute(sql, params)
        conn.commit()
        cursor.close()
        conn.close()
        flash(("success", f"Deleted {len(orders_to_delete)} scan(s)."))
    except mysql.connector.Error as e:
        flash(("error", f"MySQL Error: {e}"))
    return redirect(url_for("index"))

@app.route("/record_batch", methods=["POST"])
def record_batch():
    batch_id = session.pop("batch_id", None)
    if not batch_id:
        flash(("error", "No batch open."))
        return redirect(url_for("index"))

    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
          SELECT tracking_number
            FROM scans
           WHERE batch_id = %s
        """, (batch_id,))
        rows = cursor.fetchall()
        tracking_list = [row["tracking_number"] for row in rows]
        pkg_count = len(tracking_list)
        tracking_csv = ",".join(tracking_list)

        cursor.execute("""
          UPDATE batches
             SET pkg_count = %s,
                 tracking_numbers = %s
           WHERE id = %s
        """, (pkg_count, tracking_csv, batch_id))
        conn.commit()
        cursor.close()
        conn.close()
        flash(("success", f"Batch #{batch_id} recorded with {pkg_count} parcel(s)."))
    except mysql.connector.Error as e:
        flash(("error", f"MySQL Error: {e}"))

    return redirect(url_for("index"))

@app.route("/all_batches", methods=["GET"])
def all_batches():
    conn = get_mysql_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("""
      SELECT id, carrier, created_at, pkg_count, tracking_numbers
        FROM batches
       ORDER BY created_at DESC
    """)
    batches = cursor.fetchall()
    cursor.close()
    conn.close()
    return render_template_string(ALL_BATCHES_TEMPLATE, batches=batches)

@app.route("/all_scans", methods=["GET"])
def all_scans():
    order_search = request.args.get("order_number", "").strip()

    conn = get_mysql_connection()
    cursor = conn.cursor(dictionary=True)

    if order_search:
        cursor.execute("""
          SELECT tracking_number, order_number, customer_name, scan_date, status, order_id, batch_id
            FROM scans
           WHERE order_number = %s
           ORDER BY scan_date DESC
        """, (order_search,))
    else:
        cursor.execute("""
          SELECT tracking_number, order_number, customer_name, scan_date, status, order_id, batch_id
            FROM scans
           ORDER BY scan_date DESC
        """)
    scans = cursor.fetchall()

    cursor.close()
    conn.close()
    return render_template_string(ALL_SCANS_TEMPLATE, scans=scans)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
