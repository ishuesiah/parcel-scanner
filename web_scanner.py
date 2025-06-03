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

# Import your Shopify lookup (unchanged)
from shopify_api import ShopifyAPI

app = Flask(__name__)
app.secret_key = os.urandom(24)  # needed for session + flashing messages

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
    """Pull a connection from the pool."""
    return db_pool.get_connection()

# ── HTML template with two “modes”: no‐batch vs in‐batch ──
PAGE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Parcel Scanner (Web)</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; }
    input[type=text] { width: 300px; padding: 8px; font-size: 16px; }
    select { font-size: 16px; padding: 4px; }
    table { border-collapse: collapse; width: 100%; margin-top: 20px; }
    th, td { border: 1px solid #ccc; padding: 8px; text-align: left; }
    .flash { color: green; margin-bottom: 10px; }
    .duplicate-row { background-color: #fdd; }
    .btn { padding: 8px 12px; font-size: 14px; margin-right: 8px; cursor: pointer; }
    .btn-new   { background: #007bff; color: #fff; border: none; }
    .btn-delete{ background: #c00; color: #fff; border: none; }
    .btn-batch { background: #28a745; color: #fff; border: none; }
  </style>
</head>
<body>
  <h1>Parcel Scanner (Web)</h1>

  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for category, msg in messages %}
      <div class="flash">{{ msg }}</div>
    {% endfor %}
  {% endwith %}

  {# === If no batch is open, show only New Batch form === #}
  {% if not current_batch %}
    <h2>Create New Batch</h2>
    <form action="{{ url_for('new_batch') }}" method="post">
      <label for="carrier"><strong>Carrier:</strong></label>
      <select name="carrier" id="carrier" required>
        <option value="">-- Select Carrier --</option>
        <option value="UPS">UPS</option>
        <option value="Canada Post">Canada Post</option>
        <option value="DHL">DHL</option>
      </select>
      <button type="submit" class="btn btn-new">Start Batch</button>
    </form>

  {# === If a batch is open, show scanning UI for that batch === #}
  {% else %}
    <h2>Batch #{{ current_batch.id }}  (Carrier: {{ current_batch.carrier }})</h2>
    <p>
      <em>Batch created at: {{ current_batch.created_at }}</em>
      &nbsp;|&nbsp;
      <a href="{{ url_for('cancel_batch') }}">Cancel This Batch</a>
    </p>

    <!-- Scan form for the open batch -->
    <form action="{{ url_for('scan') }}" method="post" autocomplete="off">
      <label for="code"><strong>Scan Tracking Number:</strong></label><br>
      <input type="text" name="code" id="code" autofocus required>
      <button type="submit" class="btn">Submit</button>
    </form>

    <h3>Scans in This Batch (Last 10)</h3>
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
              <td>
                <input type="checkbox" name="delete_orders" value="{{ row.order_number }}">
              </td>
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
    <!-- Record Carrier Pick-up to finalize this batch -->
    <form action="{{ url_for('record_batch') }}" method="post">
      <button type="submit" class="btn btn-batch">Record Carrier Pick-up</button>
    </form>
  {% endif %}
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    """
    If there is a batch open in session:
      • Fetch last 10 scans WHERE scans.batch_id = current_batch_id.
      • Pass current_batch details + those scans into the template.
    Otherwise (no batch open), just render the “New Batch” form.
    """
    batch_id = session.get("batch_id")
    if not batch_id:
        # No batch in session → render “create new batch” form
        return render_template_string(PAGE_TEMPLATE, current_batch=None, scans=[])

    # Otherwise, fetch details for this batch and last 10 scans
    conn = get_mysql_connection()
    cursor = conn.cursor(dictionary=True)

    # 1) Get batch metadata (created_at, carrier)
    cursor.execute("""
      SELECT id, created_at, carrier
        FROM batches
       WHERE id = %s
    """, (batch_id,))
    batch_row = cursor.fetchone()

    if not batch_row:
        # If somehow batch_id is invalid, clear session and redirect
        session.pop("batch_id", None)
        cursor.close()
        conn.close()
        flash(("error", "Batch not found. Please start a new batch."))
        return redirect(url_for("index"))

    # 2) Get last 10 scans in this batch
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

    # Pass batch metadata and scans into template
    return render_template_string(
        PAGE_TEMPLATE,
        current_batch=batch_row,
        scans=scans
    )

@app.route("/new_batch", methods=["POST"])
def new_batch():
    """
    Create a new batch row (with carrier + created_at), then store its ID in session.
    Redirect to index → where the scanning UI will appear.
    """
    carrier = request.form.get("carrier", "").strip()
    if carrier not in ("UPS", "Canada Post", "DHL"):
        flash(("error", "Please select a valid carrier."))
        return redirect(url_for("index"))

    # Insert an “empty” batch now; we'll fill pkg_count+tracking_numbers later.
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_mysql_connection()
    cursor = conn.cursor()
    cursor.execute("""
      INSERT INTO batches (created_at, pkg_count, tracking_numbers, carrier)
      VALUES (%s, %s, %s, %s)
    """, (created_at, 0, "", carrier))
    conn.commit()

    # Grab the new batch’s auto-increment ID, save in session:
    batch_id = cursor.lastrowid
    session["batch_id"] = batch_id

    cursor.close()
    conn.close()

    flash(("success", f"Started new {carrier} batch (ID {batch_id}). Scan parcels below."))
    return redirect(url_for("index"))

@app.route("/cancel_batch", methods=["GET"])
def cancel_batch():
    """
    If user clicks “Cancel This Batch,” we:
      • Delete any scans already inserted with this batch_id
      • Delete the empty (or partial) batch row from batches
      • Clear session['batch_id']
      • Redirect back to index (New Batch form)
    """
    batch_id = session.pop("batch_id", None)
    if not batch_id:
        return redirect(url_for("index"))

    conn = get_mysql_connection()
    cursor = conn.cursor()
    # 1) delete any scans belonging to this batch
    cursor.execute("DELETE FROM scans WHERE batch_id = %s", (batch_id,))
    # 2) delete the batch row itself
    cursor.execute("DELETE FROM batches WHERE id = %s", (batch_id,))
    conn.commit()
    cursor.close()
    conn.close()

    flash(("success", f"Batch #{batch_id} canceled."))
    return redirect(url_for("index"))

@app.route("/scan", methods=["POST"])
def scan():
    """
    Handle a new scan while a batch is open:
      - Insert into scans (with batch_id)
      - Lookup order via ShopifyAPI.get_order_by_tracking(...)
      - Mark “Duplicate” if tracking_number already in this batch
    """
    code = request.form.get("code", "").strip()
    if not code:
        flash(("error", "No code received."))
        return redirect(url_for("index"))

    batch_id = session.get("batch_id")
    if not batch_id:
        flash(("error", "No batch open. Please start a new batch first."))
        return redirect(url_for("index"))

    # Defaults for this new scan
    order_number  = "N/A"
    customer_name = "No Shopify"
    order_id      = ""
    status        = "Original"
    now_str       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_mysql_connection()
    cursor = conn.cursor()

    # 1) Check duplicate within this same batch
    cursor.execute("""
      SELECT COUNT(*) FROM scans
       WHERE tracking_number = %s AND batch_id = %s
    """, (code, batch_id))
    if cursor.fetchone()[0] > 0:
        status = "Duplicate"

    # 2) Lookup order via ShopifyAPI
    try:
        shopify_api = ShopifyAPI()  # reads credentials from ENV
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

    # 3) Insert scan into scans (with batch_id)
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
    """
    Delete selected scans by order_number (within the current batch).
    """
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
        # Add batch_id to the params
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
    """
    Finalize the current batch:
      1) Count all scans for this batch_id
      2) Concatenate their tracking_numbers
      3) UPDATE the batches row with pkg_count + tracking_numbers
      4) Clear session['batch_id']
    """
    batch_id = session.pop("batch_id", None)
    if not batch_id:
        flash(("error", "No batch open."))
        return redirect(url_for("index"))

    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)

        # 1) Fetch all scans in this batch to build the batch summary
        cursor.execute("""
          SELECT tracking_number
            FROM scans
           WHERE batch_id = %s
        """, (batch_id,))
        rows = cursor.fetchall()
        tracking_list = [row["tracking_number"] for row in rows]
        pkg_count = len(tracking_list)
        tracking_csv = ",".join(tracking_list)

        # 2) Update the already‐created batches row
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


if __name__ == "__main__":
    # For local testing:
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
