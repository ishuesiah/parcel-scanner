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

from shopify_api import ShopifyAPI  # Assumes shopify_api.py is alongside this file

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

# Read shop URL for building admin links
SHOP_URL = os.environ.get("SHOP_URL", "").rstrip("/")


# ─────────────────────────────────────────────────────────────────────────────
# ── Templates ─────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

MAIN_TEMPLATE = r'''
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>H&O Parcel Scans</title>
  <style>
    /* Reset & Base */
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }
    html, body {
      height: 100%;
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      background-color: #f5f6fa;
      color: #333;
    }

    /* Layout */
    .container {
      display: flex;
      height: 100vh;
    }

    /* ── SIDEBAR ── */
    .sidebar {
      width: 240px;
      background-color: #ffffff;
      border-right: 1px solid #e0e0e0;
      display: flex;
      flex-direction: column;
      padding: 24px 16px;
    }
    .sidebar h1 {
      font-size: 1.25rem;
      font-weight: bold;
      margin-bottom: 16px;
      color: #2c3e50;
    }
    .sidebar ul {
      list-style: none;
      margin-top: 8px;
    }
    .sidebar li {
      margin-bottom: 16px;
    }
    .sidebar a {
      text-decoration: none;
      color: #2d85f8;
      font-size: 1rem;
      font-weight: 500;
    }
    .sidebar a:hover {
      text-decoration: underline;
    }

    /* ── MAIN CONTENT ── */
    .main-content {
      flex: 1;
      overflow-y: auto;
      padding: 24px;
    }
    .flash {
      padding: 10px 14px;
      margin-bottom: 16px;
      border-radius: 4px;
      background-color: #e0f7e9;
      color: #2f7a45;
      font-weight: 500;
      border: 1px solid #b2e6c2;
    }
    h2 {
      font-size: 1.5rem;
      color: #2c3e50;
      margin-bottom: 16px;
    }
    form label {
      font-weight: 600;
      color: #333;
    }
    form input[type="text"], form select {
      width: 300px;
      padding: 8px;
      border: 1px solid #ccc;
      border-radius: 4px;
      margin-top: 4px;
      margin-bottom: 12px;
      font-size: 0.95rem;
    }
    .btn {
      padding: 8px 12px;
      font-size: 0.9rem;
      border: none;
      border-radius: 4px;
      cursor: pointer;
    }
    .btn-new {
      background-color: #2d85f8;
      color: white;
    }
    .btn-delete {
      background-color: #e74c3c;
      color: white;
    }
    .btn-batch {
      background-color: #27ae60;
      color: white;
    }
    .btn:hover {
      opacity: 0.92;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 12px;
    }
    th, td {
      border: 1px solid #ddd;
      padding: 10px 8px;
      font-size: 0.93rem;
      color: #34495e;
    }
    th {
      background-color: #f2f2f2;
      text-align: left;
      font-weight: 600;
    }
    tr:nth-child(even) {
      background-color: #fafafa;
    }
    tr:hover {
      background-color: #f1f1f1;
    }
    .duplicate-row {
      background-color: #fdecea !important;
    }
    td a {
      color: #2d85f8;
      text-decoration: none;
      font-weight: 500;
    }
    td a:hover {
      text-decoration: underline;
    }
    td input[type="checkbox"] {
      width: 16px;
      height: 16px;
    }
    .batch-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      margin-bottom: 16px;
    }
    .batch-header h2 {
      font-size: 1.5rem;
      color: #2c3e50;
    }
    .batch-nav {
      display: flex;
      gap: 24px;
      font-size: 0.95rem;
    }
    .batch-nav a {
      color: #2d85f8;
      text-decoration: none;
      font-weight: 500;
    }
    .batch-nav a:hover {
      text-decoration: underline;
    }
  </style>
</head>
<body>

  <div class="container">

    <!-- ── SIDEBAR ── -->
    <div class="sidebar">
      <h1>H&amp;O Parcel Scans</h1>
      <ul>
        <li><a href="{{ url_for('index') }}">New Batch</a></li>
        <li><a href="{{ url_for('all_batches') }}">Recorded Pick‐ups</a></li>
        <li><a href="{{ url_for('all_scans') }}">All Scans</a></li>
      </ul>
    </div>
    <!-- ── END SIDEBAR ── -->


    <!-- ── MAIN CONTENT ── -->
    <div class="main-content">

      {% with messages = get_flashed_messages(with_categories=true) %}
        {% for category, msg in messages %}
          <div class="flash">{{ msg }}</div>
        {% endfor %}
      {% endwith %}

      {% if not current_batch %}
        <h2>Create New Batch</h2>
        <form action="{{ url_for('new_batch') }}" method="post">
          <label for="carrier"><strong>Carrier:</strong></label><br>
          <select name="carrier" id="carrier" required>
            <option value="">-- Select Carrier --</option>
            <option value="UPS">UPS</option>
            <option value="Canada Post">Canada Post</option>
            <option value="DHL">DHL</option>
          </select>
          <br>
          <button type="submit" class="btn btn-new">Start Batch</button>
        </form>

      {% else %}
        <div class="batch-header">
          <h2>Batch #{{ current_batch.id }} (Carrier: {{ current_batch.carrier }})</h2>
        </div>

        <p style="margin-bottom: 16px; color: #666; font-size: 0.9rem;">
          <em>Batch created at: {{ current_batch.created_at }}</em>
          &nbsp;|&nbsp;
          <a href="{{ url_for('cancel_batch') }}" style="color:#e74c3c; text-decoration:none;">
            Cancel This Batch
          </a>
        </p>

        <!-- Scan form -->
        <form action="{{ url_for('scan') }}" method="post" autocomplete="off">
          <label for="code"><strong>Scan Tracking Number:</strong></label><br>
          <input type="text" name="code" id="code" autofocus required>
          <button type="submit" class="btn" style="margin-left: 8px;">Submit</button>
        </form>

        <!-- Scans table -->
        <h3 style="margin-top: 24px; color:#2c3e50;">Scans in This Batch</h3>
        <form action="{{ url_for('delete_scans') }}" method="post">
          <table>
            <thead>
              <tr>
                <th style="width: 40px;"> </th>
                <th>Tracking</th>
                <th>Carrier</th>
                <th>Order #</th>
                <th>Customer</th>
                <th>Scan Time</th>
                <th>Status</th>
                <th>Order ID</th>
              </tr>
            </thead>
            <tbody>
              {% for row in scans %}
                <tr class="{{ 'duplicate-row' if row.status == 'Duplicate' else '' }}">
                  <td>
                    <input type="checkbox" name="delete_orders" value="{{ row.order_number }}">
                  </td>
                  <td style="font-weight: 500;">{{ row.tracking_number }}</td>
                  <td>{{ row.carrier }}</td>
                  <td>
                    <a href="https://{{ shop_url }}/admin/orders/{{ row.order_id }}" target="_blank">
                      {{ row.order_number }}
                    </a>
                  </td>
                  <td>
                    <a href="https://{{ shop_url }}/admin/orders/{{ row.order_id }}" target="_blank">
                      {{ row.customer_name }}
                    </a>
                  </td>
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

        <br><br>
        <form action="{{ url_for('record_batch') }}" method="post">
          <button type="submit" class="btn btn-batch">Record Carrier Pick‐up</button>
        </form>

      {% endif %}

    </div> <!-- .main-content -->

  </div> <!-- .container -->

</body>
</html>
'''


