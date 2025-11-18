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
from datetime import datetime, timedelta
import threading
import csv
import io

from shopify_api import ShopifyAPI  # Assumes shopify_api.py is alongside this file
from klaviyo_events import KlaviyoEvents  # Klaviyo integration for event tracking
from ups_api import UPSAPI  # UPS tracking integration
from tracking_utils import split_concatenated_tracking_numbers  # Tracking number split detection
from address_utils import is_po_box, check_po_box_compatibility  # PO Box detection

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
    Validates connection is alive before returning.
    """
    max_retries = 3
    for retry in range(max_retries):
        try:
            conn = db_pool.get_connection()

            # Validate connection is alive
            try:
                conn.ping(reconnect=True, attempts=2, delay=1)
            except Exception as ping_error:
                print(f"Connection ping failed, getting new connection: {ping_error}")
                try:
                    conn.close()
                except Exception:
                    pass
                if retry < max_retries - 1:
                    continue
                raise

            return conn
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

# ── Klaviyo singleton ──
_klaviyo_events = None
def get_klaviyo_events():
    global _klaviyo_events
    if _klaviyo_events is None:
        _klaviyo_events = KlaviyoEvents()
    return _klaviyo_events

# ── UPS singleton ──
_ups_api = None
def get_ups_api():
    global _ups_api
    if _ups_api is None:
        _ups_api = UPSAPI()
    return _ups_api

# ── Shipments Cache System ──
def init_shipments_cache():
    """
    Initialize the shipments_cache table if it doesn't exist.
    This table caches ShipStation shipment data for faster Check Shipments page loads.
    """
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS shipments_cache (
                id INT AUTO_INCREMENT PRIMARY KEY,
                tracking_number VARCHAR(255) NOT NULL,
                order_number VARCHAR(255),
                customer_name VARCHAR(255),
                carrier_code VARCHAR(50),
                ship_date DATE,
                shipstation_batch_number VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_tracking (tracking_number),
                INDEX idx_ship_date (ship_date),
                INDEX idx_order_number (order_number),
                UNIQUE KEY unique_tracking (tracking_number)
            )
        """)
        conn.commit()
        cursor.close()
        conn.close()
        print("✓ Shipments cache table initialized")
    except Exception as e:
        print(f"❌ Error initializing shipments cache: {e}")

# Initialize cache table on startup
init_shipments_cache()

def sync_shipments_from_shipstation():
    """
    Background sync function that pulls shipments from ShipStation and updates the cache.
    Runs every 5 minutes in a background thread.
    """
    print("🔄 Starting shipments sync from ShipStation...")
    try:
        if not SHIPSTATION_API_KEY or not SHIPSTATION_API_SECRET:
            print("⚠️ ShipStation credentials not configured, skipping sync")
            return

        start_date = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%S")
        page = 1
        total_synced = 0

        while True:
            params = {
                "shipDateStart": start_date,
                "pageSize": 500,  # Max page size
                "page": page,
                "sortBy": "ShipDate",
                "sortDir": "DESC"
            }

            response = requests.get(
                "https://ssapi.shipstation.com/shipments",
                auth=(SHIPSTATION_API_KEY, SHIPSTATION_API_SECRET),
                params=params,
                timeout=30
            )

            if response.status_code != 200:
                print(f"❌ ShipStation sync error: {response.status_code}")
                break

            data = response.json()
            shipments_data = data.get("shipments", [])

            if not shipments_data:
                break

            # Batch insert/update into cache
            conn = get_mysql_connection()
            cursor = conn.cursor()

            for ss_ship in shipments_data:
                tracking_number = ss_ship.get("trackingNumber", "")
                if not tracking_number:
                    continue

                order_number = ss_ship.get("orderNumber", "")
                carrier_code = ss_ship.get("carrierCode", "").upper()
                ship_date = ss_ship.get("shipDate", "")[:10]  # Just date part
                shipstation_batch_number = ss_ship.get("batchNumber", "")

                ship_to = ss_ship.get("shipTo", {})
                customer_name = ship_to.get("name", "Unknown") if ship_to else "Unknown"

                cursor.execute("""
                    INSERT INTO shipments_cache
                    (tracking_number, order_number, customer_name, carrier_code, ship_date, shipstation_batch_number)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        order_number = VALUES(order_number),
                        customer_name = VALUES(customer_name),
                        carrier_code = VALUES(carrier_code),
                        ship_date = VALUES(ship_date),
                        shipstation_batch_number = VALUES(shipstation_batch_number),
                        updated_at = CURRENT_TIMESTAMP
                """, (tracking_number, order_number, customer_name, carrier_code, ship_date, shipstation_batch_number))
                total_synced += 1

            conn.commit()
            cursor.close()
            conn.close()

            print(f"✓ Synced page {page} ({len(shipments_data)} shipments)")

            if page >= data.get("pages", 1):
                break
            page += 1

        print(f"✅ Shipments sync complete! Total synced: {total_synced}")
    except Exception as e:
        print(f"❌ Error syncing shipments: {e}")

