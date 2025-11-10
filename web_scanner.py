# web_scanner.py

"""
Hemlock & Oak Parcel Scanner
Version: 1.2.1
Description: Track and manage parcel shipments with carrier integrations
"""

__version__ = "1.2.1"

import os
import requests
import bcrypt
import time

# Load environment variables from .env file if it exists (for local development)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed (production environment)

from flask import (
    Flask,
    request,
    redirect,
    url_for,
    render_template_string,
    flash,
    session,
    jsonify
)
import mysql.connector
from mysql.connector import pooling
from datetime import datetime

from shopify_api import ShopifyAPI  # Assumes shopify_api.py is alongside this file

app = Flask(__name__)

# ── Secure session cookie settings ──
app.config.update(
    SESSION_COOKIE_SECURE=True,    # only send cookie over HTTPS
    SESSION_COOKIE_HTTPONLY=True,  # JS can't read the cookie
    SESSION_COOKIE_SAMESITE='Lax'  # basic CSRF protection on cookies
)

# Read SECRET_KEY from the environment (and fail loudly if missing)
app.secret_key = os.environ["FLASK_SECRET_KEY"]

# 30 minutes in seconds
INACTIVITY_TIMEOUT = 30 * 60


# ── MySQL connection pool ──
db_pool = mysql.connector.pooling.MySQLConnectionPool(
    pool_name="flask_pool",
    pool_size=15,  # Increased from 5 to 15 to handle concurrent scans + background threads
    pool_reset_session=True,
    host=os.environ["MYSQL_HOST"],
    port=int(os.environ.get("MYSQL_PORT", 30603)),
    user=os.environ["MYSQL_USER"],
    password=os.environ["MYSQL_PASSWORD"],
    database=os.environ["MYSQL_DATABASE"],
    connection_timeout=10,  # 10 second timeout for getting a connection from pool
    autocommit=False,  # Explicit transaction control
)

def get_mysql_connection():
    """
    Get a connection from the pool with retry logic for pool exhaustion.
    """
    max_retries = 3
    for retry in range(max_retries):
        try:
            return db_pool.get_connection()
        except mysql.connector.errors.PoolError as e:
            if retry < max_retries - 1:
                wait = 0.5 * (retry + 1)  # 0.5s, 1s, 1.5s
                print(f"Connection pool exhausted, retry {retry + 1}/{max_retries} after {wait}s: {e}")
                time.sleep(wait)
            else:
                print(f"Failed to get database connection after {max_retries} retries")
                raise
        except Exception as e:
            print(f"Database connection error: {e}")
            raise

# Read shop URL for building admin links
SHOP_URL = os.environ.get("SHOP_URL", "").rstrip("/")

# Read application password from environment (e.g. set APP_PASSWORD in Kinsta)
PASSWORD_HASH = os.environ["APP_PASSWORD_HASH"].encode()

# Read ShipStation credentials from environment
SHIPSTATION_API_KEY = os.environ.get("SHIPSTATION_API_KEY", "")
SHIPSTATION_API_SECRET = os.environ.get("SHIPSTATION_API_SECRET", "")