ALL_BATCHES_TEMPLATE = r'''
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>All Batches – H&O Parcel Scans</title>
  <style>
    /* Reset & Base */
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }
    html, body {
      height: 100%;
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      background-color: #f5f6fa;
      color: #333;
    }

    /* Layout */
    .container {
      display: flex;
      height: 100vh;
    }

    /* ── SIDEBAR ── */
    .sidebar {
      width: 240px;
      background-color: #ffffff;
      border-right: 1px solid #e0e0e0;
      display: flex;
      flex-direction: column;
      padding: 24px 16px;
    }
    .sidebar h1 {
      font-size: 1.25rem;
      font-weight: bold;
      margin-bottom: 16px;
      color: #2c3e50;
    }
    .sidebar ul {
      list-style: none;
      margin-top: 8px;
    }
    .sidebar li {
      margin-bottom: 16px;
    }
    .sidebar a {
      text-decoration: none;
      color: #2d85f8;
      font-size: 1rem;
      font-weight: 500;
    }
    .sidebar a:hover {
      text-decoration: underline;
    }

    /* ── MAIN CONTENT ── */
    .main-content {
      flex: 1;
      overflow-y: auto;
      padding: 24px;
    }
    .flash {
      padding: 10px 14px;
      margin-bottom: 16px;
      border-radius: 4px;
      background-color: #e0f7e9;
      color: #2f7a45;
      font-weight: 500;
      border: 1px solid #b2e6c2;
    }
    h2 {
      font-size: 1.5rem;
      color: #2c3e50;
      margin-bottom: 16px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 12px;
    }
    th, td {
      border: 1px solid #ddd;
      padding: 10px 8px;
      font-size: 0.93rem;
      color: #34495e;
    }
    th {
      background-color: #f2f2f2;
      text-align: left;
      font-weight: 600;
    }
    tr:nth-child(even) {
      background-color: #fafafa;
    }
    tr:hover {
      background-color: #f1f1f1;
    }
    .batch-link {
      color: #2d85f8;
      text-decoration: none;
      font-weight: 500;
    }
    .batch-link:hover {
      text-decoration: underline;
    }
  </style>
</head>
<body>

  <div class="container">

    <!-- ── SIDEBAR ── -->
    <div class="sidebar">
      <h1>H&amp;O Parcel Scans</h1>
      <ul>
        <li><a href="{{ url_for('index') }}">New Batch</a></li>
        <li><a href="{{ url_for('all_batches') }}">Recorded Pick‐ups</a></li>
        <li><a href="{{ url_for('all_scans') }}">All Scans</a></li>
      </ul>
    </div>
    <!-- ── END SIDEBAR ── -->

    <!-- ── MAIN CONTENT ── -->
    <div class="main-content">

      {% with messages = get_flashed_messages(with_categories=true) %}
        {% for category, msg in messages %}
          <div class="flash">{{ msg }}</div>
        {% endfor %}
      {% endwith %}

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
              <td>
                <a class="batch-link" href="{{ url_for('view_batch', batch_id=b.id) }}">
                  {{ b.id }}
                </a>
              </td>
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

    </div> <!-- .main-content -->

  </div> <!-- .container -->

</body>
</html>
'''