def backfill_split_tracking_numbers():
    """
    Backfill split tracking numbers - finds concatenated tracking numbers and splits them.
    """
    print("🔄 Checking for concatenated tracking numbers to split...")
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)

        # Find scans that might have concatenated tracking numbers
        cursor.execute("""
            SELECT id, tracking_number, carrier, order_number, customer_name,
                   customer_email, batch_id, scan_date, status, order_id,
                   shipstation_batch_number
            FROM scans
            WHERE (
                LENGTH(tracking_number) = 36 OR   -- Two UPS
                LENGTH(tracking_number) = 32 OR   -- Two Canada Post
                LENGTH(tracking_number) = 24      -- Two FedEx/Purolator
            )
            AND status NOT LIKE '%Split%'  -- Don't re-process already split scans
            ORDER BY scan_date DESC
            LIMIT 100
        """)
        scans = cursor.fetchall()

        if not scans:
            print("✓ No concatenated tracking numbers found")
            cursor.close()
            conn.close()
            return

        print(f"🔍 Found {len(scans)} scans with suspicious lengths, checking for splits...")
        total_split = 0
        total_created = 0

        for scan in scans:
            tracking_number = scan['tracking_number']
            split_numbers = split_concatenated_tracking_numbers(tracking_number)

            if len(split_numbers) > 1:
                print(f"  📦 Splitting scan #{scan['id']}: {tracking_number}")
                print(f"     Into {len(split_numbers)}: {', '.join(split_numbers)}")

                # Create new scan records for each split tracking number
                for individual_tracking in split_numbers:
                    from tracking_utils import detect_carrier
                    detected_carrier = detect_carrier(individual_tracking)

                    cursor.execute(
                        """
                        INSERT INTO scans
                          (tracking_number, carrier, order_number, customer_name,
                           scan_date, status, order_id, customer_email, batch_id, shipstation_batch_number)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            individual_tracking,
                            detected_carrier,
                            "Processing...",
                            "Looking up...",
                            scan['scan_date'],
                            "Split from concatenated scan",
                            "",
                            "",
                            scan['batch_id'],
                            ""
                        )
                    )
                    total_created += 1

                # Mark original as split
                cursor.execute(
                    """
                    UPDATE scans
                    SET status = %s, order_number = %s
                    WHERE id = %s
                    """,
                    (f"Split into {len(split_numbers)} scans", f"SPLIT ({len(split_numbers)})", scan['id'])
                )

                conn.commit()
                total_split += 1

        cursor.close()
        conn.close()

        if total_split > 0:
            print(f"✅ Split {total_split} concatenated scans into {total_created} new scans")
        else:
            print("✓ No concatenated tracking numbers needed splitting")

    except Exception as e:
        print(f"❌ Error during split tracking backfill: {e}")
        import traceback
        traceback.print_exc()


def backfill_missing_emails():
    """
    Backfill customer emails for scans that are missing email addresses.
    Fetches from ShipStation first, then Shopify as fallback.
    """
    print("🔄 Starting email backfill for scans with missing emails...")
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)

        # Find scans without customer email
        cursor.execute("""
            SELECT id, tracking_number, order_id
            FROM scans
            WHERE (customer_email IS NULL OR customer_email = '')
            ORDER BY scan_date DESC
            LIMIT 500
        """)
        scans = cursor.fetchall()

        if not scans:
            print("✓ No scans need email backfill")
            cursor.close()
            conn.close()
            return

        print(f"📧 Found {len(scans)} scans missing customer email")
        updated = 0

        for scan in scans:
            scan_id = scan['id']
            tracking_number = scan['tracking_number']
            email = None

            # Try ShipStation first
            try:
                if SHIPSTATION_API_KEY and SHIPSTATION_API_SECRET:
                    response = requests.get(
                        "https://ssapi.shipstation.com/shipments",
                        auth=(SHIPSTATION_API_KEY, SHIPSTATION_API_SECRET),
                        params={"trackingNumber": tracking_number},
                        timeout=10
                    )

                    if response.status_code == 200:
                        data = response.json()
                        shipments = data.get("shipments", [])

                        if shipments:
                            first = shipments[0]

                            # Check multiple possible email locations
                            if "customerEmail" in first and first.get("customerEmail"):
                                email = first.get("customerEmail")
                            elif "buyerEmail" in first and first.get("buyerEmail"):
                                email = first.get("buyerEmail")

                            if not email:
                                ship_to = first.get("shipTo", {})
                                if ship_to and ship_to.get("email"):
                                    email = ship_to.get("email")

                            if not email:
                                bill_to = first.get("billTo", {})
                                if bill_to and bill_to.get("email"):
                                    email = bill_to.get("email")

                            if not email:
                                advanced = first.get("advancedOptions", {})
                                for field in ["customField1", "customField2", "customField3"]:
                                    value = advanced.get(field, "")
                                    if "@" in str(value):
                                        email = value
                                        break

                    time.sleep(0.5)  # Rate limiting
            except Exception as e:
                print(f"  ShipStation error for {tracking_number}: {e}")

            # Try Shopify if ShipStation didn't work
            if not email and scan['order_id']:
                try:
                    shopify_api = get_shopify_api()
                    if shopify_api and shopify_api.access_token:
                        order = shopify_api.get_order(scan['order_id'])
                        if order and order.get('customer') and order['customer'].get('email'):
                            email = order['customer']['email']
                        time.sleep(0.5)  # Rate limiting
                except Exception as e:
                    print(f"  Shopify error for order {scan['order_id']}: {e}")

            # Update database if we found an email
            if email:
                cursor.execute("""
                    UPDATE scans
                    SET customer_email = %s
                    WHERE id = %s
                """, (email, scan_id))
                conn.commit()
                updated += 1
                print(f"  ✓ Updated {tracking_number}: {email}")

        cursor.close()
        conn.close()
        print(f"✅ Email backfill complete! Updated {updated}/{len(scans)} scans")
    except Exception as e:
        print(f"❌ Error during email backfill: {e}")

def start_background_sync():
    """
    Start background thread that syncs shipments every 5 minutes.
    Also runs email backfill and split tracking backfill on startup and once per day.
    """
    def sync_loop():
        # Run backfills immediately on startup
        backfill_split_tracking_numbers()  # First, split any concatenated tracking numbers
        backfill_missing_emails()  # Then, fill missing emails for all scans (including newly split ones)

        last_backfill = datetime.now()

        while True:
            sync_shipments_from_shipstation()

            # Run backfills once per day
            if (datetime.now() - last_backfill).total_seconds() > 86400:  # 24 hours
                backfill_split_tracking_numbers()
                backfill_missing_emails()
                last_backfill = datetime.now()

            time.sleep(300)  # 5 minutes

    thread = threading.Thread(target=sync_loop, daemon=True)
    thread.start()
    print("✓ Background shipments sync started (every 5 minutes)")
    print("✓ Split tracking & email backfill will run on startup and once per day")

# Start background sync on app startup
start_background_sync()

# ── Item Location Helpers ──
def get_item_location(sku: str, item_name: str) -> str:
    """
    Find warehouse location for an item based on SKU or keyword matching.
    Returns location string like "Aisle 3, Shelf B" or empty string if not found.
    """
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)

        # First, try exact SKU match
        cursor.execute("""
            SELECT aisle, shelf
            FROM item_location_rules
            WHERE rule_type = 'sku' AND UPPER(rule_value) = UPPER(%s)
            LIMIT 1
        """, (sku,))
        result = cursor.fetchone()

        if result:
            cursor.close()
            conn.close()
            return f"{result['aisle']}, {result['shelf']}"

        # If no SKU match, try keyword matching
        cursor.execute("""
            SELECT aisle, shelf, rule_value
            FROM item_location_rules
            WHERE rule_type = 'keyword'
            ORDER BY LENGTH(rule_value) DESC
        """)
        keyword_rules = cursor.fetchall()

        cursor.close()
        conn.close()

        # Check if any keyword is in the item name (case-insensitive)
        item_name_upper = item_name.upper()
        for rule in keyword_rules:
            if rule['rule_value'].upper() in item_name_upper:
                return f"{rule['aisle']}, {rule['shelf']}"

        return ""

    except Exception as e:
        print(f"Error fetching item location: {e}")
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# ── Templates ─────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

LOGIN_TEMPLATE = r'''
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Login – H&amp;O Parcel Scans</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Figtree:ital,wght@0,300..900;1,300..900&display=swap" rel="stylesheet">
  <style>
    html, body {
      height: 100%;
      margin: 0;
      font-family: "Figtree", sans-serif;
      font-optical-sizing: auto;
      background-color: #fbfaf5;
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
      background-color: #534bc4;
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
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Figtree:ital,wght@0,300..900;1,300..900&display=swap" rel="stylesheet">
  <style>
    /* Reset & Base */
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100%;
      font-family: "Figtree", sans-serif;
      font-optical-sizing: auto;
      background-color: #fbfaf5;
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
    .sidebar li { margin-bottom: 8px; }
    .sidebar a {
      display: block;
      padding: 8px 12px;
      text-decoration: none;
      color: #534bc4;
      font-size: 1rem;
      font-weight: 500;
      border-radius: 4px;
      transition: background-color 0.2s;
    }
    .sidebar a:hover { background-color: #f0f0f0; }
    .sidebar .logout {
      display: block;
      margin-top: auto;
      padding: 8px 12px;
      color: #952746;
      font-size: 0.95rem;
      text-decoration: none;
      border-radius: 4px;
      transition: background-color 0.2s;
    }
    .sidebar .logout:hover { background-color: #fdecea; }

    /* ── MAIN CONTENT ── */
    .main-content { flex: 1; overflow-y: auto; padding: 24px; }
    .flash {
      padding: 10px 14px; margin-bottom: 16px; border-radius: 4px; font-weight: 500; border: 1px solid;
      animation: slideIn 0.3s ease-out;
    }
    .flash.success { background-color: #e0f7e9; color: #199b76; border-color: #b2e6c2; }
    .flash.error   { background-color: #fdecea; color: #952746; border-color: #f5c6cb; }
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
    .btn-new { background-color: #534bc4; color: white; }
    .btn-delete { background-color: #952746; color: white; }
    .btn-batch { background-color: #199b76; color: white; }
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
    .scan-status.success { background-color: #e0f7e9; color: #199b76; border: 1px solid #b2e6c2; }
    .scan-status.error { background-color: #fdecea; color: #952746; border: 1px solid #f5c6cb; }

    table { width: 100%; border-collapse: collapse; margin-top: 12px; background: white; }
    th, td { border: 1px solid #ddd; padding: 10px 8px; font-size: 0.93rem; color: #34495e; }
    th { background-color: #eeeee5; text-align: left; font-weight: 600; }
    tr:nth-child(even) { background-color: #fbfaf5; }
    tr:hover { background-color: #f1f1f1; }
    .duplicate-row { background-color: #fdecea !important; }
    .duplicate-row:hover { background-color: #fbd5d0 !important; }
    td a { color: #534bc4; text-decoration: none; font-weight: 500; }
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
    .batch-actions a { color: #952746; text-decoration: none; font-size: 0.9rem; font-weight: 500; }
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
      border-top: 2px solid #534bc4;
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
        <li><a href="{{ url_for('new_batch') }}">New Batch</a></li>
        <li><a href="{{ url_for('all_batches') }}">Recorded Pick‐ups</a></li>
        <li><a href="{{ url_for('all_scans') }}">All Scans</a></li>
        <li><a href="{{ url_for('stuck_orders') }}">Fix Stuck Orders</a></li>
        <li><a href="{{ url_for('pick_and_pack') }}">Pick and Pack</a></li>
        <li><a href="{{ url_for('item_locations') }}">Item Locations</a></li>
        <li><a href="{{ url_for('check_shipments') }}">Check Shipments</a></li>
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
            {% set batch_status = current_batch.get('status', 'in_progress') %}
            <p style="margin-top: 8px;">
              <strong>Status:</strong>
              {% if batch_status == 'notified' %}
                <span style="color: #199b76;">✉ Notified</span>
              {% elif batch_status == 'recorded' %}
                <span style="color: #f39c12;">✓ Picked Up (Ready to notify)</span>
              {% else %}
                <span style="color: #666;">⏳ In Progress</span>
              {% endif %}
            </p>
            <p style="font-size: 0.85rem; color: #666; margin-top: 4px;">
              💡 Tip: Order details load in background. Refresh page to see updated info.
            </p>
          </div>
          <div class="batch-actions">
            <form action="{{ url_for('finish_batch') }}" method="post" style="margin: 0; display: inline;">
              <button type="submit" class="btn btn-new" style="padding: 6px 12px; font-size: 0.85rem;">Finish & Start New</button>
            </form>
            <a href="#" onclick="return confirmCancelBatch();" style="margin-left: 12px;">Cancel This Batch</a>
          </div>
        </div>

        <!-- Batch Notes -->
        <div class="scan-section" style="margin-bottom: 12px;">
          <form action="{{ url_for('save_batch_notes') }}" method="post">
            <label for="batch_notes"><strong>Batch Notes:</strong></label><br>
            <textarea name="notes" id="batch_notes" rows="2" style="width: 100%; max-width: 600px; padding: 8px; border: 1px solid #ccc; border-radius: 4px; font-family: inherit; font-size: 0.95rem; margin-top: 4px;">{{ current_batch.get('notes', '') }}</textarea>
            <br>
            <button type="submit" class="btn btn-new" style="margin-top: 8px;">Save Notes</button>
          </form>
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
            <button type="button" class="btn btn-new" onclick="window.location.reload()">Refresh</button>
            <button type="button" class="btn btn-new" onclick="saveBatch()">Save</button>
            {% if batch_status == 'notified' %}
              <form action="{{ url_for('notify_customers') }}" method="post" style="margin: 0;">
                <button type="submit" class="btn btn-new">Resend Notifications</button>
              </form>
            {% elif batch_status == 'recorded' %}
              <form action="{{ url_for('notify_customers') }}" method="post" style="margin: 0;">
                <button type="submit" class="btn btn-batch">✉ Notify Customers</button>
              </form>
            {% else %}
              <form action="{{ url_for('record_batch') }}" method="post" style="margin: 0;">
                <button type="submit" class="btn btn-batch">✓ Mark as Picked Up</button>
              </form>
            {% endif %}
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
                <th>Email</th>
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
                  <td style="font-size: 0.85rem; color: #666;">{{ row.customer_email or '—' }}</td>
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

    // ── Async scanning functionality ──
    {% if current_batch %}
    // Declare all DOM element references first
    const scanForm = document.getElementById('scan-form');
    const codeInput = document.getElementById('code');
    const scanBtn = document.getElementById('scan-btn');
    const scanSpinner = document.getElementById('scan-spinner');
    const scanStatus = document.getElementById('scan-status');
    const scansTable = document.getElementById('scans-tbody');
    const scanCount = document.getElementById('scan-count');
    const shopUrl = '{{ shop_url }}';

    // Initialize success sound
    const successSound = new Audio('{{ url_for("static", filename="scan-success.mp3") }}');
    successSound.volume = 0.5; // Set volume to 50%

    // ── Periodic focus restoration ──
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

    // ── Form submission handler ──

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
          // Play success sound
          try {
            successSound.currentTime = 0; // Reset to start
            successSound.play().catch(e => console.log('Could not play sound:', e));
          } catch (e) {
            console.log('Sound play error:', e);
          }

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
          // Don't play sound on errors (including carrier mismatch)
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
        <td style="font-size: 0.85rem; color: #666;">${scan.customer_email || '—'}</td>
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

    // ── Save batch ──
    function saveBatch() {
      // Just reload the page to save current state
      window.location.reload();
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
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Figtree:ital,wght@0,300..900;1,300..900&display=swap" rel="stylesheet">
  <style>
                                      * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100%;
      font-family: "Figtree", sans-serif;
      background-color: #fbfaf5; color: #333;
    }
    .container { display: flex; height: 100vh; }
    .sidebar {
      width: 240px; background: #fff; border-right: 1px solid #e0e0e0;
      display: flex; flex-direction: column; padding: 24px 16px;
    }
    .sidebar h1 { font-size: 1.25rem; font-weight: bold; margin-bottom: 16px; color: #2c3e50; }
    .sidebar ul { list-style: none; margin-top: 8px; }
    .sidebar li { margin-bottom: 8px; }
    .sidebar a {
      display: block;
      padding: 8px 12px;
      text-decoration: none;
      color: #534bc4;
      font-size: 1rem;
      font-weight: 500;
      border-radius: 4px;
      transition: background-color 0.2s;
    }
    .sidebar a:hover { background-color: #f0f0f0; }
    .sidebar .logout {
      display: block;
      margin-top: auto;
      padding: 8px 12px;
      color: #952746;
      font-size: 0.95rem;
      text-decoration: none;
      border-radius: 4px;
      transition: background-color 0.2s;
    }
    .sidebar .logout:hover { background-color: #fdecea; }
    .main-content { flex: 1; overflow-y: auto; padding: 24px; }
    .flash {
      padding: 10px 14px; margin-bottom: 16px; border-radius: 4px; font-weight: 500; border: 1px solid;
    }
    .flash.success { background-color: #e0f7e9; color: #199b76; border-color: #b2e6c2; }
    .flash.error   { background-color: #fdecea; color: #952746; border-color: #f5c6cb; }
    .flash.warning { background-color: #fff4e5; color: #8a6100; border-color: #ffe0b2; }
    h2 { font-size: 1.5rem; color: #2c3e50; margin-bottom: 16px; }
    table { width: 100%; border-collapse: collapse; margin-top: 12px; background: white; }
    th, td { border: 1px solid #ddd; padding: 10px 8px; font-size: 0.93rem; color: #34495e; }
    th { background-color: #eeeee5; text-align: left; font-weight: 600; }
    tr:nth-child(even) { background-color: #fbfaf5; }
    tr:hover { background-color: #f1f1f1; }
    .batch-link { color: #2d85f8; text-decoration: none; font-weight: 500; }
    .batch-link:hover { text-decoration: underline; }
    .btn-delete-small {
      padding: 4px 8px; font-size: 0.8rem; background-color: #952746; color: #fff;
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
        <li><a href="{{ url_for('new_batch') }}">New Batch</a></li>
        <li><a href="{{ url_for('all_batches') }}">Recorded Pick‐ups</a></li>
        <li><a href="{{ url_for('all_scans') }}">All Scans</a></li>
        <li><a href="{{ url_for('stuck_orders') }}">Fix Stuck Orders</a></li>
        <li><a href="{{ url_for('pick_and_pack') }}">Pick and Pack</a></li>
        <li><a href="{{ url_for('item_locations') }}">Item Locations</a></li>
        <li><a href="{{ url_for('check_shipments') }}">Check Shipments</a></li>
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
            <th>Status</th>
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
              <td>
                {% set batch_status = b.get('status', 'in_progress') %}
                {% if batch_status == 'notified' %}
                  <span style="color: #199b76; font-weight: 500;">✉ Notified</span>
                {% elif batch_status == 'recorded' %}
                  <span style="color: #f39c12; font-weight: 500;">✓ Picked Up</span>
                {% else %}
                  <span style="color: #666;">⏳ In Progress</span>
                {% endif %}
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
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Figtree:ital,wght@0,300..900;1,300..900&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100%;
      font-family: "Figtree", sans-serif;
      font-optical-sizing: auto;
      background-color: #fbfaf5; color: #333;
    }
    .container { display: flex; height: 100vh; }
    .sidebar {
      width: 240px; background: #fff; border-right: 1px solid #e0e0e0;
      display: flex; flex-direction: column; padding: 24px 16px;
    }
    .sidebar h1 { font-size: 1.25rem; font-weight: bold; margin-bottom: 16px; color: #2c3e50; }
    .sidebar ul { list-style: none; margin-top: 8px; }
    .sidebar li { margin-bottom: 8px; }
    .sidebar a {
      display: block;
      padding: 8px 12px;
      text-decoration: none;
      color: #534bc4;
      font-size: 1rem;
      font-weight: 500;
      border-radius: 4px;
      transition: background-color 0.2s;
    }
    .sidebar a:hover { background-color: #f0f0f0; }
    .sidebar .logout {
      display: block;
      margin-top: auto;
      padding: 8px 12px;
      color: #952746;
      font-size: 0.95rem;
      text-decoration: none;
      border-radius: 4px;
      transition: background-color 0.2s;
    }
    .sidebar .logout:hover { background-color: #fdecea; }
    .main-content { flex: 1; overflow-y: auto; padding: 24px; }
    .flash {
      padding: 10px 14px; margin-bottom: 16px; border-radius: 4px; font-weight: 500; border: 1px solid;
    }
    .flash.success { background-color: #e0f7e9; color: #199b76; border-color: #b2e6c2; }
    .flash.error   { background-color: #fdecea; color: #952746; border-color: #f5c6cb; }
    .flash.warning { background-color: #fff4e5; color: #8a6100; border-color: #ffe0b2; }
    .batch-header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; margin-bottom: 16px; }
    .batch-header h2 { font-size: 1.5rem; color: #2c3e50; }
    .batch-header .back-link { color: #2d85f8; text-decoration: none; font-size: 0.95rem; font-weight: 500; }
    .batch-header .back-link:hover { text-decoration: underline; }
    p.meta { color: #666; font-size: 0.9rem; margin-bottom: 16px; }
    h3 { color: #2c3e50; margin-top: 16px; margin-bottom: 8px; font-size: 1.25rem; }
    table { width: 100%; border-collapse: collapse; margin-top: 12px; background: white; }
    th, td { border: 1px solid #ddd; padding: 10px 8px; font-size: 0.93rem; color: #34495e; }
    th { background-color: #eeeee5; text-align: left; font-weight: 600; }
    tr:nth-child(even) { background-color: #fbfaf5; }
    tr:hover { background-color: #f1f1f1; }
    .duplicate-row { background-color: #fdecea !important; }
    td a { color: #534bc4; text-decoration: none; font-weight: 500; }
    td a:hover { text-decoration: underline; }
  </style>
</head>
<body>

  <div class="container">

    <div class="sidebar">
      <h1><img src="{{ url_for('static', filename='parcel-scan.jpg') }}" width="200"></h1>
      <ul>
        <li><a href="{{ url_for('new_batch') }}">New Batch</a></li>
        <li><a href="{{ url_for('all_batches') }}">Recorded Pick‐ups</a></li>
        <li><a href="{{ url_for('all_scans') }}">All Scans</a></li>
        <li><a href="{{ url_for('stuck_orders') }}">Fix Stuck Orders</a></li>
        <li><a href="{{ url_for('pick_and_pack') }}">Pick and Pack</a></li>
        <li><a href="{{ url_for('item_locations') }}">Item Locations</a></li>
        <li><a href="{{ url_for('check_shipments') }}">Check Shipments</a></li>
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
            <th>SS Batch</th>
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
              <td>{{ row.shipstation_batch_number or '' }}</td>
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

PICK_AND_PACK_TEMPLATE = r'''
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Pick and Pack – H&O Parcel Scans</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Figtree:ital,wght@0,300..900;1,300..900&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100%;
      font-family: "Figtree", sans-serif;
      font-optical-sizing: auto;
      background-color: #fbfaf5; color: #333;
    }
    .container { display: flex; height: 100vh; }
    .sidebar {
      width: 240px; background: #fff; border-right: 1px solid #e0e0e0;
      display: flex; flex-direction: column; padding: 24px 16px;
    }
    .sidebar h1 { font-size: 1.25rem; font-weight: bold; margin-bottom: 16px; color: #2c3e50; }
    .sidebar ul { list-style: none; margin-top: 8px; }
    .sidebar li { margin-bottom: 8px; }
    .sidebar a {
      display: block;
      padding: 8px 12px;
      text-decoration: none;
      color: #534bc4;
      font-size: 1rem;
      font-weight: 500;
      border-radius: 4px;
      transition: background-color 0.2s;
    }
    .sidebar a:hover { background-color: #f0f0f0; }
    .sidebar .logout {
      display: block;
      margin-top: auto;
      padding: 8px 12px;
      color: #952746;
      font-size: 0.95rem;
      text-decoration: none;
      border-radius: 4px;
      transition: background-color 0.2s;
    }
    .sidebar .logout:hover { background-color: #fdecea; }

    .main-content { flex: 1; overflow-y: auto; padding: 24px; }
    .flash { padding: 10px 14px; margin-bottom: 16px; border-radius: 4px; font-weight: 500; border: 1px solid; }
    .flash.success { background-color: #e0f7e9; color: #199b76; border-color: #b2e6c2; }
    .flash.error   { background-color: #fdecea; color: #952746; border-color: #f5c6cb; }
    .flash.warning { background-color: #fff4e5; color: #8a6100; border-color: #ffe0b2; }

    h2 { font-size: 1.5rem; color: #2c3e50; margin-bottom: 16px; }
    h3 { font-size: 1.2rem; color: #34495e; margin-bottom: 12px; margin-top: 20px; }

    .search-box {
      background: white; padding: 24px; border-radius: 8px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 24px;
    }
    .search-box input[type="text"] {
      padding: 10px 14px; font-size: 16px; width: 400px; border: 1px solid #ccc; border-radius: 4px;
    }
    .search-box button {
      padding: 10px 20px; font-size: 16px; border: none; border-radius: 4px;
      background-color: #2d85f8; color: #fff; cursor: pointer; margin-left: 8px;
    }
    .search-box button:hover { opacity: 0.92; }

    .order-card {
      background: white; padding: 24px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    }
    .order-header {
      background-color: #f8f9fa; padding: 16px; border-radius: 4px; margin-bottom: 20px;
    }
    .order-header p { margin: 6px 0; font-size: 0.95rem; }
    .order-header strong { color: #2c3e50; }

    .verification-notice {
      background-color: #fff4e5; border-left: 4px solid #f39c12;
      padding: 14px; margin-bottom: 20px; border-radius: 4px;
    }
    .verification-notice strong { color: #8a6100; }

    .scanner-box {
      background-color: #e8f4f8; border: 2px solid #3498db; padding: 16px;
      border-radius: 4px; margin-bottom: 20px;
    }
    .scanner-box label { font-weight: 600; color: #2c3e50; display: block; margin-bottom: 8px; }
    .scanner-box input[type="text"] {
      width: 100%; padding: 10px; font-size: 16px; border: 2px solid #3498db;
      border-radius: 4px; font-family: monospace;
    }
    .scan-feedback {
      margin-top: 10px; padding: 10px; border-radius: 4px; font-weight: 600; display: none;
    }
    .scan-feedback.success { background-color: #d4edda; color: #155724; display: block; }
    .scan-feedback.error { background-color: #f8d7da; color: #721c24; display: block; }

    .items-table { width: 100%; border-collapse: collapse; margin-top: 16px; }
    .items-table th { background-color: #f8f9fa; padding: 12px 8px; text-align: left;
                      border-bottom: 2px solid #dee2e6; font-weight: 600; color: #495057; }
    .items-table td { padding: 12px 8px; border-bottom: 1px solid #dee2e6; vertical-align: top; }
    .items-table tr:hover { background-color: #f8f9fa; }
    .items-table tr.matched { background-color: #d4edda; animation: highlight 0.5s ease; }
    @keyframes highlight {
      0% { background-color: #a3e4a0; }
      100% { background-color: #d4edda; }
    }
    .items-table input[type="checkbox"] { width: 20px; height: 20px; cursor: pointer; }
    .item-name { font-weight: 600; color: #2c3e50; display: block; margin-bottom: 4px; }
    .item-variant { color: #6c757d; font-size: 0.9rem; display: block; margin-bottom: 4px; }
    .item-properties {
      margin-top: 6px; padding: 6px; background-color: #f8f9fa;
      border-radius: 3px; font-size: 0.85rem; color: #555;
    }
    .qty-normal { color: #333; }
    .qty-red { color: #dc3545; font-weight: 700; }

    .verify-form { margin-top: 24px; }
    .verify-form textarea {
      width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 4px;
      font-family: inherit; font-size: 14px; margin-bottom: 16px; resize: vertical;
    }
    .verify-form button {
      padding: 12px 24px; font-size: 16px; border: none; border-radius: 4px;
      background-color: #199b76; color: #fff; cursor: pointer; font-weight: 600;
    }
    .verify-form button:hover { opacity: 0.92; }

    .error-box {
      background-color: #fdecea; border-left: 4px solid #952746;
      padding: 16px; margin-bottom: 20px; border-radius: 4px;
    }
    .error-box p { color: #a33a2f; font-weight: 500; }
    .error-box button {
      margin-top: 12px; padding: 8px 16px; font-size: 14px; border: none;
      border-radius: 4px; background-color: #952746; color: #fff; cursor: pointer;
    }
    .error-box button:hover { opacity: 0.92; }
  </style>
</head>
<body>

  <div class="container">

    <div class="sidebar">
      <h1><img src="{{ url_for('static', filename='parcel-scan.jpg') }}" width="200"></h1>
      <ul>
        <li><a href="{{ url_for('new_batch') }}">New Batch</a></li>
        <li><a href="{{ url_for('all_batches') }}">Recorded Pick‐ups</a></li>
        <li><a href="{{ url_for('all_scans') }}">All Scans</a></li>
        <li><a href="{{ url_for('stuck_orders') }}">Fix Stuck Orders</a></li>
        <li><a href="{{ url_for('pick_and_pack') }}">Pick and Pack</a></li>
        <li><a href="{{ url_for('item_locations') }}">Item Locations</a></li>
        <li><a href="{{ url_for('check_shipments') }}">Check Shipments</a></li>
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

      <h2>Pick and Pack - Order Verification</h2>

      <div class="search-box">
        <form method="post" action="{{ url_for('pick_and_pack') }}">
          <input type="hidden" name="action" value="search">
          <label for="identifier"><strong>Enter Tracking Number or Order Number:</strong></label><br><br>
          <input type="text" name="identifier" id="identifier" value="{{ search_identifier }}"
                 placeholder="1Z999AA10123456784 or 1234" autofocus required>
          <button type="submit">Search</button>
        </form>
      </div>

      {% if error_message %}
        <div class="error-box">
          <p>{{ error_message }}</p>
          <form method="post" action="{{ url_for('pick_and_pack') }}">
            <input type="hidden" name="action" value="search">
            <input type="hidden" name="identifier" value="{{ search_identifier }}">
            <button type="submit">Retry</button>
          </form>
        </div>
      {% endif %}

      {% if order_data %}
        <div class="order-card">
          <div class="order-header">
            <p><strong>Order Number:</strong> {{ order_data.order_name }}</p>
            <p><strong>Customer:</strong> {{ order_data.customer_name }}
               {% if order_data.customer_email %}({{ order_data.customer_email }}){% endif %}</p>
            {% if order_data.tracking_number %}
              <p><strong>Tracking:</strong> {{ order_data.tracking_number }}</p>
            {% endif %}
            <p><strong>Total Items:</strong> {{ order_data.total_items }}</p>
          </div>

          {% if already_verified %}
            <div class="verification-notice">
              <strong>⚠️ Already Verified:</strong> This order was verified on {{ already_verified.date }}
              ({{ already_verified.items_checked }}/{{ already_verified.total_items }} items checked).
              You can verify again to update the record.
            </div>
          {% endif %}

          <div class="scanner-box">
            <label for="barcode_scanner">📦 Scan Barcode / Enter SKU:</label>
            <input type="text" id="barcode_scanner" placeholder="Scan item barcode here..." autocomplete="off">
            <div id="scan_feedback" class="scan-feedback"></div>
          </div>

          <h3>Line Items - Check off each item as you pack:</h3>

          <form method="post" action="{{ url_for('pick_and_pack') }}" class="verify-form" id="verify_form">
            <input type="hidden" name="action" value="verify">
            <input type="hidden" name="order_number" value="{{ order_data.order_number }}">
            <input type="hidden" name="tracking_number" value="{{ order_data.tracking_number or '' }}">
            <input type="hidden" name="shopify_order_id" value="{{ order_data.shopify_order_id }}">
            <input type="hidden" name="total_items" value="{{ order_data.total_items }}">

            <table class="items-table">
              <thead>
                <tr>
                  <th style="width: 50px;">✓</th>
                  <th>Item Details</th>
                  <th style="width: 150px;">SKU</th>
                  <th style="width: 150px;">Location</th>
                  <th style="width: 80px; text-align: center;">Quantity</th>
                </tr>
              </thead>
              <tbody>
                {% for item in order_data.line_items %}
                  <tr id="row_{{ loop.index }}" data-sku="{{ item.sku }}">
                    <td>
                      <input type="checkbox" name="item_{{ loop.index }}" id="item_{{ loop.index }}" value="{{ item.id }}">
                    </td>
                    <td>
                      <label for="item_{{ loop.index }}" class="item-name">{{ item.name }}</label>
                      {% if item.variant_title %}
                        <span class="item-variant">{{ item.variant_title }}</span>
                      {% endif %}
                      {% if item.properties %}
                        <div class="item-properties">
                          {% for prop in item.properties %}
                            <div>{{ prop }}</div>
                          {% endfor %}
                        </div>
                      {% endif %}
                    </td>
                    <td style="font-family: monospace; font-size: 0.95rem;">{{ item.sku }}</td>
                    <td style="font-weight: 600; color: #2980b9;">
                      {% if item.location %}
                        📍 {{ item.location }}
                      {% else %}
                        <span style="color: #95a5a6;">—</span>
                      {% endif %}
                    </td>
                    <td style="text-align: center;">
                      <span class="{{ 'qty-red' if item.quantity > 1 else 'qty-normal' }}">{{ item.quantity }}</span>
                    </td>
                  </tr>
                {% endfor %}
              </tbody>
            </table>

            <label for="notes" style="margin-top: 24px; display: block;"><strong>Notes (optional):</strong></label>
            <textarea name="notes" id="notes" rows="3" placeholder="Add any notes about this verification..."></textarea>

            <button type="submit">✅ Verify Order</button>
          </form>

          <script>
            // Barcode scanner logic
            const barcodeInput = document.getElementById('barcode_scanner');
            const feedbackDiv = document.getElementById('scan_feedback');
            const allRows = document.querySelectorAll('.items-table tbody tr');

            // Focus on barcode input when page loads
            barcodeInput.focus();

            barcodeInput.addEventListener('keypress', function(e) {
              if (e.key === 'Enter') {
                e.preventDefault();
                const scannedSku = this.value.trim().toUpperCase();

                if (!scannedSku) {
                  return;
                }

                // Find matching SKU
                let found = false;
                allRows.forEach(row => {
                  const rowSku = row.dataset.sku.toUpperCase();
                  if (rowSku === scannedSku) {
                    found = true;

                    // Get the checkbox for this row
                    const checkbox = row.querySelector('input[type="checkbox"]');

                    // Check the checkbox
                    checkbox.checked = true;

                    // Add matched class for visual feedback
                    row.classList.add('matched');
                    setTimeout(() => row.classList.remove('matched'), 2000);

                    // Show success feedback
                    feedbackDiv.className = 'scan-feedback success';
                    feedbackDiv.textContent = '✓ Match found! Item checked.';

                    // Scroll row into view
                    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
                  }
                });

                if (!found) {
                  // Show error feedback
                  feedbackDiv.className = 'scan-feedback error';
                  feedbackDiv.textContent = '✗ Error: Wrong item. Please double-check the SKU.';

                  // Play error sound if available
                  try {
                    const audio = new Audio('data:audio/wav;base64,UklGRnoGAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQoGAACBhYqFbF1fdJivrJBhNjVgodDbq2EcBj+a2/LDciUFLIHO8tiJNwgZaLvt559NEAxQp+PwtmMcBjiR1/LMeSwFJHfH8N2QQAoUXrTp66hVFApGn+DyvmwhBjiR1/LMeSwFJHfH8N2QQAoUXrTp66hVFApGn+DyvmwhBjiR1/LMeSwFJHfH8N2QQAoUXrTp66hVFApGn+DyvmwhBjiR1/LMeSwFJHfH8N2QQAoUXrTp66hVFApGn+DyvmwhBjiR1/LMeSwFJHfH8N2QQAoUXrTp66hVFApGn+DyvmwhBjiR1/LMeSwFJHfH8N2QQAoUXrTp66hVFApGn+Dy');
                  } catch(e) {}
                }

                // Clear input and refocus
                this.value = '';
                setTimeout(() => {
                  feedbackDiv.className = 'scan-feedback';
                  feedbackDiv.textContent = '';
                  this.focus();
                }, 2000);
              }
            });

            // Keep focus on barcode scanner
            document.addEventListener('click', function(e) {
              if (e.target.type !== 'checkbox' && e.target.type !== 'submit' && e.target.type !== 'textarea') {
                barcodeInput.focus();
              }
            });
          </script>
        </div>
      {% endif %}

    </div>

  </div>

</body>
</html>
'''

ITEM_LOCATIONS_TEMPLATE = r'''
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Item Locations – H&O Parcel Scans</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Figtree:ital,wght@0,300..900;1,300..900&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100%;
      font-family: "Figtree", sans-serif;
      font-optical-sizing: auto;
      background-color: #fbfaf5; color: #333;
    }
    .container { display: flex; height: 100vh; }
    .sidebar {
      width: 240px; background: #fff; border-right: 1px solid #e0e0e0;
      display: flex; flex-direction: column; padding: 24px 16px;
    }
    .sidebar h1 { font-size: 1.25rem; font-weight: bold; margin-bottom: 16px; color: #2c3e50; }
    .sidebar ul { list-style: none; margin-top: 8px; }
    .sidebar li { margin-bottom: 8px; }
    .sidebar a {
      display: block;
      padding: 8px 12px;
      text-decoration: none;
      color: #534bc4;
      font-size: 1rem;
      font-weight: 500;
      border-radius: 4px;
      transition: background-color 0.2s;
    }
    .sidebar a:hover { background-color: #f0f0f0; }
    .sidebar .logout {
      display: block;
      margin-top: auto;
      padding: 8px 12px;
      color: #952746;
      font-size: 0.95rem;
      text-decoration: none;
      border-radius: 4px;
      transition: background-color 0.2s;
    }
    .sidebar .logout:hover { background-color: #fdecea; }

    .main-content { flex: 1; overflow-y: auto; padding: 24px; }
    .flash { padding: 10px 14px; margin-bottom: 16px; border-radius: 4px; font-weight: 500; border: 1px solid; }
    .flash.success { background-color: #e0f7e9; color: #199b76; border-color: #b2e6c2; }
    .flash.error   { background-color: #fdecea; color: #952746; border-color: #f5c6cb; }

    h2 { font-size: 1.5rem; color: #2c3e50; margin-bottom: 16px; }

    .add-form {
      background: white; padding: 24px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);
      margin-bottom: 24px;
    }
    .add-form h3 { font-size: 1.2rem; color: #34495e; margin-bottom: 16px; }
    .form-row { display: flex; gap: 12px; margin-bottom: 16px; align-items: end; }
    .form-group { flex: 1; }
    .form-group label { display: block; font-weight: 600; margin-bottom: 6px; color: #2c3e50; font-size: 0.9rem; }
    .form-group input, .form-group select {
      width: 100%; padding: 10px; font-size: 14px; border: 1px solid #ccc; border-radius: 4px;
    }
    .form-group.narrow { flex: 0 0 150px; }
    .add-btn {
      padding: 10px 24px; font-size: 14px; border: none; border-radius: 4px;
      background-color: #199b76; color: #fff; cursor: pointer; font-weight: 600;
    }
    .add-btn:hover { opacity: 0.92; }

    .rules-table { width: 100%; border-collapse: collapse; background: white; }
    .rules-table th, .rules-table td { border: 1px solid #ddd; padding: 12px 10px; font-size: 0.93rem; }
    .rules-table th { background-color: #f8f9fa; text-align: left; font-weight: 600; color: #495057; }
    .rules-table tr:nth-child(even) { background-color: #fafafa; }
    .rules-table tr:hover { background-color: #f1f1f1; }
    .rule-type-badge {
      display: inline-block; padding: 4px 8px; border-radius: 3px; font-size: 0.8rem;
      font-weight: 600; text-transform: uppercase;
    }
    .rule-type-sku { background-color: #d4edda; color: #155724; }
    .rule-type-keyword { background-color: #cce5ff; color: #004085; }
    .delete-btn {
      padding: 6px 12px; font-size: 0.85rem; background-color: #952746; color: #fff;
      border: none; border-radius: 4px; cursor: pointer;
    }
    .delete-btn:hover { opacity: 0.92; }
  </style>
</head>
<body>

  <div class="container">

    <div class="sidebar">
      <h1><img src="{{ url_for('static', filename='parcel-scan.jpg') }}" width="200"></h1>
      <ul>
        <li><a href="{{ url_for('new_batch') }}">New Batch</a></li>
        <li><a href="{{ url_for('all_batches') }}">Recorded Pick‐ups</a></li>
        <li><a href="{{ url_for('all_scans') }}">All Scans</a></li>
        <li><a href="{{ url_for('stuck_orders') }}">Fix Stuck Orders</a></li>
        <li><a href="{{ url_for('pick_and_pack') }}">Pick and Pack</a></li>
        <li><a href="{{ url_for('item_locations') }}">Item Locations</a></li>
        <li><a href="{{ url_for('check_shipments') }}">Check Shipments</a></li>
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

      <h2>Item Location Rules</h2>
      <p style="margin-bottom: 20px; color: #666;">
        Set warehouse locations for items by matching SKUs or keywords. These locations will appear in the Pick and Pack page.
      </p>

      <div class="add-form">
        <h3>Add New Location Rule</h3>
        <form method="post" action="{{ url_for('add_location_rule') }}">
          <div class="form-row">
            <div class="form-group narrow">
              <label for="aisle">Aisle</label>
              <input type="text" name="aisle" id="aisle" required placeholder="A1">
            </div>
            <div class="form-group narrow">
              <label for="shelf">Shelf</label>
              <input type="text" name="shelf" id="shelf" required placeholder="B3">
            </div>
            <div class="form-group narrow">
              <label for="rule_type">Match By</label>
              <select name="rule_type" id="rule_type" required>
                <option value="sku">SKU</option>
                <option value="keyword">Keyword</option>
              </select>
            </div>
            <div class="form-group">
              <label for="rule_value">Value</label>
              <input type="text" name="rule_value" id="rule_value" required placeholder="SKU-12345 or 'Bracelet'">
            </div>
            <div class="form-group" style="flex: 0;">
              <button type="submit" class="add-btn">+ Add Rule</button>
            </div>
          </div>
        </form>
      </div>

      <table class="rules-table">
        <thead>
          <tr>
            <th>Aisle</th>
            <th>Shelf</th>
            <th>Rule Type</th>
            <th>Match Value</th>
            <th>Created</th>
            <th style="width: 100px;">Actions</th>
          </tr>
        </thead>
        <tbody>
          {% if rules %}
            {% for rule in rules %}
              <tr>
                <td><strong>{{ rule.aisle }}</strong></td>
                <td><strong>{{ rule.shelf }}</strong></td>
                <td>
                  <span class="rule-type-badge rule-type-{{ rule.rule_type }}">
                    {{ rule.rule_type }}
                  </span>
                </td>
                <td style="font-family: monospace;">{{ rule.rule_value }}</td>
                <td>{{ rule.created_at.strftime('%Y-%m-%d %H:%M') if rule.created_at else '—' }}</td>
                <td>
                  <form method="post" action="{{ url_for('delete_location_rule') }}" style="display: inline;">
                    <input type="hidden" name="rule_id" value="{{ rule.id }}">
                    <button type="submit" class="delete-btn" onclick="return confirm('Delete this rule?')">Delete</button>
                  </form>
                </td>
              </tr>
            {% endfor %}
          {% else %}
            <tr>
              <td colspan="6" style="text-align: center; padding: 32px; color: #999;">
                No location rules configured yet. Add your first rule above!
              </td>
            </tr>
          {% endif %}
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
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Figtree:ital,wght@0,300..900;1,300..900&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100%;
      font-family: "Figtree", sans-serif;
      font-optical-sizing: auto;
      background-color: #fbfaf5; color: #333;
    }
    .container { display: flex; height: 100vh; }
    .sidebar {
      width: 240px; background: #fff; border-right: 1px solid #e0e0e0;
      display: flex; flex-direction: column; padding: 24px 16px;
    }
    .sidebar h1 { font-size: 1.25rem; font-weight: bold; margin-bottom: 16px; color: #2c3e50; }
    .sidebar ul { list-style: none; margin-top: 8px; }
    .sidebar li { margin-bottom: 8px; }
    .sidebar a {
      display: block;
      padding: 8px 12px;
      text-decoration: none;
      color: #534bc4;
      font-size: 1rem;
      font-weight: 500;
      border-radius: 4px;
      transition: background-color 0.2s;
    }
    .sidebar a:hover { background-color: #f0f0f0; }
    .sidebar .logout {
      display: block;
      margin-top: auto;
      padding: 8px 12px;
      color: #952746;
      font-size: 0.95rem;
      text-decoration: none;
      border-radius: 4px;
      transition: background-color 0.2s;
    }
    .sidebar .logout:hover { background-color: #fdecea; }

    .main-content { flex: 1; overflow-y: auto; padding: 24px; }
    .flash { padding: 10px 14px; margin-bottom: 16px; border-radius: 4px; font-weight: 500; border: 1px solid; }
    .flash.success { background-color: #e0f7e9; color: #199b76; border-color: #b2e6c2; }
    .flash.error   { background-color: #fdecea; color: #952746; border-color: #f5c6cb; }
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
    th { background-color: #eeeee5; text-align: left; font-weight: 600; }
    tr:nth-child(even) { background-color: #fbfaf5; }
    tr:hover { background-color: #f1f1f1; }
    .duplicate-row { background-color: #fdecea !important; }
    td a { color: #534bc4; text-decoration: none; font-weight: 500; }
    td a:hover { text-decoration: underline; }
    .btn-delete-small {
      padding: 4px 8px; font-size: 0.8rem; background-color: #952746; color: #fff; border: none; border-radius: 4px; cursor: pointer;
    }
    .btn-delete-small:hover { opacity: 0.92; }
  </style>
</head>
<body>

  <div class="container">

    <div class="sidebar">
      <h1><img src="{{ url_for('static', filename='parcel-scan.jpg') }}" width="200"></h1>
      <ul>
        <li><a href="{{ url_for('new_batch') }}">New Batch</a></li>
        <li><a href="{{ url_for('all_batches') }}">Recorded Pick‐ups</a></li>
        <li><a href="{{ url_for('all_scans') }}">All Scans</a></li>
        <li><a href="{{ url_for('stuck_orders') }}">Fix Stuck Orders</a></li>
        <li><a href="{{ url_for('pick_and_pack') }}">Pick and Pack</a></li>
        <li><a href="{{ url_for('item_locations') }}">Item Locations</a></li>
        <li><a href="{{ url_for('check_shipments') }}">Check Shipments</a></li>
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
            <th>SS Batch</th>
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
              <td>{{ s.shipstation_batch_number or '' }}</td>
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
                {% if s.order_number in ['Processing...', 'N/A'] or s.customer_name in ['Looking up...', 'Not Found', 'No Order Found'] %}
                  <form action="{{ url_for('retry_fetch_scan') }}" method="post" style="display: inline; margin-right: 4px;">
                    <input type="hidden" name="scan_id" value="{{ s.id }}">
                    <button type="submit" class="btn-delete-small" style="background-color: #3498db;">Retry</button>
                  </form>
                {% endif %}
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

      <!-- Pagination Controls -->
      {% if total_pages > 1 %}
        <div style="margin-top: 24px; text-align: center;">
          <p style="margin-bottom: 12px; color: #666;">
            Showing page {{ page }} of {{ total_pages }} ({{ total_scans }} total scans)
          </p>
          <div style="display: inline-flex; gap: 8px; align-items: center;">
            {% if page > 1 %}
              <a href="{{ url_for('all_scans', page=page-1, order_number=order_search) }}"
                 style="padding: 8px 16px; background: #2d85f8; color: white; text-decoration: none; border-radius: 4px; font-size: 14px;">
                ← Previous
              </a>
            {% else %}
              <span style="padding: 8px 16px; background: #ccc; color: #666; border-radius: 4px; font-size: 14px;">
                ← Previous
              </span>
            {% endif %}

            <span style="color: #666; font-size: 14px;">Page {{ page }} of {{ total_pages }}</span>

            {% if page < total_pages %}
              <a href="{{ url_for('all_scans', page=page+1, order_number=order_search) }}"
                 style="padding: 8px 16px; background: #2d85f8; color: white; text-decoration: none; border-radius: 4px; font-size: 14px;">
                Next →
              </a>
            {% else %}
              <span style="padding: 8px 16px; background: #ccc; color: #666; border-radius: 4px; font-size: 14px;">
                Next →
              </span>
            {% endif %}
          </div>
        </div>
      {% endif %}

    </div>

  </div>

</body>
</html>
'''


STUCK_ORDERS_TEMPLATE = r'''
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Fix Stuck Orders – H&O Parcel Scans</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Figtree:ital,wght@0,300..900;1,300..900&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100%;
      font-family: "Figtree", sans-serif;
      font-optical-sizing: auto;
      background-color: #fbfaf5; color: #333;
    }
    .container { display: flex; height: 100vh; }
    .sidebar {
      width: 240px; background: #fff; border-right: 1px solid #e0e0e0;
      display: flex; flex-direction: column; padding: 24px 16px;
    }
    .sidebar h1 { font-size: 1.25rem; font-weight: bold; margin-bottom: 16px; color: #2c3e50; }
    .sidebar ul { list-style: none; margin-top: 8px; }
    .sidebar li { margin-bottom: 8px; }
    .sidebar a {
      display: block;
      padding: 8px 12px;
      text-decoration: none;
      color: #534bc4;
      font-size: 1rem;
      font-weight: 500;
      border-radius: 4px;
      transition: background-color 0.2s;
    }
    .sidebar a:hover { background-color: #f0f0f0; }
    .sidebar .logout {
      display: block;
      margin-top: auto;
      padding: 8px 12px;
      color: #952746;
      font-size: 0.95rem;
      text-decoration: none;
      border-radius: 4px;
      transition: background-color 0.2s;
    }
    .sidebar .logout:hover { background-color: #fdecea; }

    .main-content { flex: 1; overflow-y: auto; padding: 24px; }
    .flash { padding: 10px 14px; margin-bottom: 16px; border-radius: 4px; font-weight: 500; border: 1px solid; }
    .flash.success { background-color: #e0f7e9; color: #199b76; border-color: #b2e6c2; }
    .flash.error   { background-color: #fdecea; color: #952746; border-color: #f5c6cb; }
    .flash.warning { background-color: #fff4e5; color: #8a6100; border-color: #ffe0b2; }

    h2 { font-size: 1.5rem; color: #2c3e50; margin-bottom: 16px; }
    .info-box { background: #e3f2fd; padding: 12px 16px; border-radius: 6px; margin-bottom: 20px; border-left: 4px solid #2196f3; }
    .info-box p { margin: 4px 0; font-size: 0.95rem; color: #1565c0; }

    table { width: 100%; border-collapse: collapse; margin-top: 12px; background: white; }
    th, td { border: 1px solid #ddd; padding: 10px 8px; font-size: 0.93rem; color: #34495e; }
    th { background-color: #eeeee5; text-align: left; font-weight: 600; }
    tr:nth-child(even) { background-color: #fbfaf5; }
    tr:hover { background-color: #f1f1f1; }
    .stuck-row { background-color: #fff3cd !important; }
    td a { color: #534bc4; text-decoration: none; font-weight: 500; }
    td a:hover { text-decoration: underline; }

    .btn-fix {
      padding: 6px 14px; font-size: 0.85rem; background-color: #28a745; color: #fff;
      border: none; border-radius: 4px; cursor: pointer; font-weight: 500;
    }
    .btn-fix:hover { opacity: 0.92; }
    .btn-fix:disabled { background-color: #ccc; cursor: not-allowed; }

    .fixing { opacity: 0.6; }
    .status-processing { color: #ff6b6b; font-weight: 600; }
    .status-error { color: #dc3545; font-weight: 600; }

    .empty-state {
      text-align: center; padding: 60px 20px; background: white; border-radius: 8px; margin-top: 20px;
    }
    .empty-state h3 { color: #28a745; font-size: 1.3rem; margin-bottom: 10px; }
    .empty-state p { color: #666; font-size: 1rem; }
  </style>
</head>
<body>

  <div class="container">

    <div class="sidebar">
      <h1><img src="{{ url_for('static', filename='parcel-scan.jpg') }}" width="200"></h1>
      <ul>
        <li><a href="{{ url_for('new_batch') }}">New Batch</a></li>
        <li><a href="{{ url_for('all_batches') }}">Recorded Pick‐ups</a></li>
        <li><a href="{{ url_for('all_scans') }}">All Scans</a></li>
        <li><a href="{{ url_for('stuck_orders') }}">Fix Stuck Orders</a></li>
        <li><a href="{{ url_for('pick_and_pack') }}">Pick and Pack</a></li>
        <li><a href="{{ url_for('item_locations') }}">Item Locations</a></li>
        <li><a href="{{ url_for('check_shipments') }}">Check Shipments</a></li>
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

      <h2>Fix Stuck Orders</h2>

      <div class="info-box">
        <p><strong>What are stuck orders?</strong></p>
        <p>These are scans where customer information couldn't be retrieved from Shopify/ShipStation.</p>
        <p>Click the "Fix" button to retry fetching the order details.</p>
      </div>

      {% if stuck_scans|length == 0 %}
        <div class="empty-state">
          <h3>✓ All Clear!</h3>
          <p>No stuck orders found. All scans have customer information.</p>
        </div>
      {% else %}
        <div style="margin-bottom: 16px;">
          <button class="btn-fix" onclick="fixAllOrders()" id="fix-all-btn" style="font-size: 1rem; padding: 10px 20px;">
            🔧 Fix All ({{ stuck_scans|length }} orders)
          </button>
          <span id="fix-all-status" style="margin-left: 12px; font-weight: 500;"></span>
        </div>
        <table>
          <thead>
            <tr>
              <th>Tracking #</th>
              <th>Carrier</th>
              <th>SS Batch</th>
              <th>Order #</th>
              <th>Customer</th>
              <th>Scan Date</th>
              <th>Status</th>
              <th>Batch ID</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {% for s in stuck_scans %}
              <tr class="stuck-row" id="row-{{ s.id }}">
                <td id="tracking-{{ s.id }}">{{ s.tracking_number }}</td>
                <td id="carrier-{{ s.id }}">{{ s.carrier }}</td>
                <td id="ss-batch-{{ s.id }}">{{ s.shipstation_batch_number or '' }}</td>
                <td id="order-{{ s.id }}">
                  <span class="{{ 'status-processing' if s.order_number == 'Processing...' else '' }}">
                    {{ s.order_number }}
                  </span>
                </td>
                <td id="customer-{{ s.id }}">
                  <span class="{{ 'status-processing' if s.customer_name in ['Looking up...', 'No Order Found'] else 'status-error' if s.customer_name.startswith('Error:') else '' }}">
                    {{ s.customer_name }}
                  </span>
                </td>
                <td>{{ s.scan_date }}</td>
                <td id="status-{{ s.id }}">{{ s.status }}</td>
                <td>
                  {% if s.batch_id %}
                    <a href="{{ url_for('view_batch', batch_id=s.batch_id) }}">{{ s.batch_id }}</a>
                  {% endif %}
                </td>
                <td>
                  <button class="btn-fix" onclick="fixOrder({{ s.id }}, '{{ s.tracking_number }}', '{{ s.carrier }}')" id="btn-{{ s.id }}">
                    Fix
                  </button>
                </td>
              </tr>
            {% endfor %}
          </tbody>
        </table>
      {% endif %}

    </div>

  </div>

  <script>
    async function fixOrder(scanId, trackingNumber, carrier) {
      const btn = document.getElementById('btn-' + scanId);
      const row = document.getElementById('row-' + scanId);
      const orderCell = document.getElementById('order-' + scanId);
      const customerCell = document.getElementById('customer-' + scanId);
      const statusCell = document.getElementById('status-' + scanId);

      // Disable button and show loading state
      btn.disabled = true;
      btn.textContent = 'Fixing...';
      row.classList.add('fixing');

      try {
        const response = await fetch(`/api/fix_order/${scanId}`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            tracking_number: trackingNumber,
            carrier: carrier
          })
        });

        const data = await response.json();

        if (data.success) {
          // Update the table row with new data
          orderCell.innerHTML = data.scan.order_number || 'N/A';
          customerCell.innerHTML = data.scan.customer_name || 'Not Found';
          statusCell.innerHTML = data.scan.status || 'Complete';

          // Remove stuck styling if order was found
          if (data.scan.order_number !== 'N/A' && data.scan.customer_name !== 'Not Found') {
            row.classList.remove('stuck-row');
            row.style.backgroundColor = '#d4edda';
            btn.textContent = 'Fixed ✓';
            btn.style.backgroundColor = '#155724';

            // Remove row after 2 seconds
            setTimeout(() => {
              row.style.transition = 'opacity 0.5s';
              row.style.opacity = '0';
              setTimeout(() => row.remove(), 500);
            }, 2000);
          } else {
            // Still not found
            btn.disabled = false;
            btn.textContent = 'Retry';
            row.classList.remove('fixing');
            alert('Order information still not found. The order may not exist in Shopify/ShipStation.');
          }
        } else {
          throw new Error(data.message || 'Failed to fix order');
        }
      } catch (error) {
        console.error('Error fixing order:', error);
        alert('Error: ' + error.message);
        btn.disabled = false;
        btn.textContent = 'Fix';
        row.classList.remove('fixing');
      }
    }

    async function fixAllOrders() {
      const fixAllBtn = document.getElementById('fix-all-btn');
      const statusSpan = document.getElementById('fix-all-status');
      const rows = document.querySelectorAll('.stuck-row');

      if (!confirm('Fix all stuck orders? This will attempt to fetch data for all ' + rows.length + ' orders.')) {
        return;
      }

      fixAllBtn.disabled = true;
      let fixed = 0;
      let errors = 0;

      for (let i = 0; i < rows.length; i++) {
        const row = rows[i];
        const scanId = row.id.replace('row-', '');
        const tracking = document.getElementById('tracking-' + scanId).textContent;
        const carrier = document.getElementById('carrier-' + scanId).textContent;

        statusSpan.textContent = `Processing ${i + 1}/${rows.length}...`;

        try {
          const response = await fetch('/api/fix_order/' + scanId, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tracking_number: tracking, carrier: carrier })
          });

          const result = await response.json();

          if (result.success) {
            row.style.backgroundColor = '#e0f7e9';
            fixed++;
          } else {
            errors++;
          }
        } catch (error) {
          errors++;
        }

        await new Promise(resolve => setTimeout(resolve, 200)); // Small delay between requests
      }

      statusSpan.textContent = `✅ Done! Fixed ${fixed}, Errors ${errors}`;

      if (fixed > 0) {
        setTimeout(() => {
          window.location.reload();
        }, 2000);
      }

      fixAllBtn.disabled = false;
    }
  </script>

</body>
</html>
'''

CHECK_SHIPMENTS_TEMPLATE = r'''
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Check Shipments – H&O Parcel Scans</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Figtree:ital,wght@0,300..900;1,300..900&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100%;
      font-family: "Figtree", sans-serif;
      font-optical-sizing: auto;
      background-color: #fbfaf5;
      color: #333;
    }
    .container { display: flex; height: 100vh; }
    .sidebar {
      width: 240px; background: #fff; border-right: 1px solid #e0e0e0;
      display: flex; flex-direction: column; padding: 24px 16px;
    }
    .sidebar h1 { font-size: 1.25rem; font-weight: bold; margin-bottom: 16px; color: #2c3e50; }
    .sidebar ul { list-style: none; margin-top: 8px; }
    .sidebar li { margin-bottom: 8px; }
    .sidebar a {
      display: block;
      padding: 8px 12px;
      text-decoration: none;
      color: #534bc4;
      font-size: 1rem;
      font-weight: 500;
      border-radius: 4px;
      transition: background-color 0.2s;
    }
    .sidebar a:hover { background-color: #f0f0f0; }
    .sidebar .logout {
      display: block;
      margin-top: auto;
      padding: 8px 12px;
      color: #952746;
      font-size: 0.95rem;
      text-decoration: none;
      border-radius: 4px;
      transition: background-color 0.2s;
    }
    .sidebar .logout:hover { background-color: #fdecea; }

    .main-content { flex: 1; overflow-y: auto; padding: 24px; }
    .flash {
      padding: 10px 14px; margin-bottom: 16px; border-radius: 4px; font-weight: 500; border: 1px solid;
    }
    .flash.success { background-color: #e0f7e9; color: #199b76; border-color: #b2e6c2; }
    .flash.error   { background-color: #fdecea; color: #952746; border-color: #f5c6cb; }
    .flash.warning { background-color: #fff4e5; color: #8a6100; border-color: #ffe0b2; }

    h2 { font-size: 1.5rem; color: #2c3e50; margin-bottom: 16px; }

    .search-box {
      background: white; padding: 20px; border-radius: 8px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 20px;
    }
    .search-box input[type="text"] {
      width: 400px; padding: 8px 12px; border: 1px solid #ccc; border-radius: 4px;
      margin-right: 8px; font-size: 0.95rem;
    }
    .btn {
      padding: 8px 16px; font-size: 0.9rem; border: none; border-radius: 4px;
      cursor: pointer; transition: all 0.2s;
    }
    .btn-search { background-color: #534bc4; color: white; }
    .btn-search:hover { opacity: 0.92; }

    table { width: 100%; border-collapse: collapse; margin-top: 12px; background: white; }
    th, td { border: 1px solid #ddd; padding: 10px 8px; font-size: 0.93rem; color: #34495e; }
    th { background-color: #eeeee5; text-align: left; font-weight: 600; }
    tr:nth-child(even) { background-color: #fbfaf5; }
    tr:hover { background-color: #f1f1f1; }

    .status-badge {
      display: inline-block; padding: 4px 8px; border-radius: 4px;
      font-size: 0.85rem; font-weight: 500;
    }
    .status-label-created { background-color: #e3f2fd; color: #1976d2; }
    .status-in-transit { background-color: #fff4e5; color: #8a6100; }
    .status-delivered { background-color: #e0f7e9; color: #199b76; }
    .status-exception { background-color: #fdecea; color: #952746; }
    .status-unknown { background-color: #f5f5f5; color: #666; }
    .status-error { background-color: #fdecea; color: #952746; }

    .cancelled-row {
      background-color: #fdecea !important;
      opacity: 0.7;
    }
    .btn-cancel {
      padding: 4px 8px; font-size: 0.8rem; border-radius: 4px;
      border: 1px solid #952746; background: white; color: #952746;
      cursor: pointer; transition: all 0.2s;
    }
    .btn-cancel:hover {
      background-color: #952746; color: white;
    }
    .btn-uncancel {
      padding: 4px 8px; font-size: 0.8rem; border-radius: 4px;
      border: 1px solid #199b76; background: white; color: #199b76;
      cursor: pointer; transition: all 0.2s;
    }
    .btn-uncancel:hover {
      background-color: #199b76; color: white;
    }

    .flag-critical {
      color: #952746; font-weight: 700; font-size: 1.3rem;
      animation: pulse 2s ease-in-out infinite;
    }
    .flag-warning {
      color: #e67e00; font-weight: 600; font-size: 1.2rem;
    }
    .flag-ok {
      color: #199b76; font-weight: 600; font-size: 1.2rem;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.6; }
    }

    .pagination {
      margin-top: 20px; display: flex; gap: 8px; align-items: center;
      justify-content: center;
    }
    .pagination button {
      padding: 6px 12px; border: 1px solid #534bc4; background: white;
      color: #534bc4; border-radius: 4px; cursor: pointer;
    }
    .pagination button:hover { background-color: #534bc4; color: white; }
    .pagination button:disabled { opacity: 0.5; cursor: not-allowed; }

    .loading {
      text-align: center; padding: 40px; color: #666;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="sidebar">
      <h1><img src="{{ url_for('static', filename='parcel-scan.jpg') }}" width="200"></h1>
      <ul>
        <li><a href="{{ url_for('new_batch') }}">New Batch</a></li>
        <li><a href="{{ url_for('all_batches') }}">Recorded Pick‐ups</a></li>
        <li><a href="{{ url_for('all_scans') }}">All Scans</a></li>
        <li><a href="{{ url_for('stuck_orders') }}">Fix Stuck Orders</a></li>
        <li><a href="{{ url_for('pick_and_pack') }}">Pick and Pack</a></li>
        <li><a href="{{ url_for('item_locations') }}">Item Locations</a></li>
        <li><a href="{{ url_for('check_shipments') }}" style="font-weight: 700;">Check Shipments</a></li>
      </ul>
      <a href="{{ url_for('logout') }}" class="logout">Log Out</a>
    </div>

    <div class="main-content">
      <h2>📦 Check Shipments</h2>
      <p style="margin-bottom: 16px; color: #666;">
        Track shipments from ShipStation and UPS. Shows label status, scanning status, and delivery tracking.
      </p>

      {% with messages = get_flashed_messages(with_categories=true) %}
        {% for category, msg in messages %}
          <div class="flash {{ category }}">{{ msg }}</div>
        {% endfor %}
      {% endwith %}

      <div class="search-box">
        <form method="get" action="{{ url_for('check_shipments') }}">
          <input type="text" name="search" placeholder="Search by customer name, order #, or tracking #..." value="{{ search_query }}" autofocus>
          <button type="submit" class="btn btn-search">🔍 Search</button>
          {% if search_query %}
            <a href="{{ url_for('check_shipments') }}" style="margin-left: 8px; color: #534bc4;">Clear</a>
          {% endif %}
        </form>
      </div>

      {% if loading %}
        <div class="loading">
          <p>⏳ Loading shipments from ShipStation...</p>
        </div>
      {% elif shipments %}
        <table>
          <thead>
            <tr>
              <th>Flag</th>
              <th>Order #</th>
              <th>Customer</th>
              <th>Tracking #</th>
              <th>Carrier</th>
              <th>Ship Date</th>
              <th>Scanned?</th>
              <th>UPS Status</th>
              <th>Last Activity</th>
              <th>Cancelled?</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {% for ship in shipments %}
              <tr {% if ship.is_cancelled %}class="cancelled-row"{% endif %}>
                <td>
                  {% if ship.is_cancelled %}
                    <span style="color: #952746; font-size: 1.3rem;" title="ORDER CANCELLED">🚫</span>
                  {% elif ship.flag %}
                    {% if ship.flag_severity == 'critical' %}
                      <span class="flag-critical" title="{{ ship.flag_reason }}">🚨</span>
                    {% else %}
                      <span class="flag-warning" title="{{ ship.flag_reason }}">⚠️</span>
                    {% endif %}
                  {% else %}
                    <span class="flag-ok">✓</span>
                  {% endif %}
                </td>
                <td>{{ ship.order_number }}</td>
                <td>{{ ship.customer_name }}</td>
                <td style="font-family: monospace;">{{ ship.tracking_number }}</td>
                <td>{{ ship.carrier }}</td>
                <td>{{ ship.ship_date }}</td>
                <td>
                  {% if ship.scanned %}
                    <span style="color: #199b76;">✓ {{ ship.scan_date }}</span>
                  {% else %}
                    <span style="color: #666;">—</span>
                  {% endif %}
                </td>
                <td>
                  <span class="status-badge status-{{ ship.ups_status }}">
                    {{ ship.ups_status_text }}
                  </span>
                </td>
                <td style="font-size: 0.85rem;">{{ ship.ups_last_activity }}</td>
                <td>
                  {% if ship.is_cancelled %}
                    <span style="color: #952746; font-weight: 600;">🚫 CANCELLED</span>
                    <div style="font-size: 0.8rem; color: #666; margin-top: 4px;">{{ ship.cancel_reason }}</div>
                  {% else %}
                    <span style="color: #666;">—</span>
                  {% endif %}
                </td>
                <td>
                  {% if ship.is_cancelled %}
                    <form method="post" action="{{ url_for('uncancel_order') }}" style="display:inline;">
                      <input type="hidden" name="order_number" value="{{ ship.order_number }}">
                      <button type="submit" class="btn-uncancel">✓ Restore</button>
                    </form>
                  {% else %}
                    <button type="button" class="btn-cancel" onclick="cancelOrder('{{ ship.order_number }}', '{{ ship.tracking_number }}')">🚫 Cancel</button>
                  {% endif %}
                </td>
              </tr>
            {% endfor %}
          </tbody>
        </table>

        <script>
        function cancelOrder(orderNumber, trackingNumber) {
          const reason = prompt("Reason for cancellation:", "Customer requested cancellation");
          if (reason !== null) {
            const form = document.createElement('form');
            form.method = 'POST';
            form.action = "{{ url_for('cancel_order') }}";

            const orderInput = document.createElement('input');
            orderInput.type = 'hidden';
            orderInput.name = 'order_number';
            orderInput.value = orderNumber;
            form.appendChild(orderInput);

            const trackingInput = document.createElement('input');
            trackingInput.type = 'hidden';
            trackingInput.name = 'tracking_number';
            trackingInput.value = trackingNumber;
            form.appendChild(trackingInput);

            const reasonInput = document.createElement('input');
            reasonInput.type = 'hidden';
            reasonInput.name = 'reason';
            reasonInput.value = reason;
            form.appendChild(reasonInput);

            document.body.appendChild(form);
            form.submit();
          }
        }
        </script>

        <div class="pagination">
          <button onclick="window.location.href='{{ prev_url }}'" {% if not has_prev %}disabled{% endif %}>← Previous</button>
          <span>Page {{ page }} of {{ total_pages }} ({{ total_shipments }} shipments)</span>
          <button onclick="window.location.href='{{ next_url }}'" {% if not has_next %}disabled{% endif %}>Next →</button>
        </div>
      {% else %}
        <p style="padding: 40px; text-align: center; color: #666;">
          {% if search_query %}
            No shipments found for "{{ search_query }}".
          {% else %}
            No shipped orders found in the last 120 days.
          {% endif %}
        </p>
      {% endif %}
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
          SELECT id, created_at, carrier, status, notes
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
            customer_email,
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


@app.route("/new_batch", methods=["GET", "POST"])
def new_batch():
    """
    GET: Clear current batch from session and start a new batch (from sidebar link)
    POST: Create a new batch with carrier selection (from form)
    """
    if request.method == "GET":
        # Clear session and start fresh (from sidebar link)
        batch_id = session.pop("batch_id", None)
        if batch_id:
            flash(f"Batch #{batch_id} finished. Starting a new batch.", "success")
        return redirect(url_for("index"))

    # POST: Create new batch from form
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

    # Initialize with defaults (will be used if APIs fail)
    order_number = "N/A"
    customer_name = "Not Found"
    order_id = ""
    customer_email = ""
    scan_carrier = batch_carrier
    shipstation_batch_number = ""

    conn = None
    try:
        conn = get_mysql_connection()

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

                        # Debug: Log what ShipStation is returning
                        print(f"🔍 DEBUG ShipStation fields: {list(first.keys())}")

                        # Try to get email from multiple possible locations
                        customer_email = ""

                        # Check shipment-level fields for email
                        if "customerEmail" in first:
                            customer_email = first.get("customerEmail", "")
                            print(f"📧 Found customerEmail: {customer_email}")
                        elif "buyerEmail" in first:
                            customer_email = first.get("buyerEmail", "")
                            print(f"📧 Found buyerEmail: {customer_email}")

                        # Check shipTo object
                        ship_to = first.get("shipTo", {})
                        if not customer_email and ship_to:
                            if "email" in ship_to:
                                customer_email = ship_to.get("email", "")
                                print(f"📧 Found email in shipTo: {customer_email}")
                            print(f"   shipTo keys: {list(ship_to.keys())}")

                        # Check billTo object
                        bill_to = first.get("billTo", {})
                        if not customer_email and bill_to and "email" in bill_to:
                            customer_email = bill_to.get("email", "")
                            print(f"📧 Found email in billTo: {customer_email}")

                        # Check advancedOptions
                        advanced_options = first.get("advancedOptions", {})
                        if not customer_email and advanced_options:
                            for field in ["customField1", "customField2", "customField3"]:
                                value = advanced_options.get(field, "")
                                if "@" in str(value):
                                    customer_email = value
                                    print(f"📧 Found email in {field}: {customer_email}")
                                    break

                        customer_name = ship_to.get("name", "No Name") if ship_to else "No Name"
                        carrier_code = first.get("carrierCode", "").lower()
                        shipstation_batch_number = first.get("batchNumber", "")

                        if shipstation_batch_number:
                            print(f"📦 ShipStation batch: #{shipstation_batch_number}")

                        if not customer_email:
                            print(f"⚠️ NO EMAIL found in ShipStation for {tracking_number}")

                        carrier_map = {
                            "ups": "UPS",
                            "canadapost": "Canada Post",
                            "canada_post": "Canada Post",
                            "dhl": "DHL",
                            "dhl_express": "DHL",
                            "purolator": "Purolator",
                        }
                        scan_carrier = carrier_map.get(carrier_code, batch_carrier)

                        # ── PO BOX DETECTION ──
                        # Check if shipping address contains PO Box and carrier is incompatible
                        if ship_to:
                            address_lines = [
                                ship_to.get("street1", ""),
                                ship_to.get("street2", ""),
                                ship_to.get("street3", "")
                            ]
                            full_address = " ".join([line for line in address_lines if line])

                            is_valid, po_box_error = check_po_box_compatibility(full_address, scan_carrier)
                            if not is_valid:
                                # PO Box + incompatible carrier detected!
                                print(f"🚫 PO BOX ALERT: {po_box_error}")
                                # Update order_number to show PO BOX warning
                                order_number = f"⚠️ PO BOX - {order_number}"
                                customer_name = f"🚫 PO BOX ({scan_carrier}) - {customer_name}"

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
                # Only update email if Shopify has one (don't overwrite ShipStation's email)
                shopify_email = shopify_info.get("customer_email", "")
                if shopify_email:
                    customer_email = shopify_email
                    print(f"📧 Shopify: Found email {customer_email} for {tracking_number}")
                order_id = shopify_info.get("order_id", order_id)
                print(f"✅ Shopify lookup successful for {tracking_number}: order {order_number}")
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

    except Exception as e:
        print(f"Background API processing error for scan {scan_id}: {e}")

    # ── ALWAYS update the scan record, even if APIs failed ──
    # This ensures we never leave scans stuck with "Processing..." or "Looking up..."
    try:
        if conn is None:
            conn = get_mysql_connection()

        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE scans
            SET carrier = %s,
                order_number = %s,
                customer_name = %s,
                order_id = %s,
                customer_email = %s,
                shipstation_batch_number = %s,
                status = 'Complete'
            WHERE id = %s
            """,
            (scan_carrier, order_number, customer_name, order_id, customer_email, shipstation_batch_number, scan_id)
        )
        conn.commit()
        cursor.close()
        print(f"✓ Updated scan {scan_id}: {tracking_number} -> Order: {order_number}, Customer: {customer_name}")

        # NOTE: Klaviyo notifications are sent when batch is marked as picked up
        # See notify_customers() function - sends "Order Shipped" event for all unique customers in batch
        # This prevents premature notifications before packages are actually ready for pickup

    except Exception as db_error:
        print(f"CRITICAL: Failed to update scan {scan_id} in database: {db_error}")
    finally:
        if conn:
            conn.close()


