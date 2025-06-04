# web_scanner.py
import os
import requests
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

# Read application password from environment (e.g. set APP_PASSWORD in Kinsta)
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

# Read ShipStation credentials from environment
SHIPSTATION_API_KEY = os.environ.get("SHIPSTATION_API_KEY", "")
SHIPSTATION_API_SECRET = os.environ.get("SHIPSTATION_API_SECRET", "")


# ─────────────────────────────────────────────────────────────────────────────
# ── Templates ─────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

LOGIN_TEMPLATE = r'''
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Login – H&amp;O Parcel Scans</title>
  <style>
    html, body {
      height: 100%;
      margin: 0;
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      background-color: #f5f6fa;
      color: #333;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .login-container {
      background: #fff;
      padding: 32px 24px;
      border-radius: 8px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.1);
      width: 320px;
      text-align: center;
    }
    .login-container h2 {
      margin-bottom: 24px;
      font-size: 1.5rem;
      color: #2c3e50;
    }
    .login-container input[type="password"] {
      display: block;
      width: 80%;
      margin: 0 auto 16px auto;
      padding: 10px 12px;
      font-size: 1rem;
      border: 1px solid #ccc;
      border-radius: 4px;
    }
    .login-container .btn {
      display: block;
      width: 80%;
      margin: 0 auto;
      padding: 10px 0;
      font-size: 1rem;
      background-color: #2d85f8;
      color: #fff;
      border: none;
      border-radius: 4px;
      cursor: pointer;
    }
    .login-container .btn:hover {
      opacity: 0.92;
    }
    .error {
      color: #e74c3c;
      margin-bottom: 16px;
      font-size: 0.95rem;
    }
  </style>
</head>
<body>
  <div class="login-container">
    <h2>Please Enter Password</h2>
    {% if error %}
      <div class="error">{{ error }}</div>
    {% endif %}
    <form action="{{ url_for('login') }}" method="post">
      <input type="password" name="password" placeholder="Password" required autofocus>
      <button type="submit" class="btn">Log In</button>
    </form>
  </div>
</body>
</html>
'''

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
    .sidebar .logout {
      margin-top: auto;
      color: #e74c3c;
      font-size: 0.95rem;
      cursor: pointer;
      text-decoration: none;
    }
    .sidebar .logout:hover {
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
      <a href="{{ url_for('logout') }}" class="logout">Log Out</a>
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
          <div class="batch-nav">
            <a href="#">Live Batch</a>
            <a href="#">Record Carrier Pick‐up</a>
            <a href="#">Open Batch</a>
          </div>
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
    .sidebar .logout {
      margin-top: auto;
      color: #e74c3c;
      font-size: 0.95rem;
      cursor: pointer;
      text-decoration: none;
    }
    .sidebar .logout:hover {
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
      <a href="{{ url_for('logout') }}" class="logout">Log Out</a>
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