BATCH_VIEW_TEMPLATE = r'''
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Batch #{{ batch.id }} – H&O Parcel Scans</title>
  <style>
    /* Reset & Base */
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }
    html, body {
      height: 100%;
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      background-color: #f5f6fa;
      color: #333;
    }

    /* Layout */
    .container {
      display: flex;
      height: 100vh;
    }

    /* ── SIDEBAR ── */
    .sidebar {
      width: 240px;
      background-color: #ffffff;
      border-right: 1px solid #e0e0e0;
      display: flex;
      flex-direction: column;
      padding: 24px 16px;
    }
    .sidebar h1 {
      font-size: 1.25rem;
      font-weight: bold;
      margin-bottom: 16px;
      color: #2c3e50;
    }
    .sidebar ul {
      list-style: none;
      margin-top: 8px;
    }
    .sidebar li {
      margin-bottom: 16px;
    }
    .sidebar a {
      text-decoration: none;
      color: #2d85f8;
      font-size: 1rem;
      font-weight: 500;
    }
    .sidebar a:hover {
      text-decoration: underline;
    }

    /* ── MAIN CONTENT ── */
    .main-content {
      flex: 1;
      overflow-y: auto;
      padding: 24px;
    }
    .flash {
      padding: 10px 14px;
      margin-bottom: 16px;
      border-radius: 4px;
      background-color: #e0f7e9;
      color: #2f7a45;
      font-weight: 500;
      border: 1px solid #b2e6c2;
    }
    .batch-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      margin-bottom: 16px;
    }
    .batch-header h2 {
      font-size: 1.5rem;
      color: #2c3e50;
    }
    .batch-header .back-link {
      color: #2d85f8;
      text-decoration: none;
      font-size: 0.95rem;
      font-weight: 500;
    }
    .batch-header .back-link:hover {
      text-decoration: underline;
    }
    p.meta {
      color: #666;
      font-size: 0.9rem;
      margin-bottom: 16px;
    }
    h3 {
      color: #2c3e50;
      margin-top: 16px;
      margin-bottom: 8px;
      font-size: 1.25rem;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 12px;
    }
    th, td {
      border: 1px solid #ddd;
      padding: 10px 8px;
      font-size: 0.93rem;
      color: #34495e;
    }
    th {
      background-color: #f2f2f2;
      text-align: left;
      font-weight: 600;
    }
    tr:nth-child(even) {
      background-color: #fafafa;
    }
    tr:hover {
      background-color: #f1f1f1;
    }
    .duplicate-row {
      background-color: #fdecea !important;
    }
    td a {
      color: #2d85f8;
      text-decoration: none;
      font-weight: 500;
    }
    td a:hover {
      text-decoration: underline;
    }
  </style>
</head>
<body>

  <div class="container">

    <!-- ── SIDEBAR ── -->
    <div class="sidebar">
      <h1>H&amp;O Parcel Scans</h1>
      <ul>
        <li><a href="{{ url_for('index') }}">New Batch</a></li>
        <li><a href="{{ url_for('all_batches') }}">Recorded Pick‐ups</a></li>
        <li><a href="{{ url_for('all_scans') }}">All Scans</a></li>
      </ul>
    </div>
    <!-- ── END SIDEBAR ── -->

    <!-- ── MAIN CONTENT ── -->
    <div class="main-content">

      {% with messages = get_flashed_messages(with_categories=true) %}
        {% for category, msg in messages %}
          <div class="flash">{{ msg }}</div>
        {% endfor %}
      {% endwith %}

      <div class="batch-header">
        <h2>Batch #{{ batch.id }} (Carrier: {{ batch.carrier }})</h2>
        <a href="{{ url_for('all_batches') }}" class="back-link">← Back to All Batches</a>
      </div>

      <p class="meta">
        <em>Created at: {{ batch.created_at }}</em><br>
        <em>Parcel Count: {{ batch.pkg_count }}</em><br>
        <em>Tracking Numbers: {{ batch.tracking_numbers }}</em>
      </p>

      <h3>All Scans in Batch {{ batch.id }}</h3>
      <table>
        <thead>
          <tr>
            <th>Tracking</th>
            <th>Carrier</th>
            <th>Order #</th>
            <th>Customer</th>
            <th>Scan Time</th>
            <th>Status</th>
            <th>Order ID</th>
          </tr>
        </thead>
        <tbody>
          {% for row in scans %}
            <tr class="{{ 'duplicate-row' if row.status == 'Duplicate' else '' }}">
              <td>{{ row.tracking_number }}</td>
              <td>{{ row.carrier }}</td>
              <td>
                <a href="https://{{ shop_url }}/admin/orders/{{ row.order_id }}" target="_blank">
                  {{ row.order_number }}
                </a>
              </td>
              <td>
                <a href="https://{{ shop_url }}/admin/orders/{{ row.order_id }}" target="_blank">
                  {{ row.customer_name }}
                </a>
              </td>
              <td>{{ row.scan_date }}</td>
              <td>{{ row.status }}</td>
              <td>{{ row.order_id }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>

    </div> <!-- .main-content -->

  </div> <!-- .container -->

</body>
</html>
'''