@app.route("/scan", methods=["POST"])
def scan():
    """
    INSTANT scan endpoint - inserts to database immediately,
    then processes APIs in background thread.

    ✨ NEW: Checks for duplicates across ALL batches in the database,
    not just the current batch.

    ✨ NEW: Automatically detects and splits concatenated tracking numbers
    (e.g., two UPS numbers stuck together like 1ZAC508867380623021ZAC50882034286504)
    """
    code = request.form.get("code", "").strip()
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    if not code:
        if is_ajax:
            return jsonify({"success": False, "error": "No code received."}), 400
        flash("No code received.", "error")
        return redirect(url_for("index"))

    # ═══════════════════════════════════════════════════════════════════
    # ✨ SPLIT DETECTION: Check if multiple tracking numbers are stuck together
    # ═══════════════════════════════════════════════════════════════════
    split_codes = split_concatenated_tracking_numbers(code)

    if len(split_codes) > 1:
        # Multiple tracking numbers detected! Process each one separately
        print(f"🔍 SPLIT DETECTED: {len(split_codes)} tracking numbers found in '{code}'")

        # Process each tracking number through the scan logic
        all_scans = []
        all_messages = []

        for i, individual_code in enumerate(split_codes, 1):
            print(f"   Processing split {i}/{len(split_codes)}: {individual_code}")

            # Process this individual scan
            result = _process_single_scan(individual_code, is_ajax)

            if isinstance(result, tuple):  # Error response
                # If any scan fails, return the error
                return result
            elif isinstance(result, dict):  # AJAX response
                all_scans.append(result.get("scan"))
                all_messages.append(result.get("message"))
            # For redirects, we'll collect and show summary

        # Return combined result for AJAX
        if is_ajax:
            combined_message = f"✓ SPLIT SCAN: {len(split_codes)} tracking numbers processed\n" + "\n".join(all_messages)
            return jsonify({
                "success": True,
                "split_detected": True,
                "scans": all_scans,
                "message": combined_message,
                "count": len(split_codes)
            })
        else:
            flash(f"✓ SPLIT SCAN: Processed {len(split_codes)} tracking numbers from concatenated input", "success")
            for msg in all_messages:
                flash(msg, "info")
            return redirect(url_for("index"))

    # Single tracking number - process normally
    return _process_single_scan(code, is_ajax)


