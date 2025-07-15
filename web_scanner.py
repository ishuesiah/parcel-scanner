# web_scanner.py

import os
import requests
import bcrypt
import time
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

# ── Secure session cookie settings ──
app.config.update(
    SESSION_COOKIE_SECURE=True,    # only send cookie over HTTPS
    SESSION_COOKIE_HTTPONLY=True,  # JS can’t read the cookie
    SESSION_COOKIE_SAMESITE='Lax'  # basic CSRF protection on cookies
)

# Read SECRET_KEY from the environment (and fail loudly if missing)
app.secret_key = os.environ["FLASK_SECRET_KEY"]

INACTIVITY_TIMEOUT = 60 * 60  # 30 minutes in seconds


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
PASSWORD_HASH = os.environ["APP_PASSWORD_HASH"].encode()

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
            <option value="Purolator">Purolator</option>
          </select>
          <br>
          <button type="submit" class="btn btn-new">Start Batch</button>
        </form>

      {% else %}
        <div class="batch-header">
          <h2>Batch #{{ current_batch.id }} (Carrier: {{ current_batch.carrier }})</h2>
          <p style="margin-top:4px; color:#666; font-size:0.9rem;">
            Scans count: {{ scans|length }}
          </p>
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
                    <input type="checkbox" name="delete_scan_ids" value="{{ row.id }}">
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
    /* Small delete button styling */
    .btn-delete-small {
      padding: 4px 8px;
      font-size: 0.8rem;
      background-color: #e74c3c;
      color: #fff;
      border: none;
      border-radius: 4px;
      cursor: pointer;
    }
    .btn-delete-small:hover {
      opacity: 0.92;
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
            <th>Actions</th>
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
              <td>
                <form action="{{ url_for('delete_batch') }}" method="post"
                      onsubmit="return confirm('Are you sure you want to delete batch #{{ b.id }}? This will remove all associated scans.');">
                  <input type="hidden" name="batch_id" value="{{ b.id }}">
                  <button type="submit" class="btn-delete-small">Delete</button>
                </form>
                <a href="{{ url_for('edit_batch', batch_id=b.id) }}" class="batch-link" style="margin-left:8px;">
                  Edit
                </a>
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
    /* Small delete button styling */
    .btn-delete-small {
      padding: 4px 8px;
      font-size: 0.8rem;
      background-color: #e74c3c;
      color: #fff;
      border: none;
      border-radius: 4px;
      cursor: pointer;
    }
    .btn-delete-small:hover {
      opacity: 0.92;
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
          <td>
            <form action="{{ url_for('delete_scan') }}" method="post"
                  onsubmit="return confirm('Are you sure you want to delete this scan?');">
              <input type="hidden" name="scan_id"  value="{{ s.id }}">
              <button type="submit" class="btn-delete-small">Delete</button>
            </form>
          </td>
        </tr>
      {% endfor %}
    </tbody>
  </table>

</div> <!-- .main-content -->

  </div> <!-- .container -->

<script>
  // define the handler so we can remove it later
  function requireLogoutPrompt(e) {
    e.preventDefault();
    // Chrome requires setting returnValue
    e.returnValue = "";
  }

  // register the prompt on page load
  window.addEventListener("beforeunload", requireLogoutPrompt);

  // disable the prompt when they click “Log Out”
  document.addEventListener("DOMContentLoaded", function() {
    const logoutLink = document.querySelector(".logout");
    if (!logoutLink) return;

    logoutLink.addEventListener("click", function() {
      window.removeEventListener("beforeunload", requireLogoutPrompt);
    });
  });
</script>


</body>
</html>
'''


# ─────────────────────────────────────────────────────────────────────────────
# ── BEFORE REQUEST: require login ──────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@app.before_request
def require_login():
    # always allow login & static assets
    if request.endpoint in ("login", "static", "favicon"):
        return

    last = session.get("last_active")
    now  = time.time()

    # if they’ve been idle too long, clear session & go to login
    if last and (now - last) > INACTIVITY_TIMEOUT:
        session.clear()
        flash(("error", "Logged out due to 30m inactivity."))
        return redirect(url_for("login"))

    # stamp this request’s activity
    session["last_active"] = now

    # then enforce that they must be authenticated
    if not session.get("authenticated"):
        return redirect(url_for("login"))



# ─────────────────────────────────────────────────────────────────────────────
# ── Routes ────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    error_msg = None
    if request.method == "POST":
        entered = request.form.get("password", "").encode()
        if bcrypt.checkpw(entered, PASSWORD_HASH):
            session.clear()
            session["authenticated"] = True
            session["last_active"]  = time.time()
            return redirect(url_for("index"))
        else:
            error_msg = "Invalid password. Please try again."
    return render_template_string(LOGIN_TEMPLATE, error=error_msg)




@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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

    # Fetch batch metadata
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

    # Fetch all scans in this batch (no 'id' column)
    cursor.execute("""
      SELECT
        id,
        tracking_number,
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
    if carrier not in ("UPS", "Canada Post", "DHL", "Purolator"):
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

@app.route("/edit_batch/<int:batch_id>", methods=["GET"])
def edit_batch(batch_id):
    # make sure the batch exists
    conn = get_mysql_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM batches WHERE id = %s", (batch_id,))
    if not cursor.fetchone():
        flash(("error", f"Batch #{batch_id} not found."))
        cursor.close()
        conn.close()
        return redirect(url_for("all_batches"))
    cursor.close()
    conn.close()

    # stash it back in session so index() shows the scan UI
    session["batch_id"] = batch_id
    flash(("success", f"Editing batch #{batch_id}."))
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

@app.route("/delete_batch", methods=["POST"])
def delete_batch():
    batch_id = request.form.get("batch_id")
    if not batch_id:
        flash(("error", "No batch specified for deletion."))
        return redirect(url_for("all_batches"))

    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()

        # First delete any scans associated with this batch:
        cursor.execute("DELETE FROM scans WHERE batch_id = %s", (batch_id,))
        # Then delete the batch itself:
        cursor.execute("DELETE FROM batches WHERE id = %s", (batch_id,))

        conn.commit()
        cursor.close()
        conn.close()

        flash(("success", f"Batch #{batch_id} and its scans have been deleted."))
    except mysql.connector.Error as e:
        flash(("error", f"MySQL Error: {e}"))

    return redirect(url_for("all_batches"))



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

    # Get the batch’s configured carrier
    conn = get_mysql_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT carrier FROM batches WHERE id = %s", (batch_id,))
    batch_carrier = cursor.fetchone()[0] or ""
    cursor.close() 

    # Normalize Canada Post codes
    if batch_carrier == "Canada Post":
        if code.startswith("2016"):
            code = code[7:-5]
        else:
            code = ""
            
    # Normalize Purolator codes
    if batch_carrier == "Purolator":
        if len(code) == 34:
            code = code[11:-11]
        else:
            code = ""
    # Defaults
    order_number  = "N/A"
    customer_name = "No ShipStation"
    order_id      = ""
    status        = "Original"
    now_str       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    scan_carrier  = ""

    # ── ShipStation lookup (including carrierCode) ──
       shipments = []
    # ── ShipStation lookup ──
    try:
        if not SHIPSTATION_API_KEY or not SHIPSTATION_API_SECRET:
            # credentials are truly missing—this is fatal
            raise RuntimeError("ShipStation credentials not configured")

        url = f"https://ssapi.shipstation.com/shipments?trackingNumber={code}"
        resp = requests.get(
            url,
            auth=(SHIPSTATION_API_KEY, SHIPSTATION_API_SECRET),
            headers={"Accept": "application/json"},
            timeout=5
        )
        resp.raise_for_status()
        data = resp.json()
        shipments = data.get("shipments", [])

        if shipments:
            first = shipments[0]
            order_number  = first.get("orderNumber", "N/A")
            customer_name = first.get("shipTo", {}).get("name", "No Name")
            carrier_code  = first.get("carrierCode", "").lower()

            carrier_map = {
                "ups":        "UPS",
                "canadapost": "Canada Post",
                "dhl":        "DHL",
                "purolator":  "Purolator",
            }
            scan_carrier = carrier_map.get(carrier_code, "")

    except RuntimeError as e:
        # truly fatal (no credentials)
        flash(("error", str(e)))
        conn.close()
        return redirect(url_for("index"))
    except requests.RequestException as e:
        # network/HTTP errors—warn but continue to fallback
        flash(("warning", f"ShipStation request failed: {e}"))
    except ValueError as e:
        # JSON decode errors, etc.—warn but continue
        flash(("warning", f"ShipStation returned bad data: {e}"))
    # no generic "except Exception": let truly unexpected errors bubble up

    # ── Fallback: if no shipments from ShipStation, query Shopify ──
    if not shipments:
        try:
            shopify_api = ShopifyAPI()
            info = shopify_api.get_order_by_tracking(code)
            order_number  = info.get("order_number", "N/A")
            customer_name = info.get("customer_name", "Unknown")
            order_id      = info.get("order_id", "")
        except Exception as e:
            flash(("error", f"Shopify lookup error: {e}"))



    # ── Fallback: detect DHL by 10-char code, then UPS/Canada Post ──
    if not scan_carrier:
        if len(code) == 12:
            scan_carrier = "Purolator"
        elif len(code) == 10:
            scan_carrier = "DHL"
        elif code.startswith("1ZAC"):
            scan_carrier = "UPS"
        elif code.startswith("2016"):
            scan_carrier = "Canada Post"
        elif code.startswith("LA") or len(code) == 30:
            scan_carrier = "USPS"


    # ── Duplicate check ──
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM scans WHERE tracking_number = %s AND batch_id = %s",
        (code, batch_id)
    )
    if cursor.fetchone()[0] > 0:
        status = "Duplicate"
    cursor.close()

    # ── Shopify lookup for order_id ──
    try:
        shopify_api = ShopifyAPI()
        info = shopify_api.get_order_by_tracking(code)
        order_id = info.get("order_id", "")
    except Exception as e:
        flash(("error", f"Shopify lookup error: {e}"))

    # ── Insert the scan record ──
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO scans
          (tracking_number, carrier, order_number, customer_name,
           scan_date, status, order_id, batch_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (code, scan_carrier, order_number, customer_name,
         now_str, status, order_id, batch_id)
    )
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

    scan_ids = request.form.getlist("delete_scan_ids")
    if not scan_ids:
        flash(("error", "No scans selected for deletion."))
        return redirect(url_for("index"))

    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()

        # Build placeholders: (%s,%s,...)
        placeholders = ",".join(["%s"] * len(scan_ids))
        sql = f"DELETE FROM scans WHERE id IN ({placeholders}) AND batch_id = %s"
        params = scan_ids + [batch_id]

        cursor.execute(sql, params)
        conn.commit()
        cursor.close()
        conn.close()

        flash(("success", f"Deleted {len(scan_ids)} scan(s)."))
    except mysql.connector.Error as e:
        flash(("error", f"MySQL Error: {e}"))

    return redirect(url_for("index"))



@app.route("/delete_scan", methods=["POST"])
def delete_scan():
    scan_id = request.form.get("scan_id")
    if not scan_id:
        flash(("error", "No scan specified for deletion."))
        return redirect(url_for("all_scans"))

    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scans WHERE id = %s", (scan_id,))
        conn.commit()
        cursor.close()
        conn.close()
        flash(("success", f"Deleted scan #{scan_id}."))
    except mysql.connector.Error as e:
        flash(("error", f"MySQL Error: {e}"))

    return redirect(url_for("all_scans"))


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
          SELECT
            tracking_number,
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
          SELECT
            tracking_number,
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