ALL_SCANS_TEMPLATE = r'''
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>All Scans – H&O Parcel Scans</title>
  <style>
    /* Reset & Base */
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }
    html, body {
      height: 100%;
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      background-color: #f5f6fa;
      color: #333;
    }

    /* Layout */
    .container {
      display: flex;
      height: 100vh;
    }

    /* ── SIDEBAR ── */
    .sidebar {
      width: 240px;
      background-color: #ffffff;
      border-right: 1px solid #e0e0e0;
      display: flex;
      flex-direction: column;
      padding: 24px 16px;
    }
    .sidebar h1 {
      font-size: 1.25rem;
      font-weight: bold;
      margin-bottom: 16px;
      color: #2c3e50;
    }
    .sidebar ul {
      list-style: none;
      margin-top: 8px;
    }
    .sidebar li {
      margin-bottom: 16px;
    }
    .sidebar a {
      text-decoration: none;
      color: #2d85f8;
      font-size: 1rem;
      font-weight: 500;
    }
    .sidebar a:hover {
      text-decoration: underline;
    }

    /* ── MAIN CONTENT ── */
    .main-content {
      flex: 1;
      overflow-y: auto;
      padding: 24px;
    }
    .flash {
      padding: 10px 14px;
      margin-bottom: 16px;
      border-radius: 4px;
      background-color: #e0f7e9;
      color: #2f7a45;
      font-weight: 500;
      border: 1px solid #b2e6c2;
    }
    h2 {
      font-size: 1.5rem;
      color: #2c3e50;
      margin-bottom: 16px;
    }
    .search-form {
      margin-top: 10px;
      margin-bottom: 5px;
    }
    .search-form input[type="text"] {
      padding: 6px;
      font-size: 14px;
      width: 200px;
      border: 1px solid #ccc;
      border-radius: 4px;
    }
    .search-form button {
      padding: 6px 10px;
      font-size: 14px;
      border: none;
      border-radius: 4px;
      background-color: #2d85f8;
      color: #fff;
      cursor: pointer;
      margin-left: 6px;
    }
    .search-form a {
      margin-left: 12px;
      font-size: 14px;
      text-decoration: none;
      color: #2d85f8;
      font-weight: 500;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 12px;
    }
    th, td {
      border: 1px solid #ddd;
      padding: 10px 8px;
      font-size: 0.93rem;
      color: #34495e;
    }
    th {
      background-color: #f2f2f2;
      text-align: left;
      font-weight: 600;
    }
    tr:nth-child(even) {
      background-color: #fafafa;
    }
    tr:hover {
      background-color: #f1f1f1;
    }
    .duplicate-row {
      background-color: #fdecea !important;
    }
    td a {
      color: #2d85f8;
      text-decoration: none;
      font-weight: 500;
    }
    td a:hover {
      text-decoration: underline;
    }
  </style>
</head>
<body>

  <div class="container">

    <!-- ── SIDEBAR ── -->
    <div class="sidebar">
      <h1>H&amp;O Parcel Scans</h1>
      <ul>
        <li><a href="{{ url_for('index') }}">New Batch</a></li>
        <li><a href="{{ url_for('all_batches') }}">Recorded Pick‐ups</a></li>
        <li><a href="{{ url_for('all_scans') }}">All Scans</a></li>
      </ul>
    </div>
    <!-- ── END SIDEBAR ── -->

    <!-- ── MAIN CONTENT ── -->
    <div class="main-content">

      {% with messages = get_flashed_messages(with_categories=true) %}
        {% for category, msg in messages %}
          <div class="flash">{{ msg }}</div>
        {% endfor %}
      {% endwith %}

      <h2>All Scans</h2>

      <form class="search-form" method="get" action="{{ url_for('all_scans') }}">
        <label for="order_search"><strong>Search by Order # or Customer Name:</strong></label>
        <input type="text" name="order_number" id="order_search" value="{{ request.args.get('order_number','') }}">
        <button type="submit">Search</button>
        {% if request.args.get('order_number') %}
          <a href="{{ url_for('all_scans') }}">Clear</a>
        {% endif %}
      </form>

      <table>
        <thead>
          <tr>
            <th>Tracking</th>
            <th>Carrier</th>
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
              <td>{{ s.carrier }}</td>
              <td>
                <a href="https://{{ shop_url }}/admin/orders/{{ s.order_id }}" target="_blank">
                  {{ s.order_number }}
                </a>
              </td>
              <td>
                <a href="https://{{ shop_url }}/admin/orders/{{ s.order_id }}" target="_blank">
                  {{ s.customer_name }}
                </a>
              </td>
              <td>{{ s.scan_date }}</td>
              <td>{{ s.status }}</td>
              <td>{{ s.order_id }}</td>
              <td>{{ s.batch_id or '' }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>

    </div> <!-- .main-content -->

  </div> <!-- .container -->

</body>
</html>
'''