def _process_single_scan(code, is_ajax):
    """
    Process a single tracking number scan.

    Args:
        code: The tracking number to process
        is_ajax: Whether this is an AJAX request

    Returns:
        JSON response for AJAX, redirect for regular requests
    """
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

        # ── STRICT CARRIER VALIDATION (BEFORE normalization) ──
        # Must match the batch carrier's expected format, reject everything else
        validation_error = None

        if batch_carrier == "UPS":
            # UPS: MUST start with "1Z"
            if not code.startswith("1Z"):
                validation_error = f"❌ Not a UPS label! UPS tracking numbers must start with '1Z'. (Scanned: {code[:20]}...)"

        elif batch_carrier == "Canada Post":
            # Canada Post: MUST be 16+ chars starting with digits or "CA"
            if not (len(code) >= 16 and (code[:4].isdigit() or code.startswith("CA"))):
                validation_error = f"❌ Not a Canada Post label! Expected 16+ characters starting with digits or 'CA'. (Scanned: {code[:20]}...)"

        elif batch_carrier == "Purolator":
            # Purolator: MUST be 12 digits (after normalization it would be 12 digits)
            # Before normalization, it could be 34 chars
            if not (len(code) == 12 and code.isdigit()) and len(code) != 34:
                validation_error = f"❌ Not a Purolator label! Expected 12 digits or 34-character barcode. (Scanned: {code[:20]}...)"

        elif batch_carrier == "DHL":
            # DHL: MUST be 10 digits
            if not (len(code) == 10 and code.isdigit()):
                validation_error = f"❌ Not a DHL label! Expected 10-digit tracking number. (Scanned: {code[:20]}...)"

        # If validation failed, reject the scan immediately
        if validation_error:
            print(f"🚫 REJECTED: {validation_error}")
            if is_ajax:
                return jsonify({"success": False, "error": validation_error, "carrier_mismatch": True}), 400
            flash(validation_error, "error")
            return redirect(url_for("index"))

        # NOW normalize codes for specific carriers (AFTER validation)
        original_code = code
        if batch_carrier == "Canada Post":
            if len(code) >= 12:
                code = code[7:-5]
        elif batch_carrier == "Purolator":
            if len(code) == 34:
                code = code[11:-11]

        # ─────────────────────────────────────────────────────────────────
        # ✨ CHECK FOR CANCELLED ORDERS (BEFORE duplicate check)
        # ─────────────────────────────────────────────────────────────────
        cursor = conn.cursor(dictionary=True)

        # First, get the order number for this tracking number (if known)
        cursor.execute(
            """
            SELECT order_number FROM scans
            WHERE tracking_number = %s
            ORDER BY scan_date DESC
            LIMIT 1
            """,
            (code,)
        )
        scan_with_order = cursor.fetchone()

        # Check if this order is cancelled
        order_to_check = scan_with_order.get('order_number') if scan_with_order else None
        if order_to_check and order_to_check not in ('Processing...', 'N/A', ''):
            cursor.execute(
                """
                SELECT reason FROM cancelled_orders
                WHERE order_number = %s
                """,
                (order_to_check,)
            )
            cancelled = cursor.fetchone()

            if cancelled:
                cancel_reason = cancelled.get('reason', 'Order cancelled')
                error_msg = f"🚫 CANCELLED ORDER: {order_to_check} - DO NOT SHIP\nReason: {cancel_reason}"
                print(f"🚫 CANCELLED: {error_msg}")

                cursor.close()
                conn.close()

                if is_ajax:
                    return jsonify({
                        "success": False,
                        "error": error_msg,
                        "cancelled_order": True,
                        "order_number": order_to_check,
                        "cancel_reason": cancel_reason
                    }), 400
                flash(error_msg, "error")
                return redirect(url_for("index"))

        # ─────────────────────────────────────────────────────────────────
        # ✨ Check for duplicate across ALL BATCHES in the database
        # ─────────────────────────────────────────────────────────────────
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
               scan_date, status, order_id, customer_email, batch_id, shipstation_batch_number)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (code, scan_carrier, order_number, customer_name,
             now_str, status, order_id, "", batch_id, "")
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
    batch_id = session.get("batch_id")
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
                 tracking_numbers = %s,
                 status = 'recorded'
           WHERE id = %s
        """, (pkg_count, tracking_csv, batch_id))
        conn.commit()

        # Keep session for immediate notification, but allow viewing from batches page
        flash(f"✓ Batch #{batch_id} marked as picked up ({pkg_count} parcels). Ready to notify customers.", "success")
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


@app.route("/finish_batch", methods=["POST"])
def finish_batch():
    """
    Finish the current batch and clear session so user can create a new batch.
    """
    batch_id = session.pop("batch_id", None)
    if batch_id:
        flash(f"Batch #{batch_id} finished. You can now create a new batch.", "success")
    return redirect(url_for("index"))


@app.route("/save_batch_notes", methods=["POST"])
def save_batch_notes():
    """
    Save notes for the current batch.
    """
    batch_id = session.get("batch_id")
    if not batch_id:
        flash("No batch open.", "error")
        return redirect(url_for("index"))

    notes = request.form.get("notes", "").strip()

    try:
        conn = get_mysql_connection()
    except Exception as e:
        flash(f"Database connection error: {e}", "error")
        return redirect(url_for("index"))

    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE batches SET notes = %s WHERE id = %s", (notes, batch_id))
        conn.commit()
        flash("Notes saved successfully.", "success")
    except mysql.connector.Error as e:
        flash(f"Error saving notes: {e}", "error")
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    return redirect(url_for("index"))


@app.route("/notify_customers", methods=["POST"])
def notify_customers():
    """
    Send Klaviyo notifications to customers for all orders in the batch.
    Only notifies each order number once (prevents duplicate notifications).
    """
    batch_id = session.get("batch_id")
    if not batch_id:
        flash("No batch open.", "error")
        return redirect(url_for("index"))

    try:
        conn = get_mysql_connection()
    except Exception as e:
        flash(f"Database connection error: {e}", "error")
        return redirect(url_for("index"))

    try:
        cursor = conn.cursor(dictionary=True)

        # Get batch info
        cursor.execute("SELECT carrier, status FROM batches WHERE id = %s", (batch_id,))
        batch = cursor.fetchone()
        if not batch:
            flash("Batch not found.", "error")
            return redirect(url_for("index"))

        carrier = batch['carrier']
        batch_status = batch.get('status', 'in_progress')

        # Check if batch is recorded
        if batch_status != 'recorded' and batch_status != 'notified':
            flash("Please mark the batch as picked up first.", "warning")
            return redirect(url_for("index"))

        # Get all scans with customer emails
        cursor.execute("""
            SELECT DISTINCT
                order_number,
                customer_email,
                customer_name,
                tracking_number,
                order_id
            FROM scans
            WHERE batch_id = %s
              AND order_number != 'N/A'
              AND order_number != 'Processing...'
              AND customer_email != ''
              AND customer_email IS NOT NULL
        """, (batch_id,))

        scans = cursor.fetchall()

        print(f"🔍 DEBUG: Found {len(scans)} scans with customer emails in batch {batch_id}")
        for scan in scans:
            print(f"   - Order {scan['order_number']}: {scan['customer_email']}")

        if not scans:
            # Check total scans in batch
            cursor.execute("SELECT COUNT(*) as total FROM scans WHERE batch_id = %s", (batch_id,))
            total = cursor.fetchone()['total']
            print(f"⚠️ No emails found! Total scans in batch: {total}")

            # Check how many have emails vs no emails
            cursor.execute("SELECT customer_email, COUNT(*) as count FROM scans WHERE batch_id = %s GROUP BY customer_email", (batch_id,))
            email_breakdown = cursor.fetchall()
            print(f"📊 Email breakdown:")
            for row in email_breakdown:
                print(f"   - '{row['customer_email']}': {row['count']} scans")

            flash("No orders with email addresses found in this batch.", "warning")
            return redirect(url_for("index"))

        # Initialize Klaviyo API
        try:
            from klaviyo_api import KlaviyoAPI
            klaviyo = KlaviyoAPI()
        except Exception as e:
            flash(f"Klaviyo API initialization failed: {e}", "error")
            return redirect(url_for("index"))

        # Track notifications
        success_count = 0
        skip_count = 0
        error_count = 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Initialize Shopify API to fetch line items
        try:
            shopify_api = get_shopify_api()
        except Exception as e:
            print(f"⚠️ Shopify API not available: {e}")
            shopify_api = None

        for scan in scans:
            order_number = scan['order_number']
            customer_email = scan['customer_email']
            tracking_number = scan['tracking_number']
            order_id = scan.get('order_id', '')

            # Check if this order was already notified (in ANY batch)
            cursor.execute("""
                SELECT id FROM notifications
                WHERE order_number = %s
                LIMIT 1
            """, (order_number,))

            if cursor.fetchone():
                print(f"Skipping order {order_number} - already notified")
                skip_count += 1
                continue

            # Fetch line items from Shopify
            line_items = []
            if shopify_api and tracking_number:
                try:
                    order_data = shopify_api.get_order_by_tracking(tracking_number)
                    line_items = order_data.get('line_items', [])
                    if line_items:
                        print(f"📦 Found {len(line_items)} items for order {order_number}")
                except Exception as e:
                    print(f"⚠️ Could not fetch line items for {order_number}: {e}")

            # Send Klaviyo event
            success = klaviyo.notify_order_shipped(
                email=customer_email,
                order_number=order_number,
                tracking_number=tracking_number,
                carrier=carrier,
                line_items=line_items
            )

            # Record notification attempt
            try:
                cursor.execute("""
                    INSERT INTO notifications
                        (batch_id, order_number, customer_email, tracking_number, notified_at, success, error_message)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (batch_id, order_number, customer_email, tracking_number, now, success, None if success else "Klaviyo API error"))
                conn.commit()

                if success:
                    success_count += 1
                else:
                    error_count += 1
            except mysql.connector.IntegrityError:
                # Duplicate entry - order already notified
                skip_count += 1
                print(f"Order {order_number} already in notifications table")

        # Update batch status to 'notified'
        cursor.execute("""
            UPDATE batches
            SET status = 'notified', notified_at = %s
            WHERE id = %s
        """, (now, batch_id))
        conn.commit()

        # Build success message
        message_parts = []
        if success_count > 0:
            message_parts.append(f"✉ {success_count} customer(s) notified")
        if skip_count > 0:
            message_parts.append(f"{skip_count} already notified")
        if error_count > 0:
            message_parts.append(f"{error_count} failed")

        flash(" | ".join(message_parts), "success" if error_count == 0 else "warning")
        return redirect(url_for("index"))

    except Exception as e:
        print(f"Error in notify_customers: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Error sending notifications: {e}", "error")
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