# ── Shopify singleton ──
_shopify_api = None
def get_shopify_api():
    global _shopify_api
    if _shopify_api is None:
        _shopify_api = ShopifyAPI()
    return _shopify_api


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
      @import url("https://d3a1s2k5oq9b60.cloudfront.net/WF-062340-d8eba8d3266ba707a7e48a89247d3873.css?fsf=22601");
  @font-face {
  font-family: "ABC Arizona Flare Regular";
  src: url("Webfont-062340-003957-022601-e4892a18d562a49278782e582c6385b87590aea0.woff2") format("woff2"), url("Webfont-062340-003957-022601-6707a17205951254095bffe39e6cf21dc9435ddd.woff") format("woff");
  }

      @font-face {
        font-family: 'Arizona Regular';
        src: url("https://cdn.shopify.com/s/files/1/0280/1175/7703/files/Arizona_Flare_Light.woff2?v=1745606070") format("woff2");
        font-weight: normal;
        font-display: swap;
      }

      @font-face {
        font-family: 'Arizona Italic';
        src: url("https://cdn.shopify.com/s/files/1/0280/1175/7703/files/Webfont-062340-003957-022602-3d874fa6cd082c5453f60ea524707bf1a00ad7d7.woff2?v=1745605778") format("woff2");
        font-weight: normal;
        font-style: italic;
        font-display: swap;
      }
          @font-face {
          font-family: 'SprigSansRegular';
          src: url('https://cdn.shopify.com/s/files/1/0280/1175/7703/files/FAIRE-SprigSans-Regular.woff2?v=1724296405') format('woff2');
          font-weight: 300;
          font-style: normal;
          font-display: swap;
        }

        @font-face {
          font-family: 'SprigSansRegularItalic';
          src: url('https://cdn.shopify.com/s/files/1/0280/1175/7703/files/FAIRE-SprigSans-RegularItalic.woff2?v=1724296404') format('woff2');
          font-weight: 400;
          font-style: italic;
          font-display: swap;
        }

        @font-face {
          font-family: 'SprigSansMedium';
          src: url('https://cdn.shopify.com/s/files/1/0280/1175/7703/files/FAIRE-SprigSans-Bold.woff2?v=1724296404') format('woff2');
          font-weight: 500;
          font-style: normal;
          font-display: swap;
        }

        @font-face {
          font-family: 'SprigSansMediumItalic';
          src: url('https://cdn.shopify.com/s/files/1/0280/1175/7703/files/FAIRE-Sprig-MediumItalic.woff2?v=1724305674') format('woff2');
          font-weight: 500;
          font-style: italic;
          font-display: swap;
        }
    html, body {
      height: 100%;
      margin: 0;
      font-family: "SprigSansRegular", Tahoma, Geneva, Verdana, sans-serif;
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
    .flash {
      padding: 10px 14px;
      margin-bottom: 16px;
      border-radius: 4px;
      background-color: #fdecea;
      color: #a33a2f;
      font-size: 0.95rem;
      border: 1px solid #f5c6cb;
    }
  </style>
</head>
<body>
  <div class="login-container">
    <h2>Please Enter Password</h2>

    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for category, msg in messages %}
        <div class="flash">{{ msg }}</div>
      {% endfor %}
    {% endwith %}

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
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100%;
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      background-color: #f5f6fa;
      color: #333;
    }

    /* Layout */
    .container { display: flex; height: 100vh; }

    /* ── SIDEBAR ── */
    .sidebar {
      width: 240px; background-color: #ffffff; border-right: 1px solid #e0e0e0;
      display: flex; flex-direction: column; padding: 24px 16px;
    }
    .sidebar h1 { font-size: 1.25rem; font-weight: bold; margin-bottom: 16px; color: #2c3e50; }
    .sidebar ul { list-style: none; margin-top: 8px; }
    .sidebar li { margin-bottom: 16px; }
    .sidebar a { text-decoration: none; color: #2d85f8; font-size: 1rem; font-weight: 500; }
    .sidebar a:hover { text-decoration: underline; }
    .sidebar .logout { margin-top: auto; color: #e74c3c; font-size: 0.95rem; text-decoration: none; }
    .sidebar .logout:hover { text-decoration: underline; }

    /* ── MAIN CONTENT ── */
    .main-content { flex: 1; overflow-y: auto; padding: 24px; }
    .flash {
      padding: 10px 14px; margin-bottom: 16px; border-radius: 4px; font-weight: 500; border: 1px solid;
      animation: slideIn 0.3s ease-out;
    }
    .flash.success { background-color: #e0f7e9; color: #2f7a45; border-color: #b2e6c2; }
    .flash.error   { background-color: #fdecea; color: #a33a2f; border-color: #f5c6cb; }
    .flash.warning { background-color: #fff4e5; color: #8a6100; border-color: #ffe0b2; }

    @keyframes slideIn {
      from { opacity: 0; transform: translateY(-10px); }
      to { opacity: 1; transform: translateY(0); }
    }

    h2 { font-size: 1.5rem; color: #2c3e50; margin-bottom: 16px; }
    form label { font-weight: 600; color: #333; }
    form input[type="text"], form select {
      width: 300px; padding: 8px; border: 1px solid #ccc; border-radius: 4px;
      margin-top: 4px; margin-bottom: 12px; font-size: 0.95rem;
    }
    .btn { padding: 8px 12px; font-size: 0.9rem; border: none; border-radius: 4px; cursor: pointer; transition: all 0.2s; }
    .btn-new { background-color: #2d85f8; color: white; }
    .btn-delete { background-color: #e74c3c; color: white; }
    .btn-batch { background-color: #27ae60; color: white; }
    .btn:hover { opacity: 0.92; transform: translateY(-1px); }
    .btn:active { transform: translateY(0); }
    .btn:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }

    /* Scan form improvements */
    .scan-section { 
      background: white; 
      padding: 20px; 
      border-radius: 8px; 
      box-shadow: 0 1px 3px rgba(0,0,0,0.1); 
      margin-bottom: 20px; 
    }
    .scan-form { display: flex; align-items: flex-end; gap: 12px; }
    .scan-form .form-group { flex: 1; max-width: 400px; }
    .scan-form input[type="text"] { width: 100%; }
    .scan-status { 
      margin-top: 12px; 
      padding: 8px 12px; 
      border-radius: 4px; 
      font-size: 0.9rem; 
      display: none;
    }
    .scan-status.show { display: block; }
    .scan-status.processing { background-color: #fff4e5; color: #8a6100; border: 1px solid #ffe0b2; }
    .scan-status.success { background-color: #e0f7e9; color: #2f7a45; border: 1px solid #b2e6c2; }
    .scan-status.error { background-color: #fdecea; color: #a33a2f; border: 1px solid #f5c6cb; }

    table { width: 100%; border-collapse: collapse; margin-top: 12px; background: white; }
    th, td { border: 1px solid #ddd; padding: 10px 8px; font-size: 0.93rem; color: #34495e; }
    th { background-color: #f2f2f2; text-align: left; font-weight: 600; }
    tr:nth-child(even) { background-color: #fafafa; }
    tr:hover { background-color: #f1f1f1; }
    .duplicate-row { background-color: #fdecea !important; }
    .duplicate-row:hover { background-color: #fbd5d0 !important; }
    td a { color: #2d85f8; text-decoration: none; font-weight: 500; }
    td a:hover { text-decoration: underline; }
    td input[type="checkbox"] { width: 16px; height: 16px; cursor: pointer; }
    
    .batch-header { 
      display: flex; 
      align-items: center; 
      justify-content: space-between; 
      flex-wrap: wrap; 
      margin-bottom: 16px; 
      background: white;
      padding: 16px 20px;
      border-radius: 8px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    }
    .batch-info h2 { font-size: 1.5rem; color: #2c3e50; margin-bottom: 4px; }
    .batch-info p { color: #666; font-size: 0.9rem; margin: 2px 0; }
    .batch-actions { display: flex; gap: 12px; align-items: center; }
    .batch-actions a { color: #e74c3c; text-decoration: none; font-size: 0.9rem; font-weight: 500; }
    .batch-actions a:hover { text-decoration: underline; }

    /* Actions bar for delete */
    .actions-bar {
      background: white;
      padding: 16px 20px;
      border-radius: 8px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.1);
      margin-bottom: 16px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .actions-bar h3 { font-size: 1.1rem; color: #2c3e50; }

    /* Loading spinner */
    .spinner {
      display: inline-block;
      width: 14px;
      height: 14px;
      border: 2px solid #f3f3f3;
      border-top: 2px solid #2d85f8;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      margin-left: 8px;
    }
    @keyframes spin {
      0% { transform: rotate(0deg); }
      100% { transform: rotate(360deg); }
    }
  </style>
</head>
<body>

  <div class="container">

    <!-- ── SIDEBAR ── -->
    <div class="sidebar">
      <h1><img src="{{ url_for('static', filename='parcel-scan.jpg') }}" width="200"></h1>
      <ul>
        <li><a href="{{ url_for('index') }}">New Batch</a></li>
        <li><a href="{{ url_for('all_batches') }}">Recorded Pick‐ups</a></li>
        <li><a href="{{ url_for('all_scans') }}">All Scans</a></li>
      </ul>
      <a href="{{ url_for('logout') }}" class="logout">Log Out</a>
      <div style="margin-top: 16px; padding-top: 16px; border-top: 1px solid #e0e0e0; font-size: 0.75rem; color: #999; text-align: center;">
        v{{ version }}
      </div>
    </div>
    <!-- ── END SIDEBAR ── -->


    <!-- ── MAIN CONTENT ── -->
    <div class="main-content">

      <div id="flash-container">
        {% with messages = get_flashed_messages(with_categories=true) %}
          {% for category, msg in messages %}
            <div class="flash {{ category }}">{{ msg }}</div>
          {% endfor %}
        {% endwith %}
      </div>

      {% if not current_batch %}
        <h2>Create New Batch</h2>
        <div class="scan-section">
          <form action="{{ url_for('new_batch') }}" method="post">
            <label for="carrier"><strong>Carrier:</strong></label><br>
            <select name="carrier" id="carrier" required>
              <option value="">-- Select Carrier --</option>
              <option value="UPS">UPS</option>
              <option value="Canada Post">Canada Post</option>
              <option value="DHL">DHL</option>
              <option value="Purolator">Purolator</option>
            </select>
            <br><br>
            <button type="submit" class="btn btn-new">Start Batch</button>
          </form>
        </div>

      {% else %}
        <div class="batch-header">
          <div class="batch-info">
            <h2>Batch #{{ current_batch.id }} ({{ current_batch.carrier }})</h2>
            <p><em>Created: {{ current_batch.created_at }}</em></p>
            <p>Scans in batch: <strong id="scan-count">{{ scans|length }}</strong></p>
            <p style="font-size: 0.85rem; color: #666; margin-top: 4px;">
              💡 Tip: Order details load in background. Refresh page to see updated info.
            </p>
          </div>
          <div class="batch-actions">
            <a href="#" onclick="return confirmCancelBatch();">Cancel This Batch</a>
          </div>
        </div>

        <!-- Scan form with async capability -->
        <div class="scan-section">
          <form id="scan-form" class="scan-form" autocomplete="off">
            <div class="form-group">
              <label for="code"><strong>Scan Tracking Number:</strong></label><br>
              <input type="text" name="code" id="code" autofocus required>
            </div>
            <button type="submit" class="btn" id="scan-btn">
              Submit<span id="scan-spinner" class="spinner" style="display:none;"></span>
            </button>
          </form>
          <div id="scan-status" class="scan-status"></div>
        </div>

        <!-- Actions bar at top -->
        <div class="actions-bar">
          <h3>Scans in This Batch</h3>
          <div style="display: flex; gap: 12px;">
            <form action="{{ url_for('delete_scans') }}" method="post" id="delete-form" style="margin: 0;">
              <button type="submit" class="btn btn-delete" id="delete-btn">Delete Selected</button>
            </form>
            <button type="button" class="btn btn-new" onclick="window.location.reload()">Save</button>
            <form action="{{ url_for('record_batch') }}" method="post" style="margin: 0;">
              <button type="submit" class="btn btn-batch">Record Carrier Pick-up</button>
            </form>
          </div>
        </div>

        <!-- Scans table -->
        <form id="scans-table-form">
          <table>
            <thead>
              <tr>
                <th style="width: 40px;"><input type="checkbox" id="select-all"></th>
                <th>Tracking</th>
                <th>Carrier</th>
                <th>Order #</th>
                <th>Customer</th>
                <th>Scan Time</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody id="scans-tbody">
              {% for row in scans %}
                <tr class="{{ 'duplicate-row' if row.status.startswith('Duplicate') else '' }}" data-scan-id="{{ row.id }}">
                  <td>
                    <input type="checkbox" class="scan-checkbox" name="delete_scan_ids" value="{{ row.id }}">
                  </td>
                  <td style="font-weight: 500;">{{ row.tracking_number }}</td>
                  <td>{{ row.carrier }}</td>
                  <td>
                    {% if row.order_id %}
                      <a href="https://{{ shop_url }}/admin/orders/{{ row.order_id }}" target="_blank">
                        {{ row.order_number }}
                      </a>
                    {% else %}
                      {{ row.order_number }}
                    {% endif %}
                  </td>
                  <td>
                    {% if row.order_id %}
                      <a href="https://{{ shop_url }}/admin/orders/{{ row.order_id }}" target="_blank">
                        {{ row.customer_name }}
                      </a>
                    {% else %}
                      {{ row.customer_name }}
                    {% endif %}
                  </td>
                  <td>{{ row.scan_date }}</td>
                  <td>
                    {% if row.status.startswith('Duplicate (Batch #') %}
                      {% set batch_num = row.status.split('#')[1].rstrip(')') %}
                      {% if batch_num and batch_num.isdigit() %}
                        Duplicate (<a href="{{ url_for('view_batch', batch_id=batch_num|int) }}" style="color: #2d85f8; text-decoration: none; font-weight: 500;">Batch #{{ batch_num }}</a>)
                      {% else %}
                        {{ row.status }}
                      {% endif %}
                    {% else %}
                      {{ row.status }}
                    {% endif %}
                  </td>
                </tr>
              {% endfor %}
            </tbody>
          </table>
        </form>

      {% endif %}

    </div> <!-- .main-content -->

  </div> <!-- .container -->

  <script>
    // ── Auto-refresh order details every 5 seconds ──
    let autoRefreshInterval;
    
    function startAutoRefresh() {
      {% if current_batch %}
      // Poll every 5 seconds for updated order details
      autoRefreshInterval = setInterval(async function() {
        try {
          const response = await fetch('{{ url_for("get_batch_updates", batch_id=current_batch.id) }}');

          // Check if response is OK and is JSON
          if (!response.ok) {
            console.error('Auto-refresh HTTP error:', response.status);
            return;
          }

          const contentType = response.headers.get('content-type');
          if (!contentType || !contentType.includes('application/json')) {
            console.error('Auto-refresh returned non-JSON response:', contentType);
            return;
          }

          const data = await response.json();

          if (data.success && data.scans) {
            // Update each row with new data
            data.scans.forEach(scan => {
              updateScanRow(scan);
            });
          }
        } catch (error) {
          console.error('Auto-refresh error:', error);
        }
      }, 5000); // Every 5 seconds
      {% endif %}
    }
    
    function stopAutoRefresh() {
      if (autoRefreshInterval) {
        clearInterval(autoRefreshInterval);
      }
    }
    
    function updateScanRow(scan) {
      // Find the row for this scan
      const row = document.querySelector(`tr[data-scan-id="${scan.id}"]`);
      if (!row) return;
      
      // Only update if the data has actually changed (not still "Processing...")
      if (scan.order_number === 'Processing...' || scan.customer_name === 'Looking up...') {
        return; // Still processing, skip
      }
      
      // Update the cells
      const cells = row.querySelectorAll('td');
      
      // Update carrier (cell 2)
      if (cells[2]) cells[2].textContent = scan.carrier;
      
      // Update order number (cell 3)
      if (cells[3]) {
        if (scan.order_id) {
          cells[3].innerHTML = `<a href="https://${shopUrl}/admin/orders/${scan.order_id}" target="_blank">${scan.order_number}</a>`;
        } else {
          cells[3].textContent = scan.order_number;
        }
      }
      
      // Update customer name (cell 4)
      if (cells[4]) {
        if (scan.order_id) {
          cells[4].innerHTML = `<a href="https://${shopUrl}/admin/orders/${scan.order_id}" target="_blank">${scan.customer_name}</a>`;
        } else {
          cells[4].textContent = scan.customer_name;
        }
      }
      
      // Update status (cell 6) - change from "Processing" to "Complete"
      if (cells[6] && scan.status === 'Complete') {
        cells[6].textContent = scan.status;
      }
    }
    
    // Start auto-refresh when page loads (only if there's an active batch)
    {% if current_batch %}
    startAutoRefresh();
    {% endif %}
    
    // Stop auto-refresh when page is hidden (save bandwidth)
    document.addEventListener('visibilitychange', function() {
      if (document.hidden) {
        stopAutoRefresh();
      } else {
        {% if current_batch %}
        startAutoRefresh();
        {% endif %}
      }
    });

    // ── Periodic focus restoration ──
    {% if current_batch %}
    // Ensure focus is set on page load (with small delay to ensure DOM is ready)
    setTimeout(function() {
      if (codeInput) codeInput.focus();
    }, 100);

    // Restore focus to tracking input every 3 seconds if user hasn't focused elsewhere
    setInterval(function() {
      if (!codeInput || document.hidden) return;

      const activeElement = document.activeElement;

      // Only restore focus if active element is body or non-interactive element
      // This allows users to interact with buttons, links, checkboxes, etc.
      const isInteractiveElement = activeElement && (
        activeElement.tagName === 'INPUT' ||
        activeElement.tagName === 'TEXTAREA' ||
        activeElement.tagName === 'SELECT' ||
        activeElement.tagName === 'BUTTON' ||
        activeElement.tagName === 'A' ||
        activeElement.isContentEditable
      );

      // Restore focus only if not interacting with anything else
      if (!isInteractiveElement || activeElement === document.body) {
        codeInput.focus();
      }
    }, 3000); // Every 3 seconds
    {% endif %}

    // ── Async scanning functionality ──
    {% if current_batch %}
    const scanForm = document.getElementById('scan-form');
    const codeInput = document.getElementById('code');
    const scanBtn = document.getElementById('scan-btn');
    const scanSpinner = document.getElementById('scan-spinner');
    const scanStatus = document.getElementById('scan-status');
    const scansTable = document.getElementById('scans-tbody');
    const scanCount = document.getElementById('scan-count');
    const shopUrl = '{{ shop_url }}';

    scanForm.addEventListener('submit', async function(e) {
      e.preventDefault();
      
      const code = codeInput.value.trim();
      if (!code) return;

      // Show processing but DON'T disable button
      scanSpinner.style.display = 'inline-block';
      
      // Show immediate feedback
      scanStatus.textContent = `Scanning: ${code}...`;
      scanStatus.className = 'scan-status processing show';

      try {
        const formData = new FormData();
        formData.append('code', code);

        const response = await fetch('{{ url_for("scan") }}', {
          method: 'POST',
          headers: {
            'X-Requested-With': 'XMLHttpRequest'
          },
          body: formData
        });

        // Check if response is JSON before parsing
        const contentType = response.headers.get('content-type');
        if (!contentType || !contentType.includes('application/json')) {
          scanStatus.textContent = 'Server error - received non-JSON response';
          scanStatus.className = 'scan-status error show';
          console.error('Scan returned non-JSON response:', contentType);
          return;
        }

        const data = await response.json();

        if (data.success) {
          // Show success message
          scanStatus.textContent = data.message + ' (Details loading in background...)';
          scanStatus.className = 'scan-status success show';

          // Add new row to table
          addScanToTable(data.scan);

          // Update scan count
          const currentCount = parseInt(scanCount.textContent);
          scanCount.textContent = currentCount + 1;

          // Clear input IMMEDIATELY
          codeInput.value = '';

          // Hide status after 1.5 seconds
          setTimeout(() => {
            scanStatus.classList.remove('show');
          }, 1500);
        } else {
          scanStatus.textContent = 'Error: ' + data.error;
          scanStatus.className = 'scan-status error show';
        }
      } catch (error) {
        let errorMsg = error.message;
        if (error instanceof SyntaxError) {
          // JSON parse error
          errorMsg = 'Server returned invalid response (not JSON)';
        }
        scanStatus.textContent = 'Error: ' + errorMsg;
        scanStatus.className = 'scan-status error show';
        console.error('Scan error:', error);
      } finally {
        // Hide spinner and keep button enabled
        scanSpinner.style.display = 'none';
        codeInput.focus();
      }
    });

    function addScanToTable(scan) {
      const row = document.createElement('tr');
      row.className = scan.status.startsWith('Duplicate') ? 'duplicate-row' : '';
      row.dataset.scanId = scan.id;

      // Note: order_number and customer_name will be "Processing..." and "Looking up..."
      // They'll update in the database in background, refresh page to see updates
      const orderLink = scan.order_id 
        ? `<a href="https://${shopUrl}/admin/orders/${scan.order_id}" target="_blank">${scan.order_number}</a>`
        : scan.order_number;

      const customerLink = scan.order_id
        ? `<a href="https://${shopUrl}/admin/orders/${scan.order_id}" target="_blank">${scan.customer_name}</a>`
        : scan.customer_name;

      // Format status with batch link if it's a duplicate
      let statusDisplay = scan.status;
      if (scan.status.startsWith('Duplicate (Batch #')) {
        const batchMatch = scan.status.match(/Batch #(\d+)/);
        if (batchMatch) {
          const batchNum = batchMatch[1];
          statusDisplay = `Duplicate (<a href="/view_batch/${batchNum}" style="color: #2d85f8; text-decoration: none; font-weight: 500;">Batch #${batchNum}</a>)`;
        }
      }

      row.innerHTML = `
        <td><input type="checkbox" class="scan-checkbox" name="delete_scan_ids" value="${scan.id}"></td>
        <td style="font-weight: 500;">${scan.tracking_number}</td>
        <td>${scan.carrier}</td>
        <td>${orderLink}</td>
        <td>${customerLink}</td>
        <td>${scan.scan_date}</td>
        <td>${statusDisplay}</td>
      `;

      // Insert at the top of the table
      scansTable.insertBefore(row, scansTable.firstChild);
    }

    // ── Select all checkboxes functionality ──
    const selectAllCheckbox = document.getElementById('select-all');
    selectAllCheckbox.addEventListener('change', function() {
      const checkboxes = document.querySelectorAll('.scan-checkbox');
      checkboxes.forEach(cb => cb.checked = this.checked);
    });

    // ── Delete form handling ──
    const deleteForm = document.getElementById('delete-form');
    deleteForm.addEventListener('submit', function(e) {
      const checkboxes = document.querySelectorAll('.scan-checkbox:checked');
      
      if (checkboxes.length === 0) {
        e.preventDefault();
        alert('Please select at least one scan to delete.');
        return false;
      }

      // Add the selected IDs to the delete form
      checkboxes.forEach(cb => {
        const input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'delete_scan_ids';
        input.value = cb.value;
        deleteForm.appendChild(input);
      });
    });
    {% endif %}

    // ── Cancel batch confirmation ──
    function confirmCancelBatch() {
      if (confirm('Are you sure you want to cancel this batch? This will delete all scans in the batch.')) {
        window.location.href = '{{ url_for("cancel_batch") }}';
      }
      return false;
    }

    // ── Auto-dismiss flash messages ──
    setTimeout(function() {
      const flashes = document.querySelectorAll('.flash');
      flashes.forEach(flash => {
        flash.style.transition = 'opacity 0.5s';
        flash.style.opacity = '0';
        setTimeout(() => flash.remove(), 500);
      });
    }, 5000);
  </script>

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
      @import url("https://d3a1s2k5oq9b60.cloudfront.net/WF-062340-d8eba8d3266ba707a7e48a89247d3873.css?fsf=22601");
  @font-face {
  font-family: "ABC Arizona Flare Regular";
  src: url("Webfont-062340-003957-022601-e4892a18d562a49278782e582c6385b87590aea0.woff2") format("woff2"), url("Webfont-062340-003957-022601-6707a17205951254095bffe39e6cf21dc9435ddd.woff") format("woff");
  }

      @font-face {
        font-family: 'Arizona Regular';
        src: url("https://cdn.shopify.com/s/files/1/0280/1175/7703/files/Arizona_Flare_Light.woff2?v=1745606070") format("woff2");
        font-weight: normal;
        font-display: swap;
      }

      @font-face {
        font-family: 'Arizona Italic';
        src: url("https://cdn.shopify.com/s/files/1/0280/1175/7703/files/Webfont-062340-003957-022602-3d874fa6cd082c5453f60ea524707bf1a00ad7d7.woff2?v=1745605778") format("woff2");
        font-weight: normal;
        font-style: italic;
        font-display: swap;
      }
          @font-face {
          font-family: 'SprigSansRegular';
          src: url('https://cdn.shopify.com/s/files/1/0280/1175/7703/files/FAIRE-SprigSans-Regular.woff2?v=1724296405') format('woff2');
          font-weight: 300;
          font-style: normal;
          font-display: swap;
        }

        @font-face {
          font-family: 'SprigSansRegularItalic';
          src: url('https://cdn.shopify.com/s/files/1/0280/1175/7703/files/FAIRE-SprigSans-RegularItalic.woff2?v=1724296404') format('woff2');
          font-weight: 400;
          font-style: italic;
          font-display: swap;
        }

        @font-face {
          font-family: 'SprigSansMedium';
          src: url('https://cdn.shopify.com/s/files/1/0280/1175/7703/files/FAIRE-SprigSans-Bold.woff2?v=1724296404') format('woff2');
          font-weight: 500;
          font-style: normal;
          font-display: swap;
        }

        @font-face {
          font-family: 'SprigSansMediumItalic';
          src: url('https://cdn.shopify.com/s/files/1/0280/1175/7703/files/FAIRE-Sprig-MediumItalic.woff2?v=1724305674') format('woff2');
          font-weight: 500;
          font-style: italic;
          font-display: swap;
        }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100%;
      font-family: "SprigSansRegular", Tahoma, Geneva, Verdana, sans-serif;
      background-color: #f5f6fa; color: #333;
    }
    .container { display: flex; height: 100vh; }
    .sidebar {
      width: 240px; background: #fff; border-right: 1px solid #e0e0e0;
      display: flex; flex-direction: column; padding: 24px 16px;
    }
    .sidebar h1 { font-size: 1.25rem; font-weight: bold; margin-bottom: 16px; color: #2c3e50; }
    .sidebar ul { list-style: none; margin-top: 8px; }
    .sidebar li { margin-bottom: 16px; }
    .sidebar a { text-decoration: none; color: #2d85f8; font-size: 1rem; font-weight: 500; }
    .sidebar a:hover { text-decoration: underline; }
    .sidebar .logout { margin-top: auto; color: #e74c3c; font-size: 0.95rem; text-decoration: none; }
    .sidebar .logout:hover { text-decoration: underline; }
    .main-content { flex: 1; overflow-y: auto; padding: 24px; }
    .flash {
      padding: 10px 14px; margin-bottom: 16px; border-radius: 4px; font-weight: 500; border: 1px solid;
    }
    .flash.success { background-color: #e0f7e9; color: #2f7a45; border-color: #b2e6c2; }
    .flash.error   { background-color: #fdecea; color: #a33a2f; border-color: #f5c6cb; }
    .flash.warning { background-color: #fff4e5; color: #8a6100; border-color: #ffe0b2; }
    h2 { font-size: 1.5rem; color: #2c3e50; margin-bottom: 16px; }
    table { width: 100%; border-collapse: collapse; margin-top: 12px; background: white; }
    th, td { border: 1px solid #ddd; padding: 10px 8px; font-size: 0.93rem; color: #34495e; }
    th { background-color: #f2f2f2; text-align: left; font-weight: 600; }
    tr:nth-child(even) { background-color: #fafafa; }
    tr:hover { background-color: #f1f1f1; }
    .batch-link { color: #2d85f8; text-decoration: none; font-weight: 500; }
    .batch-link:hover { text-decoration: underline; }
    .btn-delete-small {
      padding: 4px 8px; font-size: 0.8rem; background-color: #e74c3c; color: #fff;
      border: none; border-radius: 4px; cursor: pointer;
    }
    .btn-delete-small:hover { opacity: 0.92; }
  </style>
</head>
<body>

  <div class="container">

    <div class="sidebar">
      <h1><img src="{{ url_for('static', filename='parcel-scan.jpg') }}" width="200"></img></h1>
      <ul>
        <li><a href="{{ url_for('index') }}">New Batch</a></li>
        <li><a href="{{ url_for('all_batches') }}">Recorded Pick‐ups</a></li>
        <li><a href="{{ url_for('all_scans') }}">All Scans</a></li>
      </ul>
      <a href="{{ url_for('logout') }}" class="logout">Log Out</a>
      <div style="margin-top: 16px; padding-top: 16px; border-top: 1px solid #e0e0e0; font-size: 0.75rem; color: #999; text-align: center;">
        v{{ version }}
      </div>
    </div>

    <div class="main-content">

      {% with messages = get_flashed_messages(with_categories=true) %}
        {% for category, msg in messages %}
          <div class="flash {{ category }}">{{ msg }}</div>
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
                <form action="{{ url_for('delete_batch') }}" method="post" style="display: inline;"
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

    </div>

  </div>

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
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100%;
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      background-color: #f5f6fa; color: #333;
    }
    .container { display: flex; height: 100vh; }
    .sidebar {
      width: 240px; background: #fff; border-right: 1px solid #e0e0e0;
      display: flex; flex-direction: column; padding: 24px 16px;
    }
    .sidebar h1 { font-size: 1.25rem; font-weight: bold; margin-bottom: 16px; color: #2c3e50; }
    .sidebar ul { list-style: none; margin-top: 8px; }
    .sidebar li { margin-bottom: 16px; }
    .sidebar a { text-decoration: none; color: #2d85f8; font-size: 1rem; font-weight: 500; }
    .sidebar a:hover { text-decoration: underline; }
    .sidebar .logout { margin-top: auto; color: #e74c3c; font-size: 0.95rem; text-decoration: none; }
    .sidebar .logout:hover { text-decoration: underline; }
    .main-content { flex: 1; overflow-y: auto; padding: 24px; }
    .flash {
      padding: 10px 14px; margin-bottom: 16px; border-radius: 4px; font-weight: 500; border: 1px solid;
    }
    .flash.success { background-color: #e0f7e9; color: #2f7a45; border-color: #b2e6c2; }
    .flash.error   { background-color: #fdecea; color: #a33a2f; border-color: #f5c6cb; }
    .flash.warning { background-color: #fff4e5; color: #8a6100; border-color: #ffe0b2; }
    .batch-header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; margin-bottom: 16px; }
    .batch-header h2 { font-size: 1.5rem; color: #2c3e50; }
    .batch-header .back-link { color: #2d85f8; text-decoration: none; font-size: 0.95rem; font-weight: 500; }
    .batch-header .back-link:hover { text-decoration: underline; }
    p.meta { color: #666; font-size: 0.9rem; margin-bottom: 16px; }
    h3 { color: #2c3e50; margin-top: 16px; margin-bottom: 8px; font-size: 1.25rem; }
    table { width: 100%; border-collapse: collapse; margin-top: 12px; background: white; }
    th, td { border: 1px solid #ddd; padding: 10px 8px; font-size: 0.93rem; color: #34495e; }
    th { background-color: #f2f2f2; text-align: left; font-weight: 600; }
    tr:nth-child(even) { background-color: #fafafa; }
    tr:hover { background-color: #f1f1f1; }
    .duplicate-row { background-color: #fdecea !important; }
    td a { color: #2d85f8; text-decoration: none; font-weight: 500; }
    td a:hover { text-decoration: underline; }
  </style>
</head>
<body>

  <div class="container">

    <div class="sidebar">
      <h1><img src="{{ url_for('static', filename='parcel-scan.jpg') }}" width="200"></h1>
      <ul>
        <li><a href="{{ url_for('index') }}">New Batch</a></li>
        <li><a href="{{ url_for('all_batches') }}">Recorded Pick‐ups</a></li>
        <li><a href="{{ url_for('all_scans') }}">All Scans</a></li>
      </ul>
      <a href="{{ url_for('logout') }}" class="logout">Log Out</a>
      <div style="margin-top: 16px; padding-top: 16px; border-top: 1px solid #e0e0e0; font-size: 0.75rem; color: #999; text-align: center;">
        v{{ version }}
      </div>
    </div>

    <div class="main-content">

      {% with messages = get_flashed_messages(with_categories=true) %}
        {% for category, msg in messages %}
          <div class="flash {{ category }}">{{ msg }}</div>
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
          </tr>
        </thead>
        <tbody>
          {% for row in scans %}
            <tr class="{{ 'duplicate-row' if row.status.startswith('Duplicate') else '' }}">
              <td>{{ row.tracking_number }}</td>
              <td>{{ row.carrier }}</td>
              <td>
                {% if row.order_id %}
                  <a href="https://{{ shop_url }}/admin/orders/{{ row.order_id }}" target="_blank">
                    {{ row.order_number }}
                  </a>
                {% else %}
                  {{ row.order_number }}
                {% endif %}
              </td>
              <td>
                {% if row.order_id %}
                  <a href="https://{{ shop_url }}/admin/orders/{{ row.order_id }}" target="_blank">
                    {{ row.customer_name }}
                  </a>
                {% else %}
                  {{ row.customer_name }}
                {% endif %}
              </td>
              <td>{{ row.scan_date }}</td>
              <td>
                {% if row.status.startswith('Duplicate (Batch #') %}
                  {% set batch_num = row.status.split('#')[1].rstrip(')') %}
                  {% if batch_num and batch_num.isdigit() %}
                    Duplicate (<a href="{{ url_for('view_batch', batch_id=batch_num|int) }}" style="color: #2d85f8; text-decoration: none; font-weight: 500;">Batch #{{ batch_num }}</a>)
                  {% else %}
                    {{ row.status }}
                  {% endif %}
                {% else %}
                  {{ row.status }}
                {% endif %}
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>

    </div>

  </div>

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
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100%;
      font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
      background-color: #f5f6fa; color: #333;
    }
    .container { display: flex; height: 100vh; }
    .sidebar {
      width: 240px; background: #fff; border-right: 1px solid #e0e0e0;
      display: flex; flex-direction: column; padding: 24px 16px;
    }
    .sidebar h1 { font-size: 1.25rem; font-weight: bold; margin-bottom: 16px; color: #2c3e50; }
    .sidebar ul { list-style: none; margin-top: 8px; }
    .sidebar li { margin-bottom: 16px; }
    .sidebar a { text-decoration: none; color: #2d85f8; font-size: 1rem; font-weight: 500; }
    .sidebar a:hover { text-decoration: underline; }
    .sidebar .logout { margin-top: auto; color: #e74c3c; font-size: 0.95rem; text-decoration: none; }
    .sidebar .logout:hover { text-decoration: underline; }

    .main-content { flex: 1; overflow-y: auto; padding: 24px; }
    .flash { padding: 10px 14px; margin-bottom: 16px; border-radius: 4px; font-weight: 500; border: 1px solid; }
    .flash.success { background-color: #e0f7e9; color: #2f7a45; border-color: #b2e6c2; }
    .flash.error   { background-color: #fdecea; color: #a33a2f; border-color: #f5c6cb; }
    .flash.warning { background-color: #fff4e5; color: #8a6100; border-color: #ffe0b2; }

    h2 { font-size: 1.5rem; color: #2c3e50; margin-bottom: 16px; }

    .search-form { margin-top: 10px; margin-bottom: 20px; background: white; padding: 16px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
    .search-form input[type="text"] {
      padding: 8px 12px; font-size: 14px; width: 300px; border: 1px solid #ccc; border-radius: 4px;
    }
    .search-form button {
      padding: 8px 16px; font-size: 14px; border: none; border-radius: 4px; background-color: #2d85f8; color: #fff; cursor: pointer; margin-left: 8px;
    }
    .search-form button:hover { opacity: 0.92; }
    .search-form a { margin-left: 12px; font-size: 14px; text-decoration: none; color: #2d85f8; font-weight: 500; }

    table { width: 100%; border-collapse: collapse; margin-top: 12px; background: white; }
    th, td { border: 1px solid #ddd; padding: 10px 8px; font-size: 0.93rem; color: #34495e; }
    th { background-color: #f2f2f2; text-align: left; font-weight: 600; }
    tr:nth-child(even) { background-color: #fafafa; }
    tr:hover { background-color: #f1f1f1; }
    .duplicate-row { background-color: #fdecea !important; }
    td a { color: #2d85f8; text-decoration: none; font-weight: 500; }
    td a:hover { text-decoration: underline; }
    .btn-delete-small {
      padding: 4px 8px; font-size: 0.8rem; background-color: #e74c3c; color: #fff; border: none; border-radius: 4px; cursor: pointer;
    }
    .btn-delete-small:hover { opacity: 0.92; }
  </style>
</head>
<body>

  <div class="container">

    <div class="sidebar">
      <h1><img src="{{ url_for('static', filename='parcel-scan.jpg') }}" width="200"></h1>
      <ul>
        <li><a href="{{ url_for('index') }}">New Batch</a></li>
        <li><a href="{{ url_for('all_batches') }}">Recorded Pick‐ups</a></li>
        <li><a href="{{ url_for('all_scans') }}">All Scans</a></li>
      </ul>
      <a href="{{ url_for('logout') }}" class="logout">Log Out</a>
      <div style="margin-top: 16px; padding-top: 16px; border-top: 1px solid #e0e0e0; font-size: 0.75rem; color: #999; text-align: center;">
        v{{ version }}
      </div>
    </div>

    <div class="main-content">

      {% with messages = get_flashed_messages(with_categories=true) %}
        {% for category, msg in messages %}
          <div class="flash {{ category }}">{{ msg }}</div>
        {% endfor %}
      {% endwith %}

      <h2>All Scans</h2>

      <form class="search-form" method="get" action="{{ url_for('all_scans') }}">
        <label for="order_search"><strong>Search by Order # or Customer Name:</strong></label><br><br>
        <input type="text" name="order_number" id="order_search" value="{{ request.args.get('order_number','') }}" placeholder="Enter order number or name...">
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
            <th>Batch ID</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {% for s in scans %}
            <tr class="{{ 'duplicate-row' if s.status.startswith('Duplicate') else '' }}">
              <td>{{ s.tracking_number }}</td>
              <td>{{ s.carrier }}</td>
              <td>
                {% if s.order_id %}
                  <a href="https://{{ shop_url }}/admin/orders/{{ s.order_id }}" target="_blank">
                    {{ s.order_number }}
                  </a>
                {% else %}
                  {{ s.order_number }}
                {% endif %}
              </td>
              <td>
                {% if s.order_id %}
                  <a href="https://{{ shop_url }}/admin/orders/{{ s.order_id }}" target="_blank">
                    {{ s.customer_name }}
                  </a>
                {% else %}
                  {{ s.customer_name }}
                {% endif %}
              </td>
              <td>{{ s.scan_date }}</td>
              <td>
                {% if s.status.startswith('Duplicate (Batch #') %}
                  {% set batch_num = s.status.split('#')[1].rstrip(')') %}
                  {% if batch_num and batch_num.isdigit() %}
                    Duplicate (<a href="{{ url_for('view_batch', batch_id=batch_num|int) }}" style="color: #2d85f8; text-decoration: none; font-weight: 500;">Batch #{{ batch_num }}</a>)
                  {% else %}
                    {{ s.status }}
                  {% endif %}
                {% else %}
                  {{ s.status }}
                {% endif %}
              </td>
              <td>{{ s.batch_id or '' }}</td>
              <td>
                <form action="{{ url_for('delete_scan') }}" method="post" style="display: inline;"
                      onsubmit="return confirm('Are you sure you want to delete this scan?');">
                  <input type="hidden" name="scan_id"  value="{{ s.id }}">
                  <button type="submit" class="btn-delete-small">Delete</button>
                </form>
              </td>
            </tr>
          {% endfor %}
        </tbody>
      </table>

    </div>

  </div>

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

    # if they've been idle too long, clear session & go to login
    if last and (now - last) > INACTIVITY_TIMEOUT:
        session.clear()
        flash("Logged out due to 30m inactivity.", "error")
        return redirect(url_for("login"))

    # stamp this request's activity
    session["last_active"] = now

    # then enforce that they must be authenticated
    if not session.get("authenticated"):
        return redirect(url_for("login"))


# ─────────────────────────────────────────────────────────────────────────────
# ── Routes ────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        entered = request.form.get("password", "").encode()
        if bcrypt.checkpw(entered, PASSWORD_HASH):
            session.clear()
            session["authenticated"] = True
            session["last_active"]  = time.time()
            return redirect(url_for("index"))
        else:
            flash("Invalid password. Please try again.", "error")
    return render_template_string(LOGIN_TEMPLATE)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
def index():
    batch_id = session.get("batch_id")
    if not batch_id:
        # No batch open → show "Create New Batch"
        return render_template_string(
            MAIN_TEMPLATE,
            current_batch=None,
            scans=[],
            shop_url=SHOP_URL,
            version=__version__
        )

    conn = get_mysql_connection()
    try:
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
            flash("Batch not found. Please start a new batch.", "error")
            return redirect(url_for("index"))

        # Fetch all scans in this batch
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

        return render_template_string(
            MAIN_TEMPLATE,
            current_batch=batch_row,
            scans=scans,
            shop_url=SHOP_URL,
            version=__version__
        )
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


@app.route("/new_batch", methods=["POST"])
def new_batch():
    carrier = request.form.get("carrier", "").strip()
    if carrier not in ("UPS", "Canada Post", "DHL", "Purolator"):
        flash("Please select a valid carrier.", "error")
        return redirect(url_for("index"))

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
          INSERT INTO batches (created_at, pkg_count, tracking_numbers, carrier)
          VALUES (%s, %s, %s, %s)
        """, (created_at, 0, "", carrier))
        conn.commit()

        batch_id = cursor.lastrowid
        session["batch_id"] = batch_id

        flash(f"Started new {carrier} batch (ID {batch_id}). Scan parcels below.", "success")
        return redirect(url_for("index"))
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


@app.route("/edit_batch/<int:batch_id>", methods=["GET"])
def edit_batch(batch_id):
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM batches WHERE id = %s", (batch_id,))
        if not cursor.fetchone():
            flash(f"Batch #{batch_id} not found.", "error")
            return redirect(url_for("all_batches"))
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()

    # stash it back in session so index() shows the scan UI
    session["batch_id"] = batch_id
    flash(f"Editing batch #{batch_id}.", "success")
    return redirect(url_for("index"))


@app.route("/cancel_batch", methods=["GET"])
def cancel_batch():
    batch_id = session.pop("batch_id", None)
    if not batch_id:
        return redirect(url_for("index"))

    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scans WHERE batch_id = %s", (batch_id,))
        cursor.execute("DELETE FROM batches WHERE id = %s", (batch_id,))
        conn.commit()
        flash(f"Batch #{batch_id} canceled.", "success")
        return redirect(url_for("index"))
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


@app.route("/delete_batch", methods=["POST"])
def delete_batch():
    batch_id = request.form.get("batch_id")
    if not batch_id:
        flash("No batch specified for deletion.", "error")
        return redirect(url_for("all_batches"))

    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scans WHERE batch_id = %s", (batch_id,))
        cursor.execute("DELETE FROM batches WHERE id = %s", (batch_id,))
        conn.commit()
        flash(f"Batch #{batch_id} and its scans have been deleted.", "success")
    except mysql.connector.Error as e:
        flash(f"MySQL Error: {e}", "error")
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()

    return redirect(url_for("all_batches"))


def process_scan_apis_background(scan_id, tracking_number, batch_carrier):
    """
    Background thread function to process API calls after scan is already saved.
    This runs AFTER the response is sent to user, so scanning can continue immediately.
    """
    import threading
    # No delay needed - response is already sent before thread starts

    conn = get_mysql_connection()
    try:
        # Initialize with defaults
        order_number = "N/A"
        customer_name = "Not Found"
        order_id = ""
        scan_carrier = batch_carrier
        
        # ── ShipStation lookup with retry logic ──
        shipstation_found = False
        if SHIPSTATION_API_KEY and SHIPSTATION_API_SECRET:
            max_retries = 4
            for retry in range(max_retries):
                try:
                    url = f"https://ssapi.shipstation.com/shipments?trackingNumber={tracking_number}"
                    resp = requests.get(
                        url,
                        auth=(SHIPSTATION_API_KEY, SHIPSTATION_API_SECRET),
                        headers={"Accept": "application/json"},
                        timeout=12  # Increased from 6 to 12 seconds
                    )

                    # Handle 503 and other 5xx errors with retry
                    if resp.status_code == 503 or (500 <= resp.status_code < 600):
                        wait = min(2 ** retry, 8)
                        print(f"ShipStation {resp.status_code} error for {tracking_number}, retry {retry + 1}/{max_retries} after {wait}s")
                        if retry < max_retries - 1:
                            time.sleep(wait)
                            continue
                        else:
                            break

                    resp.raise_for_status()

                    # Validate response is JSON before parsing
                    content_type = resp.headers.get('Content-Type', '')
                    if 'application/json' not in content_type:
                        print(f"ShipStation returned non-JSON response for {tracking_number}. Content-Type: {content_type}")
                        print(f"Response preview: {resp.text[:200]}")
                        break  # Exit retry loop, use defaults

                    try:
                        data = resp.json()
                    except ValueError as e:
                        print(f"ShipStation JSON parse error for {tracking_number}: {e}")
                        print(f"Response preview: {resp.text[:200]}")
                        break  # Exit retry loop, use defaults

                    shipments = data.get("shipments", [])

                    if shipments:
                        shipstation_found = True
                        first = shipments[0]
                        order_number = first.get("orderNumber", "N/A")
                        ship_to = first.get("shipTo", {})
                        customer_name = ship_to.get("name", "No Name") if ship_to else "No Name"
                        carrier_code = first.get("carrierCode", "").lower()

                        carrier_map = {
                            "ups": "UPS",
                            "canadapost": "Canada Post",
                            "canada_post": "Canada Post",
                            "dhl": "DHL",
                            "dhl_express": "DHL",
                            "purolator": "Purolator",
                        }
                        scan_carrier = carrier_map.get(carrier_code, batch_carrier)
                    break  # Success, exit retry loop

                except requests.exceptions.Timeout as e:
                    wait = min(2 ** retry, 8)
                    print(f"ShipStation timeout for {tracking_number}, retry {retry + 1}/{max_retries} after {wait}s: {e}")
                    if retry < max_retries - 1:
                        time.sleep(wait)
                    else:
                        print(f"ShipStation failed after {max_retries} retries for {tracking_number}")

                except Exception as e:
                    wait = min(2 ** retry, 8)
                    print(f"ShipStation error for {tracking_number}, retry {retry + 1}/{max_retries}: {e}")
                    if retry < max_retries - 1:
                        time.sleep(wait)
                    else:
                        print(f"ShipStation failed after {max_retries} retries for {tracking_number}")
                    break

        # ── Shopify lookup ──
        shopify_found = False
        try:
            shopify_api = get_shopify_api()
            shopify_info = shopify_api.get_order_by_tracking(tracking_number)

            if shopify_info and shopify_info.get("order_id"):
                shopify_found = True
                order_number = shopify_info.get("order_number", order_number)
                customer_name = shopify_info.get("customer_name", customer_name)
                order_id = shopify_info.get("order_id", order_id)
                print(f"Shopify lookup successful for {tracking_number}: order {order_number}")
            else:
                print(f"Shopify lookup found no order for {tracking_number}")
        except Exception as e:
            print(f"Shopify error for {tracking_number}: {e}")
            import traceback
            traceback.print_exc()

        # ── Fallback carrier detection ──
        if not scan_carrier or scan_carrier == "":
            if len(tracking_number) == 12:
                scan_carrier = "Purolator"
            elif len(tracking_number) == 10:
                scan_carrier = "DHL"
            elif tracking_number.startswith("1Z"):
                scan_carrier = "UPS"
            elif tracking_number.startswith("2016"):
                scan_carrier = "Canada Post"
            elif tracking_number.startswith("LA") or len(tracking_number) == 30:
                scan_carrier = "USPS"
            else:
                scan_carrier = batch_carrier

        # ── Update the scan record with API results ──
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE scans
            SET carrier = %s,
                order_number = %s,
                customer_name = %s,
                order_id = %s,
                status = 'Complete'
            WHERE id = %s
            """,
            (scan_carrier, order_number, customer_name, order_id, scan_id)
        )
        conn.commit()
        cursor.close()
        
    except Exception as e:
        print(f"Background API processing error for scan {scan_id}: {e}")
    finally:
        conn.close()


@app.route("/scan", methods=["POST"])
def scan():
    """
    INSTANT scan endpoint - inserts to database immediately,
    then processes APIs in background thread.
    
    ✨ NEW: Checks for duplicates across ALL batches in the database,
    not just the current batch.
    """
    code = request.form.get("code", "").strip()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    if not code:
        if is_ajax:
            return jsonify({"success": False, "error": "No code received."}), 400
        flash("No code received.", "error")
        return redirect(url_for("index"))

    batch_id = session.get("batch_id")
    if not batch_id:
        if is_ajax:
            return jsonify({"success": False, "error": "No batch open."}), 400
        flash("No batch open. Please start a new batch first.", "error")
        return redirect(url_for("index"))

    conn = get_mysql_connection()
    try:
        # Get the batch's configured carrier
        cursor = conn.cursor()
        cursor.execute("SELECT carrier FROM batches WHERE id = %s", (batch_id,))
        row = cursor.fetchone()
        cursor.close()
        batch_carrier = (row[0] if row else "") or ""

        # Normalize codes for specific carriers
        original_code = code
        if batch_carrier == "Canada Post":
            if len(code) >= 12:
                code = code[7:-5]
        elif batch_carrier == "Purolator":
            if len(code) == 34:
                code = code[11:-11]

        # ─────────────────────────────────────────────────────────────────
        # ✨ NEW: Check for duplicate across ALL BATCHES in the database
        # ─────────────────────────────────────────────────────────────────
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT batch_id, scan_date, order_number, customer_name, order_id, carrier
            FROM scans 
            WHERE tracking_number = %s 
            ORDER BY scan_date DESC 
            LIMIT 1
            """,
            (code,)
        )
        existing_scan = cursor.fetchone()
        cursor.close()
        
        # Determine if this is a duplicate and create appropriate status message
        if existing_scan:
            is_duplicate = True
            original_batch_id = existing_scan['batch_id']
            # Check if it's a duplicate within the SAME batch or a DIFFERENT batch
            if original_batch_id == batch_id:
                status = "Duplicate (This Batch)"
            else:
                status = f"Duplicate (Batch #{original_batch_id})"
        else:
            is_duplicate = False
            status = "Processing"
        # ─────────────────────────────────────────────────────────────────

        # Set order details - use existing data for duplicates, placeholders for new scans
        if is_duplicate and existing_scan:
            # Copy order details from the existing scan
            order_number = existing_scan.get('order_number', 'Processing...')
            customer_name = existing_scan.get('customer_name', 'Looking up...')
            order_id = existing_scan.get('order_id', '')
            # Use the carrier from existing scan if available
            scan_carrier = existing_scan.get('carrier', batch_carrier) or batch_carrier
        else:
            # Use placeholders for new scans (will be filled by background thread)
            order_number = "Processing..."
            customer_name = "Looking up..."
            order_id = ""
            # Detect carrier from tracking number format (quick, no API)
            scan_carrier = batch_carrier
            if len(code) == 12:
                scan_carrier = "Purolator"
            elif len(code) == 10:
                scan_carrier = "DHL"
            elif code.startswith("1Z"):
                scan_carrier = "UPS"
            elif code.startswith("2016"):
                scan_carrier = "Canada Post"
            elif code.startswith("LA") or len(code) == 30:
                scan_carrier = "USPS"
        
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # ── INSERT IMMEDIATELY (no waiting for APIs) ──
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
        scan_id = cursor.lastrowid
        cursor.close()

        # ── Launch background thread for API calls (only if not duplicate) ──
        # Note: We still insert the scan record even if duplicate, but we don't
        # need to fetch order details for duplicates since they're already known
        if not is_duplicate:
            import threading
            api_thread = threading.Thread(
                target=process_scan_apis_background,
                args=(scan_id, code, batch_carrier),
                daemon=True
            )
            api_thread.start()

        # ── Return IMMEDIATELY (don't wait for APIs) ──
        if is_ajax:
            # Create a more informative message for duplicates
            if is_duplicate:
                if existing_scan['batch_id'] == batch_id:
                    message = f"⚠️ DUPLICATE: {code} was already scanned in THIS batch"
                else:
                    message = f"⚠️ DUPLICATE: {code} was previously scanned in Batch #{existing_scan['batch_id']}"
            else:
                message = f"✓ Scanned: {code}"
            
            return jsonify({
                "success": True,
                "scan": {
                    "id": scan_id,
                    "tracking_number": code,
                    "carrier": scan_carrier,
                    "order_number": order_number,
                    "customer_name": customer_name,
                    "scan_date": now_str,
                    "status": status,
                    "order_id": order_id
                },
                "message": message
            })
        else:
            if is_duplicate:
                if existing_scan['batch_id'] == batch_id:
                    flash(f"⚠️ DUPLICATE: {code} was already scanned in THIS batch", "warning")
                else:
                    flash(f"⚠️ DUPLICATE: {code} was previously scanned in Batch #{existing_scan['batch_id']}", "warning")
            else:
                flash(f"Recorded scan: {code} (Status: {status}, Carrier: {scan_carrier})", "success")
            return redirect(url_for("index"))

    except mysql.connector.errors.PoolError as e:
        error_msg = "Database connection pool exhausted - please wait a moment and try again"
        print(f"Pool exhaustion during scan: {e}")
        if is_ajax:
            return jsonify({"success": False, "error": error_msg}), 503
        flash(error_msg, "error")
        return redirect(url_for("index"))
    except mysql.connector.Error as e:
        error_msg = f"Database error: {e}"
        print(f"MySQL error during scan: {e}")
        if is_ajax:
            return jsonify({"success": False, "error": "Database temporarily unavailable"}), 503
        flash("Database temporarily unavailable, please try again", "error")
        return redirect(url_for("index"))
    except Exception as e:
        error_msg = str(e)
        print(f"Unexpected error during scan: {e}")
        import traceback
        traceback.print_exc()
        if is_ajax:
            return jsonify({"success": False, "error": error_msg}), 500
        flash(f"Error processing scan: {e}", "error")
        return redirect(url_for("index"))
    finally:
        try:
            conn.close()
        except Exception:
            pass  # Connection might already be closed or not exist


@app.route("/delete_scans", methods=["POST"])
def delete_scans():
    batch_id = session.get("batch_id")
    if not batch_id:
        flash("No batch open.", "error")
        return redirect(url_for("index"))

    scan_ids = request.form.getlist("delete_scan_ids")
    if not scan_ids:
        flash("No scans selected for deletion.", "error")
        return redirect(url_for("index"))

    try:
        conn = get_mysql_connection()
    except mysql.connector.errors.PoolError:
        flash("Database connection pool busy - please wait a moment and try again", "error")
        return redirect(url_for("index"))
    except Exception as e:
        flash(f"Database connection error: {e}", "error")
        return redirect(url_for("index"))

    try:
        cursor = conn.cursor()
        placeholders = ",".join(["%s"] * len(scan_ids))
        sql = f"DELETE FROM scans WHERE id IN ({placeholders}) AND batch_id = %s"
        params = scan_ids + [batch_id]
        cursor.execute(sql, params)
        conn.commit()
        flash(f"Deleted {len(scan_ids)} scan(s).", "success")
        return redirect(url_for("index"))
    except mysql.connector.Error as e:
        print(f"MySQL error during delete: {e}")
        flash("Database temporarily unavailable - delete failed, please try again", "error")
        return redirect(url_for("index"))
    except Exception as e:
        print(f"Error deleting scans: {e}")
        flash(f"Error: {e}", "error")
        return redirect(url_for("index"))
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@app.route("/delete_scan", methods=["POST"])
def delete_scan():
    scan_id = request.form.get("scan_id")
    if not scan_id:
        flash("No scan specified for deletion.", "error")
        return redirect(url_for("all_scans"))

    try:
        conn = get_mysql_connection()
    except mysql.connector.errors.PoolError:
        flash("Database connection pool busy - please wait a moment and try again", "error")
        return redirect(url_for("all_scans"))
    except Exception as e:
        flash(f"Database connection error: {e}", "error")
        return redirect(url_for("all_scans"))

    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scans WHERE id = %s", (scan_id,))
        conn.commit()
        flash(f"Deleted scan #{scan_id}.", "success")
    except mysql.connector.Error as e:
        print(f"MySQL error during delete: {e}")
        flash("Database temporarily unavailable - delete failed, please try again", "error")
    except Exception as e:
        print(f"Error deleting scan: {e}")
        flash(f"Error: {e}", "error")
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    return redirect(url_for("all_scans"))


@app.route("/record_batch", methods=["POST"])
def record_batch():
    batch_id = session.pop("batch_id", None)
    if not batch_id:
        flash("No batch open.", "error")
        return redirect(url_for("index"))

    conn = get_mysql_connection()
    try:
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
        flash(f"Batch #{batch_id} recorded with {pkg_count} parcel(s).", "success")
        return redirect(url_for("index"))
    except mysql.connector.Error as e:
        flash(f"MySQL Error: {e}", "error")
        return redirect(url_for("index"))
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


@app.route("/all_batches", methods=["GET"])
def all_batches():
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
          SELECT id, carrier, created_at, pkg_count, tracking_numbers
            FROM batches
           ORDER BY created_at DESC
        """)
        batches = cursor.fetchall()
        return render_template_string(
            ALL_BATCHES_TEMPLATE,
            batches=batches,
            shop_url=SHOP_URL,
            version=__version__
        )
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


@app.route("/view_batch/<int:batch_id>", methods=["GET"])
def view_batch(batch_id):
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
          SELECT id, carrier, created_at, pkg_count, tracking_numbers
            FROM batches
           WHERE id = %s
        """, (batch_id,))
        batch = cursor.fetchone()
        if not batch:
            flash(f"Batch #{batch_id} not found.", "error")
            return redirect(url_for("all_batches"))

        cursor.execute("""
          SELECT id,
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

        return render_template_string(
            BATCH_VIEW_TEMPLATE,
            batch=batch,
            scans=scans,
            shop_url=SHOP_URL,
            version=__version__
        )
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


@app.route("/api/batch/<int:batch_id>/updates", methods=["GET"])
def get_batch_updates(batch_id):
    """
    API endpoint to get updated scan information for auto-refresh.
    Returns scans that have been updated in the last 60 seconds.
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        
        # Get scans from this batch that were recently updated
        # (we check scans updated in last 60 seconds to catch background API updates)
        cursor.execute("""
          SELECT
            id,
            tracking_number,
            carrier,
            order_number,
            customer_name,
            status,
            order_id
          FROM scans
          WHERE batch_id = %s
            AND (order_number != 'Processing...' OR status = 'Complete')
          ORDER BY scan_date DESC
        """, (batch_id,))
        
        scans = cursor.fetchall()
        
        return jsonify({
            "success": True,
            "scans": scans
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


@app.route("/all_scans", methods=["GET"])
def all_scans():
    order_search = request.args.get("order_number", "").strip()

    conn = get_mysql_connection()
    try:
        cursor = conn.cursor(dictionary=True)

        if order_search:
            like_pattern = f"%{order_search}%"
            cursor.execute("""
              SELECT
                id,
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
                id,
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

        return render_template_string(
            ALL_SCANS_TEMPLATE,
            scans=scans,
            shop_url=SHOP_URL,
            version=__version__
        )
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