# ─────────────────────────────────────────────────────────────────────────────
# ── Routes ────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────


@app.route("/", methods=["GET"])
def index():
    batch_id = session.get("batch_id")
    if not batch_id:
        # No batch open → show “Create New Batch”
        return render_template_string(
            MAIN_TEMPLATE,
            current_batch=None,
            scans=[],
            shop_url=SHOP_URL
        )

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
      SELECT tracking_number,
             carrier,
             order_number,
             customer_name,
             scan_date,
             status,
             order_id
        FROM scans
       WHERE batch_id = %s
       ORDER BY scan_date DESC
    """, (batch_id,))
    scans = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template_string(
        MAIN_TEMPLATE,
        current_batch=batch_row,
        scans=scans,
        shop_url=SHOP_URL
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

    conn = get_mysql_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT carrier FROM batches WHERE id = %s", (batch_id,))
    batch_carrier = cursor.fetchone()[0] or ""
    cursor.close()

    if batch_carrier == "Canada Post":
        if len(code) > 12:
            code = code[7:-5]
        else:
            code = ""

    order_number  = "N/A"
    customer_name = "No Shopify"
    order_id      = ""
    status        = "Original"
    now_str       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if code.startswith("1ZAC"):
        scan_carrier = "UPS"
    elif code.startswith("2016"):
        scan_carrier = "Canada Post"
    else:
        scan_carrier = ""

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
            status = "Original"

    cursor.execute("""
      INSERT INTO scans
        (tracking_number, carrier, order_number, customer_name, scan_date, status, order_id, batch_id)
      VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (code, scan_carrier, order_number, customer_name, now_str, status, order_id, batch_id))
    conn.commit()

    cursor.close()
    conn.close()

    flash(("success", f"Recorded scan: {code} (Status: {status}, Carrier: {scan_carrier})"))
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
    return render_template_string(
        ALL_BATCHES_TEMPLATE,
        batches=batches,
        shop_url=SHOP_URL
    )