@app.route("/all_batches", methods=["GET"])
def all_batches():
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
          SELECT id, carrier, created_at, pkg_count, tracking_numbers, status, notified_at, notes
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
          SELECT id, carrier, created_at, pkg_count, tracking_numbers, status, notified_at, notes
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
                 shipstation_batch_number,
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
            customer_email,
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


@app.route("/retry_fetch_scan", methods=["POST"])
def retry_fetch_scan():
    """
    Retry fetching customer information for a scan that failed.
    """
    scan_id = request.form.get("scan_id")
    if not scan_id:
        flash("No scan specified.", "error")
        return redirect(url_for("all_scans"))

    try:
        conn = get_mysql_connection()
    except Exception as e:
        flash(f"Database connection error: {e}", "error")
        return redirect(url_for("all_scans"))

    try:
        cursor = conn.cursor(dictionary=True)

        # Get scan details
        cursor.execute("""
            SELECT id, tracking_number, batch_id
            FROM scans
            WHERE id = %s
        """, (scan_id,))
        scan = cursor.fetchone()

        if not scan:
            flash(f"Scan #{scan_id} not found.", "error")
            return redirect(url_for("all_scans"))

        # Get batch carrier
        cursor.execute("SELECT carrier FROM batches WHERE id = %s", (scan['batch_id'],))
        batch = cursor.fetchone()
        batch_carrier = batch['carrier'] if batch else ""

        # Launch background processing thread
        import threading
        api_thread = threading.Thread(
            target=process_scan_apis_background,
            args=(scan['id'], scan['tracking_number'], batch_carrier),
            daemon=True
        )
        api_thread.start()

        flash(f"Re-fetching customer info for scan #{scan_id}...", "success")
        return redirect(url_for("all_scans"))

    except Exception as e:
        print(f"Error retrying fetch for scan {scan_id}: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Error: {e}", "error")
        return redirect(url_for("all_scans"))
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