@app.route("/view_batch/<int:batch_id>", methods=["GET"])
def view_batch(batch_id):
    conn = get_mysql_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
      SELECT id, carrier, created_at, pkg_count, tracking_numbers
        FROM batches
       WHERE id = %s
    """, (batch_id,))
    batch = cursor.fetchone()
    if not batch:
        cursor.close()
        conn.close()
        flash(("error", f"Batch #{batch_id} not found."))
        return redirect(url_for("all_batches"))

    cursor.execute("""
      SELECT tracking_number,
             carrier,
             order_number,
             customer_name,
             scan_date,
             status,
             order_id
        FROM scans
       WHERE batch_id = %s
       ORDER BY scan_date DESC
    """, (batch_id,))
    scans = cursor.fetchall()

    cursor.close()
    conn.close()
    return render_template_string(
        BATCH_VIEW_TEMPLATE,
        batch=batch,
        scans=scans,
        shop_url=SHOP_URL
    )


@app.route("/all_scans", methods=["GET"])
def all_scans():
    order_search = request.args.get("order_number", "").strip()

    conn = get_mysql_connection()
    cursor = conn.cursor(dictionary=True)

    if order_search:
        like_pattern = f"%{order_search}%"
        cursor.execute("""
          SELECT tracking_number,
                 carrier,
                 order_number,
                 customer_name,
                 scan_date,
                 status,
                 order_id,
                 batch_id
            FROM scans
           WHERE order_number = %s
              OR LOWER(customer_name) LIKE LOWER(%s)
           ORDER BY scan_date DESC
        """, (order_search, like_pattern))
    else:
        cursor.execute("""
          SELECT tracking_number,
                 carrier,
                 order_number,
                 customer_name,
                 scan_date,
                 status,
                 order_id,
                 batch_id
            FROM scans
           ORDER BY scan_date DESC
        """)
    scans = cursor.fetchall()

    cursor.close()
    conn.close()
    return render_template_string(
        ALL_SCANS_TEMPLATE,
        scans=scans,
        shop_url=SHOP_URL
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