@app.route("/all_scans", methods=["GET"])
def all_scans():
    order_search = request.args.get("order_number", "").strip()
    page = int(request.args.get("page", 1))
    per_page = 100
    offset = (page - 1) * per_page

    conn = None
    cursor = None
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)

        # Get total count for pagination
        if order_search:
            like_pattern = f"%{order_search}%"
            cursor.execute("""
              SELECT COUNT(*) as total
              FROM scans
              WHERE order_number = %s
                 OR LOWER(customer_name) LIKE LOWER(%s)
            """, (order_search, like_pattern))
        else:
            cursor.execute("SELECT COUNT(*) as total FROM scans")

        total_scans = cursor.fetchone()['total']
        total_pages = (total_scans + per_page - 1) // per_page  # Ceiling division

        # Get paginated results
        if order_search:
            like_pattern = f"%{order_search}%"
            cursor.execute("""
              SELECT
                id,
                tracking_number,
                carrier,
                shipstation_batch_number,
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
              LIMIT %s OFFSET %s
            """, (order_search, like_pattern, per_page, offset))
        else:
            cursor.execute("""
              SELECT
                id,
                tracking_number,
                carrier,
                shipstation_batch_number,
                order_number,
                customer_name,
                scan_date,
                status,
                order_id,
                batch_id
              FROM scans
              ORDER BY scan_date DESC
              LIMIT %s OFFSET %s
            """, (per_page, offset))

        scans = cursor.fetchall()

        return render_template_string(
            ALL_SCANS_TEMPLATE,
            scans=scans,
            shop_url=SHOP_URL,
            version=__version__,
            page=page,
            total_pages=total_pages,
            total_scans=total_scans,
            order_search=order_search
        )
    except mysql.connector.errors.OperationalError as e:
        print(f"MySQL connection error in all_scans: {e}")
        flash("Database connection error. Please try again.", "error")
        return redirect(url_for("index"))
    except Exception as e:
        print(f"Error in all_scans: {e}")
        flash("An error occurred while loading scans.", "error")
        return redirect(url_for("index"))
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.route("/pick_and_pack", methods=["GET", "POST"])
def pick_and_pack():
    """
    Order verification / pick and pack page.
    Allows searching by tracking number or order number, displays line items,
    and saves verification records.
    """
    order_data = None
    error_message = None
    already_verified = None
    search_identifier = ""

    if request.method == "POST":
        action = request.form.get("action")

        if action == "search":
            search_identifier = request.form.get("identifier", "").strip()

            if not search_identifier:
                error_message = "Please enter a tracking number or order number"
            else:
                # Try to fetch order from Shopify
                try:
                    shopify_api = get_shopify_api()
                    order_data = shopify_api.get_order_details_for_verification(search_identifier)

                    if not order_data:
                        error_message = f"Order not found for '{search_identifier}'. Please check the number and try again."
                    else:
                        # Add location information to each line item
                        for item in order_data.get('line_items', []):
                            item['location'] = get_item_location(item['sku'], item['name'])

                        # Check if already verified
                        conn = get_mysql_connection()
                        try:
                            cursor = conn.cursor(dictionary=True)
                            cursor.execute("""
                                SELECT verified_at, items_checked, total_items
                                FROM order_verifications
                                WHERE order_number = %s
                                ORDER BY verified_at DESC
                                LIMIT 1
                            """, (order_data['order_number'],))
                            verification = cursor.fetchone()

                            if verification:
                                already_verified = {
                                    'date': verification['verified_at'].strftime('%Y-%m-%d %H:%M'),
                                    'items_checked': verification['items_checked'],
                                    'total_items': verification['total_items']
                                }
                        finally:
                            try:
                                cursor.close()
                            except Exception:
                                pass
                            conn.close()

                except Exception as e:
                    error_message = f"Error fetching order: {str(e)}"

        elif action == "verify":
            # Save verification record
            order_number = request.form.get("order_number")
            tracking_number = request.form.get("tracking_number", "")
            shopify_order_id = request.form.get("shopify_order_id")
            total_items = int(request.form.get("total_items", 0))
            notes = request.form.get("notes", "").strip()

            # Count how many items were checked
            items_checked = 0
            for key in request.form:
                if key.startswith("item_"):
                    items_checked += 1

            conn = get_mysql_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO order_verifications
                    (order_number, tracking_number, shopify_order_id, verified_at, items_checked, total_items, notes)
                    VALUES (%s, %s, %s, NOW(), %s, %s, %s)
                """, (order_number, tracking_number or None, shopify_order_id, items_checked, total_items, notes or None))
                conn.commit()

                flash(f"✅ Order #{order_number} verified! {items_checked}/{total_items} items checked.", "success")
                return redirect(url_for("pick_and_pack"))

            except Exception as e:
                flash(f"Error saving verification: {str(e)}", "error")
            finally:
                try:
                    cursor.close()
                except Exception:
                    pass
                conn.close()

    return render_template_string(
        PICK_AND_PACK_TEMPLATE,
        order_data=order_data,
        error_message=error_message,
        already_verified=already_verified,
        search_identifier=search_identifier,
        shop_url=SHOP_URL,
        version=__version__
    )


@app.route("/item_locations", methods=["GET"])
def item_locations():
    """
    Item locations admin page.
    Displays all location rules and allows adding/deleting rules.
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, aisle, shelf, rule_type, rule_value, created_at
            FROM item_location_rules
            ORDER BY aisle, shelf, rule_type, rule_value
        """)
        rules = cursor.fetchall()

        return render_template_string(
            ITEM_LOCATIONS_TEMPLATE,
            rules=rules,
            shop_url=SHOP_URL,
            version=__version__
        )
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


@app.route("/stuck_orders", methods=["GET"])
def stuck_orders():
    """
    Display all scans where customer information is missing or incomplete.
    These are orders where:
    - order_number = "Processing..." OR
    - customer_name = "Looking up..." OR
    - customer_name = "No Order Found" OR
    - customer_name = "Not Found" OR
    - customer_name starts with "Error:"
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
          SELECT
            id,
            tracking_number,
            carrier,
            shipstation_batch_number,
            order_number,
            customer_name,
            scan_date,
            status,
            order_id,
            batch_id
          FROM scans
          WHERE order_number = 'Processing...'
             OR order_number = 'N/A'
             OR customer_name = 'Looking up...'
             OR customer_name = 'No Order Found'
             OR customer_name = 'Not Found'
             OR customer_name LIKE 'Error:%'
          ORDER BY scan_date DESC
        """)

        stuck_scans = cursor.fetchall()

        return render_template_string(
            STUCK_ORDERS_TEMPLATE,
            stuck_scans=stuck_scans,
            version=__version__
        )
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


@app.route("/api/fix_order/<int:scan_id>", methods=["POST"])
def fix_order(scan_id):
    """
    API endpoint to manually retry fetching order details from Shopify/ShipStation.
    Called when user clicks "Fix" button on a stuck order.
    """
    try:
        data = request.get_json()
        tracking_number = data.get('tracking_number', '')
        carrier = data.get('carrier', '')

        if not tracking_number:
            return jsonify({
                'success': False,
                'message': 'Tracking number is required'
            }), 400

        # Get the scan from database to verify it exists
        conn = get_mysql_connection()
        try:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM scans WHERE id = %s", (scan_id,))
            scan = cursor.fetchone()

            if not scan:
                return jsonify({
                    'success': False,
                    'message': 'Scan not found'
                }), 404

            # Initialize with defaults
            order_number = "N/A"
            customer_name = "Not Found"
            customer_email = ""
            order_id = ""
            scan_carrier = carrier or scan.get('carrier', '')
            shipstation_batch_number = ""

            # ── ShipStation lookup ──
            shopify_found = False
            try:
                if SHIPSTATION_API_KEY and SHIPSTATION_API_SECRET:
                    url = f"https://ssapi.shipstation.com/shipments?trackingNumber={tracking_number}"
                    resp = requests.get(
                        url,
                        auth=(SHIPSTATION_API_KEY, SHIPSTATION_API_SECRET),
                        headers={"Accept": "application/json"},
                        timeout=15  # Increased from 6 to 15 seconds
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    shipments = data.get("shipments", [])

                    if shipments:
                        first = shipments[0]
                        order_number = first.get("orderNumber", "N/A")

                        # Debug: Log what ShipStation is returning
                        print(f"🔍 DEBUG ShipStation fields: {list(first.keys())}")

                        # Try to get email from multiple possible locations
                        customer_email = ""

                        # Check shipment-level fields
                        if "customerEmail" in first:
                            customer_email = first.get("customerEmail", "")
                            print(f"📧 Found customerEmail: {customer_email}")
                        elif "buyerEmail" in first:
                            customer_email = first.get("buyerEmail", "")
                            print(f"📧 Found buyerEmail: {customer_email}")

                        # Check shipTo
                        ship_to = first.get("shipTo", {})
                        if not customer_email and ship_to:
                            if "email" in ship_to:
                                customer_email = ship_to.get("email", "")
                                print(f"📧 Found email in shipTo: {customer_email}")
                            print(f"   shipTo keys: {list(ship_to.keys())}")

                        # Check billTo
                        bill_to = first.get("billTo", {})
                        if not customer_email and bill_to and "email" in bill_to:
                            customer_email = bill_to.get("email", "")
                            print(f"📧 Found email in billTo: {customer_email}")

                        customer_name = ship_to.get("name", "No Name") if ship_to else "No Name"
                        carrier_code = first.get("carrierCode", "").lower()
                        shipstation_batch_number = first.get("batchNumber", "")

                        if shipstation_batch_number:
                            print(f"📦 ShipStation batch: #{shipstation_batch_number}")

                        if not customer_email:
                            print(f"⚠️ NO EMAIL found in ShipStation for {tracking_number}")

                        carrier_map = {
                            "ups": "UPS",
                            "canadapost": "Canada Post",
                            "canada_post": "Canada Post",
                            "dhl": "DHL",
                            "dhl_express": "DHL",
                            "purolator": "Purolator",
                        }
                        scan_carrier = carrier_map.get(carrier_code, scan_carrier)
            except Exception as e:
                print(f"ShipStation error for {tracking_number}: {e}")

            # ── Shopify lookup ──
            try:
                shopify_api = get_shopify_api()
                shopify_info = shopify_api.get_order_by_tracking(tracking_number)

                if shopify_info and shopify_info.get("order_id"):
                    shopify_found = True
                    order_number = shopify_info.get("order_number", order_number)
                    customer_name = shopify_info.get("customer_name", customer_name)
                    # Only update email if Shopify has one (don't overwrite ShipStation's email)
                    shopify_email = shopify_info.get("customer_email", "")
                    if shopify_email:
                        customer_email = shopify_email
                        print(f"📧 Shopify: Found email {customer_email} for {tracking_number}")
                    order_id = shopify_info.get("order_id", order_id)
            except Exception as e:
                print(f"Shopify error for {tracking_number}: {e}")

            # ── Update the scan record with results ──
            cursor.execute(
                """
                UPDATE scans
                SET carrier = %s,
                    order_number = %s,
                    customer_name = %s,
                    customer_email = %s,
                    order_id = %s,
                    shipstation_batch_number = %s,
                    status = %s
                WHERE id = %s
                """,
                (scan_carrier, order_number, customer_name, customer_email, order_id, shipstation_batch_number,
                 'Complete' if (order_number != 'N/A' or customer_name != 'Not Found') else 'Processing',
                 scan_id)
            )
            conn.commit()

            # Fetch the updated scan
            cursor.execute("SELECT * FROM scans WHERE id = %s", (scan_id,))
            updated_scan = cursor.fetchone()

            return jsonify({
                'success': True,
                'message': 'Order updated successfully',
                'scan': {
                    'id': updated_scan['id'],
                    'tracking_number': updated_scan['tracking_number'],
                    'carrier': updated_scan['carrier'],
                    'order_number': updated_scan['order_number'],
                    'customer_name': updated_scan['customer_name'],
                    'order_id': updated_scan.get('order_id', ''),
                    'status': updated_scan['status']
                }
            })

        finally:
            try:
                cursor.close()
            except Exception:
                pass
            conn.close()

    except Exception as e:
        print(f"Error in fix_order: {e}")
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500


@app.route("/add_location_rule", methods=["POST"])
def add_location_rule():
    """
    Add a new location rule.
    """
    aisle = request.form.get("aisle", "").strip()
    shelf = request.form.get("shelf", "").strip()
    rule_type = request.form.get("rule_type", "").strip()
    rule_value = request.form.get("rule_value", "").strip()

    if not all([aisle, shelf, rule_type, rule_value]):
        flash("All fields are required.", "error")
        return redirect(url_for("item_locations"))

    if rule_type not in ['sku', 'keyword']:
        flash("Invalid rule type.", "error")
        return redirect(url_for("item_locations"))

    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO item_location_rules (aisle, shelf, rule_type, rule_value)
            VALUES (%s, %s, %s, %s)
        """, (aisle, shelf, rule_type, rule_value))
        conn.commit()

        flash(f"✅ Location rule added: {aisle}, {shelf} for {rule_type.upper()} '{rule_value}'", "success")
    except Exception as e:
        flash(f"Error adding rule: {str(e)}", "error")
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()

    return redirect(url_for("item_locations"))


@app.route("/delete_location_rule", methods=["POST"])
def delete_location_rule():
    """
    Delete a location rule.
    """
    rule_id = request.form.get("rule_id")

    if not rule_id:
        flash("Invalid rule ID.", "error")
        return redirect(url_for("item_locations"))

    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM item_location_rules WHERE id = %s", (rule_id,))
        conn.commit()

        if cursor.rowcount > 0:
            flash("✅ Location rule deleted.", "success")
        else:
            flash("Rule not found.", "error")
    except Exception as e:
        flash(f"Error deleting rule: {str(e)}", "error")
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()

    return redirect(url_for("item_locations"))


@app.route("/check_shipments", methods=["GET"])
def check_shipments():
    """
    Check shipment status page.
    Pulls from cached shipments data, checks scan database, and queries UPS for tracking.
    Flags shipments that are stuck (label printed but not moving).
    """
    search_query = request.args.get("search", "").strip()
    page = int(request.args.get("page", 1))
    per_page = 50

    try:
        # Query from cache instead of ShipStation API (much faster!)
        print(f"📦 Querying shipments cache (page {page})...")

        conn = get_mysql_connection()
        cursor = conn.cursor(dictionary=True)

        # Build search condition
        search_condition = ""
        search_params = []
        if search_query:
            search_condition = """
                AND (
                    sc.tracking_number LIKE %s
                    OR sc.order_number LIKE %s
                    OR sc.customer_name LIKE %s
                )
            """
            search_like = f"%{search_query}%"
            search_params = [search_like, search_like, search_like]

        # Get total count for pagination
        count_query = f"""
            SELECT COUNT(*) as total
            FROM shipments_cache sc
            WHERE sc.ship_date >= DATE_SUB(CURDATE(), INTERVAL 120 DAY)
            {search_condition}
        """
        cursor.execute(count_query, search_params)
        total_shipments = cursor.fetchone()['total']
        total_pages = max(1, (total_shipments + per_page - 1) // per_page)

        # Get paginated shipments from cache
        offset = (page - 1) * per_page
        query = f"""
            SELECT
                sc.tracking_number,
                sc.order_number,
                sc.customer_name,
                sc.carrier_code,
                sc.ship_date,
                sc.shipstation_batch_number,
                MAX(s.scan_date) as scan_date,
                co.id as cancelled_id,
                co.reason as cancel_reason,
                co.cancelled_at
            FROM shipments_cache sc
            LEFT JOIN scans s ON s.tracking_number COLLATE utf8mb4_unicode_ci = sc.tracking_number COLLATE utf8mb4_unicode_ci
            LEFT JOIN cancelled_orders co ON co.order_number COLLATE utf8mb4_unicode_ci = sc.order_number COLLATE utf8mb4_unicode_ci
            WHERE sc.ship_date >= DATE_SUB(CURDATE(), INTERVAL 120 DAY)
            {search_condition}
            GROUP BY sc.tracking_number, sc.order_number, sc.customer_name,
                     sc.carrier_code, sc.ship_date, sc.shipstation_batch_number,
                     co.id, co.reason, co.cancelled_at
            ORDER BY sc.ship_date DESC
            LIMIT %s OFFSET %s
        """
        cursor.execute(query, search_params + [per_page, offset])
        cached_shipments = cursor.fetchall()

        print(f"✓ Found {len(cached_shipments)} shipments on page {page} of {total_pages} ({total_shipments} total)")

        # Process each shipment
        shipments = []
        ups_api = get_ups_api()

        for cached_ship in cached_shipments:
            tracking_number = cached_ship.get("tracking_number", "")
            order_number = cached_ship.get("order_number", "")
            carrier_code = cached_ship.get("carrier_code", "").upper()
            ship_date = str(cached_ship.get("ship_date", ""))
            customer_name = cached_ship.get("customer_name", "Unknown")
            shipstation_batch = cached_ship.get("shipstation_batch_number", "")

            # Check if cancelled (from the LEFT JOIN)
            is_cancelled = cached_ship.get("cancelled_id") is not None
            cancel_reason = cached_ship.get("cancel_reason", "")

            # Check if scanned (from the LEFT JOIN)
            scan_date_obj = cached_ship.get("scan_date")
            scanned = scan_date_obj is not None
            scan_date = ""
            if scan_date_obj:
                if isinstance(scan_date_obj, str):
                    scan_date = scan_date_obj[:10]
                else:
                    scan_date = scan_date_obj.strftime("%Y-%m-%d")

            # Get UPS tracking status (only for UPS shipments)
            ups_status = "unknown"
            ups_status_text = "N/A"
            ups_last_activity = "—"

            if carrier_code == "UPS" and tracking_number:
                try:
                    ups_tracking = ups_api.get_tracking_status(tracking_number)
                    ups_status = ups_tracking.get("status", "unknown")
                    ups_status_text = ups_tracking.get("status_description", "Unknown")
                    ups_last_activity = ups_tracking.get("last_activity", "—")

                    # Clean up status text
                    if ups_status == "label_created":
                        ups_status_text = "Label Created"
                    elif ups_status == "in_transit":
                        ups_status_text = "In Transit"
                    elif ups_status == "delivered":
                        ups_status_text = "Delivered"
                    elif ups_status == "exception":
                        ups_status_text = "Exception/Delay"
                    elif ups_status == "error":
                        # Don't show full error in UI, just log it
                        ups_status = "unknown"
                        ups_status_text = "—"
                        print(f"⚠️ UPS error for {tracking_number}: {ups_tracking.get('error', 'Unknown error')}")
                except Exception as e:
                    # Non-blocking UPS errors
                    print(f"⚠️ UPS exception for {tracking_number}: {e}")
                    ups_status = "unknown"
                    ups_status_text = "—"

            # Determine if shipment should be flagged
            flag = False
            flag_reason = ""
            flag_severity = "normal"  # normal, warning, critical

            if carrier_code == "UPS":
                # Calculate days since ship date
                try:
                    ship_datetime = datetime.strptime(ship_date, "%Y-%m-%d")
                    days_since_ship = (datetime.now() - ship_datetime).days

                    # CRITICAL ALERT: Label created but NEVER scanned after 6+ days
                    # This means package was never sent out!
                    if not scanned and days_since_ship >= 6:
                        flag = True
                        flag_severity = "critical"
                        flag_reason = f"🚨 CRITICAL: Label created {days_since_ship} days ago but NEVER SCANNED! Package was never sent."

                    # HIGH ALERT: Scanned but UPS lost it (stuck in label_created = carrier never picked it up)
                    elif scanned and ups_status == "label_created" and days_since_ship >= 3:
                        flag = True
                        flag_severity = "critical"
                        flag_reason = f"🚨 CRITICAL: Scanned {days_since_ship} days ago but UPS shows no pickup. Carrier may have lost it."

                    # WARNING: Label created but not scanned (2-5 days)
                    elif not scanned and days_since_ship >= 2:
                        flag = True
                        flag_severity = "warning"
                        flag_reason = f"⚠️ WARNING: Not scanned after {days_since_ship} days. Check if label was printed."

                    # WARNING: Exception/delay status from carrier
                    elif ups_status == "exception":
                        flag = True
                        flag_severity = "warning"
                        flag_reason = "⚠️ WARNING: Shipment exception or delay reported by carrier."
                except:
                    pass

            shipments.append({
                "order_number": order_number,
                "customer_name": customer_name,
                "tracking_number": tracking_number,
                "carrier": carrier_code,
                "ship_date": ship_date,
                "scanned": scanned,
                "scan_date": scan_date,
                "ups_status": ups_status,
                "ups_status_text": ups_status_text,
                "ups_last_activity": ups_last_activity,
                "flag": flag,
                "flag_reason": flag_reason,
                "flag_severity": flag_severity,
                "is_cancelled": is_cancelled,
                "cancel_reason": cancel_reason
            })

        cursor.close()
        conn.close()

        # Pagination URLs
        has_prev = page > 1
        has_next = page < total_pages
        prev_url = url_for("check_shipments", page=page-1, search=search_query) if has_prev else "#"
        next_url = url_for("check_shipments", page=page+1, search=search_query) if has_next else "#"

        return render_template_string(
            CHECK_SHIPMENTS_TEMPLATE,
            shipments=shipments,
            search_query=search_query,
            loading=False,
            page=page,
            total_pages=total_pages,
            total_shipments=total_shipments,
            has_prev=has_prev,
            has_next=has_next,
            prev_url=prev_url,
            next_url=next_url,
            version=__version__
        )

    except Exception as e:
        print(f"❌ Error in check_shipments: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Error loading shipments: {str(e)}", "error")
        return render_template_string(
            CHECK_SHIPMENTS_TEMPLATE,
            shipments=[],
            search_query=search_query,
            loading=False,
            page=1,
            total_pages=1,
            total_shipments=0,
            has_prev=False,
            has_next=False,
            prev_url="#",
            next_url="#",
            version=__version__
        )


@app.route("/cancel_order", methods=["POST"])
def cancel_order():
    """
    Mark an order as cancelled so it shows up as "DO NOT SHIP" when scanned.
    """
    order_number = request.form.get("order_number", "").strip()
    tracking_number = request.form.get("tracking_number", "").strip()
    reason = request.form.get("reason", "Order cancelled").strip()

    if not order_number:
        flash("Order number is required to cancel an order.", "error")
        return redirect(url_for("check_shipments"))

    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()

        # Check if already cancelled
        cursor.execute("SELECT id FROM cancelled_orders WHERE order_number = %s", (order_number,))
        if cursor.fetchone():
            flash(f"Order #{order_number} is already marked as cancelled.", "warning")
            cursor.close()
            conn.close()
            return redirect(url_for("check_shipments"))

        # Insert into cancelled_orders table
        cursor.execute(
            """
            INSERT INTO cancelled_orders (order_number, tracking_number, reason, cancelled_by)
            VALUES (%s, %s, %s, %s)
            """,
            (order_number, tracking_number, reason, "Customer Service")
        )
        conn.commit()
        cursor.close()
        conn.close()

        flash(f"✓ Order #{order_number} marked as CANCELLED. It will show 'DO NOT SHIP' when scanned.", "success")
        return redirect(url_for("check_shipments"))

    except Exception as e:
        print(f"Error cancelling order: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Error cancelling order: {str(e)}", "error")
        return redirect(url_for("check_shipments"))


@app.route("/uncancel_order", methods=["POST"])
def uncancel_order():
    """
    Remove cancellation status from an order.
    """
    order_number = request.form.get("order_number", "").strip()

    if not order_number:
        flash("Order number is required.", "error")
        return redirect(url_for("check_shipments"))

    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()

        # Delete from cancelled_orders
        cursor.execute("DELETE FROM cancelled_orders WHERE order_number = %s", (order_number,))
        conn.commit()

        deleted_count = cursor.rowcount
        cursor.close()
        conn.close()

        if deleted_count > 0:
            flash(f"✓ Order #{order_number} cancellation removed. Order is now active.", "success")
        else:
            flash(f"Order #{order_number} was not found in cancelled orders.", "warning")

        return redirect(url_for("check_shipments"))

    except Exception as e:
        print(f"Error uncancelling order: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Error uncancelling order: {str(e)}", "error")
        return redirect(url_for("check_shipments"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
