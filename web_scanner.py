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
import atexit
import json

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
    render_template,
    render_template_string,
    flash,
    session,
    jsonify
)
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta, timezone
import threading
import csv
import io

# Google OAuth
from authlib.integrations.flask_client import OAuth

# Timezone support for Vancouver/PST
try:
    from zoneinfo import ZoneInfo
    PST = ZoneInfo("America/Vancouver")
except ImportError:
    # Fallback for Python < 3.9
    PST = timezone(timedelta(hours=-8))  # PST is UTC-8

def now_pst():
    """Get current time in Vancouver/PST timezone."""
    return datetime.now(PST)

def format_pst(dt):
    """Format a datetime to PST timezone string."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        # Assume UTC if no timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PST).strftime("%Y-%m-%d %H:%M")

def normalize_carrier(carrier_code):
    """Normalize carrier codes to display-friendly names."""
    if not carrier_code:
        return ""
    carrier_upper = carrier_code.upper()
    carrier_map = {
        "CANADA_POST_WALLETED": "Canada Post",
        "CANADA_POST": "Canada Post",
        "CANADAPOST": "Canada Post",
        "UPS": "UPS",
        "UPS_GROUND": "UPS",
        "UPS_EXPRESS": "UPS",
        "DHL": "DHL",
        "DHL_EXPRESS": "DHL",
        "PUROLATOR": "Purolator",
        "FEDEX": "FedEx",
        "USPS": "USPS",
    }
    return carrier_map.get(carrier_upper, carrier_code)

from shopify_api import ShopifyAPI  # Assumes shopify_api.py is alongside this file
from klaviyo_events import KlaviyoEvents  # Klaviyo integration for event tracking
from orders_sync import OrdersSync, update_order_scanned_status, init_orders_tables  # Orders sync from Shopify
from ups_api import UPSAPI  # UPS tracking integration
from canadapost_api import CanadaPostAPI  # Canada Post tracking integration
from tracking_utils import split_concatenated_tracking_numbers  # Tracking number split detection
from address_utils import is_po_box, check_po_box_compatibility  # PO Box detection
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)

# Trust proxy headers (Kinsta/cloud providers terminate SSL at the proxy)
# This ensures url_for generates https:// URLs
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# ── Secure session cookie settings ──
app.config.update(
    SESSION_COOKIE_SECURE=True,    # only send cookie over HTTPS
    SESSION_COOKIE_HTTPONLY=True,  # JS can't read the cookie
    SESSION_COOKIE_SAMESITE='Lax',  # basic CSRF protection on cookies
    PREFERRED_URL_SCHEME='https'   # Force https in url_for
)

# Read SECRET_KEY from the environment (and fail loudly if missing)
app.secret_key = os.environ["FLASK_SECRET_KEY"]

# 30 minutes in seconds
INACTIVITY_TIMEOUT = 30 * 60

# ── Google OAuth Configuration ──
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
ALLOWED_EMAIL_DOMAIN = "hemlockandoak.com"  # Only allow this domain

oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={
        'scope': 'openid email profile'
    }
)


# ── Jinja Template Filters ──
@app.template_filter('friendly_date')
def friendly_date_filter(value):
    """Format datetime as 'Dec 4th 2025 · 2:09pm'"""
    if not value:
        return "—"

    # If it's a string, parse it first
    if isinstance(value, str):
        try:
            value = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return value  # Return as-is if can't parse

    day = value.day
    if 11 <= day <= 13:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')

    month = value.strftime("%b")
    year = value.strftime("%Y")
    time = value.strftime("%-I:%M%p").lower()

    return f"{month} {day}{suffix} {year} · {time}"


# ── PostgreSQL connection settings (Neon) ──
DATABASE_URL = os.environ.get("DATABASE_URL", "")

def get_db_connection():
    """
    Create a fresh PostgreSQL connection with retry logic.
    Uses psycopg2 for Neon PostgreSQL.
    """
    max_retries = 3
    last_error = None

    for retry in range(max_retries):
        try:
            conn = psycopg2.connect(
                DATABASE_URL,
                cursor_factory=psycopg2.extras.RealDictCursor,
                connect_timeout=10
            )
            conn.autocommit = True
            return conn
        except psycopg2.OperationalError as e:
            last_error = e
            if retry < max_retries - 1:
                wait = min(2 ** retry, 4)
                print(f"⚠️ PostgreSQL connection error, retry {retry + 1}/{max_retries} after {wait}s: {e}")
                time.sleep(wait)
            else:
                print(f"❌ Failed to connect to PostgreSQL after {max_retries} retries: {e}")
                raise
        except Exception as e:
            last_error = e
            if retry < max_retries - 1:
                wait = min(2 ** retry, 4)
                print(f"⚠️ Database error, retry {retry + 1}/{max_retries} after {wait}s: {e}")
                time.sleep(wait)
            else:
                print(f"❌ Database connection error: {e}")
                raise

    if last_error:
        raise last_error

# Alias for compatibility with existing code
get_mysql_connection = get_db_connection


def execute_with_retry(query_func, max_retries=3):
    """
    Execute a database operation with retry logic for connection drops during query.

    Args:
        query_func: A function that takes (conn, cursor) and performs the database operation
        max_retries: Maximum number of retry attempts

    Returns:
        The result of query_func

    Example:
        def my_query(conn, cursor):
            cursor.execute("SELECT * FROM table")
            return cursor.fetchall()
        result = execute_with_retry(my_query)
    """
    last_error = None
    for attempt in range(max_retries):
        conn = None
        cursor = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            result = query_func(conn, cursor)
            return result
        except psycopg2.OperationalError as e:
            last_error = e
            if attempt < max_retries - 1:
                wait = min(2 ** attempt, 4)
                print(f"⚠️ Lost connection during query, retry {attempt + 1}/{max_retries} after {wait}s")
                time.sleep(wait)
                continue
            raise
        except psycopg2.InterfaceError as e:
            last_error = e
            if attempt < max_retries - 1:
                wait = min(2 ** attempt, 4)
                print(f"⚠️ Interface error, retry {attempt + 1}/{max_retries} after {wait}s: {e}")
                time.sleep(wait)
                continue
            raise
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

    if last_error:
        raise last_error


def fix_miscached_tracking_statuses():
    """
    One-time fix for tracking statuses that were cached with wrong mapping.
    UPS status code '012' (Clearance in Progress) was incorrectly mapped to 'delivered'.
    This corrects any cached entries with that mistake.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Find and fix entries where raw_status_code='012' but status='delivered'
        cursor.execute("""
            UPDATE tracking_status_cache
            SET status = 'in_transit',
                status_description = CASE
                    WHEN status_description LIKE '%Delivered%' THEN 'Clearance in Progress'
                    ELSE status_description
                END,
                is_delivered = false,
                updated_at = CURRENT_TIMESTAMP
            WHERE raw_status_code = '012' AND status = 'delivered'
            RETURNING tracking_number
        """)
        fixed = cursor.fetchall()
        conn.commit()

        if fixed:
            print(f"🔧 Fixed {len(fixed)} tracking entries with incorrect '012' status mapping")
            for row in fixed[:5]:  # Show first 5
                print(f"   - {row['tracking_number']}")
            if len(fixed) > 5:
                print(f"   ... and {len(fixed) - 5} more")

        cursor.close()
        conn.close()
    except Exception as e:
        print(f"⚠️ Error fixing cached tracking statuses: {e}")


# Run the fix at startup
fix_miscached_tracking_statuses()

# Read shop URL for building admin links
SHOP_URL = os.environ.get("SHOP_URL", "").rstrip("/")

# Read application password from environment (e.g. set APP_PASSWORD in Kinsta)
PASSWORD_HASH = os.environ["APP_PASSWORD_HASH"].encode()

# Read ShipStation credentials from environment
SHIPSTATION_API_KEY = os.environ.get("SHIPSTATION_API_KEY", "")
SHIPSTATION_API_SECRET = os.environ.get("SHIPSTATION_API_SECRET", "")
SHIPSTATION_V2_API_KEY = os.environ.get("SHIPSTATION_V2_API_KEY", "")

# ShipStation V2 API functions
def get_shipstation_batches(status="completed", page=1, page_size=25):
    """
    Fetch batches from ShipStation V2 API.
    Status can be: open, queued, completed, processing, archived, invalid, completed_with_errors
    """
    if not SHIPSTATION_V2_API_KEY:
        print("⚠️ ShipStation V2 API key not configured")
        return {"batches": [], "total": 0, "pages": 0}

    try:
        response = requests.get(
            "https://api.shipstation.com/v2/batches",
            headers={"API-Key": SHIPSTATION_V2_API_KEY},
            params={
                "status": status,
                "page": page,
                "page_size": page_size,
                "sort_by": "processed_at",
                "sort_dir": "desc"
            },
            timeout=30
        )

        if response.status_code == 200:
            return response.json()
        else:
            print(f"❌ ShipStation batches error: {response.status_code} - {response.text[:200]}")
            return {"batches": [], "total": 0, "pages": 0, "error": response.text[:200]}

    except Exception as e:
        print(f"❌ ShipStation batches exception: {e}")
        return {"batches": [], "total": 0, "pages": 0, "error": str(e)}


def get_shipstation_batch_shipments(batch_id):
    """
    Fetch ALL shipments for a specific ShipStation batch (handles pagination).
    """
    if not SHIPSTATION_V2_API_KEY:
        print("⚠️ ShipStation V2 API key not configured")
        return []

    all_shipments = []
    page = 1
    page_size = 100  # Max allowed by API

    try:
        while True:
            print(f"📦 Fetching batch {batch_id} shipments page {page}...")
            response = requests.get(
                f"https://api.shipstation.com/v2/shipments",
                headers={"API-Key": SHIPSTATION_V2_API_KEY},
                params={
                    "batch_id": batch_id,
                    "page": page,
                    "page_size": page_size
                },
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                shipments = data.get("shipments", [])
                all_shipments.extend(shipments)

                # Check if there are more pages
                total_pages = data.get("pages", 1)
                if page >= total_pages or len(shipments) == 0:
                    break
                page += 1
            else:
                print(f"❌ ShipStation batch shipments error: {response.status_code} - {response.text[:200]}")
                break

        print(f"📦 Fetched {len(all_shipments)} total shipments for batch {batch_id}")
        return all_shipments

    except Exception as e:
        print(f"❌ ShipStation batch shipments exception: {e}")
        return all_shipments  # Return what we got so far

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

# ── Canada Post singleton ──
_canadapost_api = None
def get_canadapost_api():
    global _canadapost_api
    if _canadapost_api is None:
        _canadapost_api = CanadaPostAPI()
    return _canadapost_api


# ══════════════════════════════════════════════════════════════════════════════
# ── Background Tracking Refresh Scheduler ──
# ══════════════════════════════════════════════════════════════════════════════
_scheduler = None
_scheduler_initialized = False

def init_background_scheduler():
    """
    Initialize background scheduler for automatic tracking updates.
    Runs every 30 minutes to refresh stale tracking statuses.
    """
    global _scheduler, _scheduler_initialized

    # Prevent multiple initializations (important with Flask debug reloader)
    if _scheduler_initialized:
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        _scheduler = BackgroundScheduler(daemon=True)

        # Schedule tracking refresh every 30 minutes
        _scheduler.add_job(
            func=background_tracking_refresh,
            trigger=IntervalTrigger(minutes=30),
            id='tracking_refresh',
            name='Refresh stale tracking statuses',
            replace_existing=True,
            max_instances=1  # Prevent overlapping executions
        )

        _scheduler.start()
        _scheduler_initialized = True
        print("✅ Background tracking scheduler started (30 min interval)")

        # Shut down scheduler when app stops
        atexit.register(lambda: _scheduler.shutdown(wait=False) if _scheduler else None)

    except ImportError:
        print("⚠️ APScheduler not installed - background tracking disabled")
    except Exception as e:
        print(f"⚠️ Failed to start background scheduler: {e}")


def background_tracking_refresh():
    """
    Background job to refresh stale tracking statuses.
    Finds non-delivered shipments with stale tracking and updates them.
    """
    print("\n🔄 [Background] Starting tracking status refresh...")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Find tracking numbers that:
        # 1. Are not delivered
        # 2. Have cache older than 4 hours OR no cache
        # 3. Were shipped in the last 30 days (ignore old ones)
        cursor.execute("""
            SELECT DISTINCT sc.tracking_number, sc.carrier_code
            FROM shipments_cache sc
            LEFT JOIN tracking_status_cache tc ON sc.tracking_number = tc.tracking_number
            WHERE sc.ship_date >= CURRENT_DATE - INTERVAL '30 days'
              AND (tc.status IS NULL
                   OR tc.status NOT IN ('delivered', 'exception')
                   OR tc.updated_at < NOW() - INTERVAL '4 hours')
              AND sc.tracking_number IS NOT NULL
              AND sc.tracking_number != ''
            ORDER BY tc.updated_at ASC NULLS FIRST
            LIMIT 100
        """)

        stale_shipments = cursor.fetchall()
        cursor.close()
        conn.close()

        if not stale_shipments:
            print("✅ [Background] No stale tracking to refresh")
            return

        # Split by carrier
        ups_tracking = [s['tracking_number'] for s in stale_shipments
                       if s['tracking_number'].startswith('1Z')]
        cp_tracking = [s['tracking_number'] for s in stale_shipments
                      if 'canada' in (s.get('carrier_code') or '').lower()
                      and not s['tracking_number'].startswith('1Z')]

        print(f"📦 [Background] Found {len(stale_shipments)} stale: {len(ups_tracking)} UPS, {len(cp_tracking)} Canada Post")

        # Update in batches (respecting API rate limits)
        if ups_tracking:
            print(f"🔄 [Background] Refreshing {min(len(ups_tracking), 30)} UPS tracking numbers...")
            update_ups_tracking_cache(ups_tracking[:30], force_refresh=True)

        if cp_tracking:
            print(f"🔄 [Background] Refreshing {min(len(cp_tracking), 20)} Canada Post tracking numbers...")
            update_canadapost_tracking_cache(cp_tracking[:20], force_refresh=True)

        print("✅ [Background] Tracking refresh complete\n")

    except Exception as e:
        print(f"❌ [Background] Tracking refresh error: {e}")
        import traceback
        traceback.print_exc()


# ── Orders Sync singleton ──
_orders_sync = None
def get_orders_sync():
    global _orders_sync
    if _orders_sync is None:
        _orders_sync = OrdersSync(get_shopify_api(), get_db_connection)
    return _orders_sync

# ── Stats Cache (5 minute TTL) ──
_stats_cache = {
    "data": None,
    "expires_at": 0
}
STATS_CACHE_TTL = 300  # 5 minutes

def get_cached_stats():
    """Get stats from cache if still valid, else return None."""
    if _stats_cache["data"] and time.time() < _stats_cache["expires_at"]:
        return _stats_cache["data"]
    return None

def set_cached_stats(stats):
    """Store stats in cache with TTL."""
    _stats_cache["data"] = stats.copy()
    _stats_cache["expires_at"] = time.time() + STATS_CACHE_TTL

def invalidate_stats_cache():
    """Clear the stats cache (call after scans or status updates)."""
    _stats_cache["data"] = None
    _stats_cache["expires_at"] = 0

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
                id SERIAL PRIMARY KEY,
                tracking_number VARCHAR(255) NOT NULL,
                order_number VARCHAR(255),
                customer_name VARCHAR(255),
                carrier_code VARCHAR(50),
                ship_date DATE,
                shipstation_batch_number VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Ensure unique constraint exists (for ON CONFLICT to work)
        try:
            cursor.execute("""
                ALTER TABLE shipments_cache
                ADD CONSTRAINT shipments_cache_tracking_unique UNIQUE (tracking_number)
            """)
        except Exception:
            pass  # Constraint already exists
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_shipments_tracking ON shipments_cache(tracking_number)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_shipments_ship_date ON shipments_cache(ship_date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_shipments_order ON shipments_cache(order_number)")
        conn.commit()
        cursor.close()
        conn.close()
        print("✓ Shipments cache table initialized")
    except Exception as e:
        print(f"❌ Error initializing shipments cache: {e}")


def init_tracking_status_cache():
    """
    Initialize the tracking_status_cache table for UPS tracking data.
    This avoids calling UPS API on every page load.
    """
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tracking_status_cache (
                id SERIAL PRIMARY KEY,
                tracking_number VARCHAR(255) NOT NULL,
                carrier VARCHAR(50) DEFAULT 'UPS',
                status VARCHAR(50),
                status_description VARCHAR(500),
                estimated_delivery VARCHAR(255),
                last_location VARCHAR(255),
                last_activity_date VARCHAR(50),
                is_delivered BOOLEAN DEFAULT FALSE,
                raw_status_code VARCHAR(50),
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Ensure unique constraint exists (for ON CONFLICT to work)
        try:
            cursor.execute("""
                ALTER TABLE tracking_status_cache
                ADD CONSTRAINT tracking_status_cache_tracking_unique UNIQUE (tracking_number)
            """)
        except Exception:
            pass  # Constraint already exists
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tracking_status ON tracking_status_cache(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tracking_delivered ON tracking_status_cache(is_delivered)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tracking_updated ON tracking_status_cache(updated_at)")
        conn.commit()
        cursor.close()
        conn.close()
        print("✓ Tracking status cache table initialized")
    except Exception as e:
        print(f"❌ Error initializing tracking status cache: {e}")


def normalize_table_collations():
    """
    Previously used to normalize MySQL collations.
    Not needed for PostgreSQL - keeping function as no-op for compatibility.
    """
    print("✓ Using PostgreSQL - collation normalization not needed")
    pass


def update_ups_tracking_cache(tracking_numbers, force_refresh=False):
    """
    Update UPS tracking cache for given tracking numbers.
    Only updates entries older than 2 hours unless force_refresh=True.
    """
    if not tracking_numbers:
        print("⚠️ update_ups_tracking_cache called with no tracking numbers")
        return

    ups_api = get_ups_api()
    if not ups_api.enabled:
        print("⚠️ UPS API is not enabled (missing credentials?)")
        return

    print(f"📦 update_ups_tracking_cache called with {len(tracking_numbers)} numbers, force_refresh={force_refresh}")

    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
    except Exception as e:
        print(f"❌ Failed to get MySQL connection in update_ups_tracking_cache: {e}")
        return

    try:
        # Filter to only UPS tracking numbers (start with 1Z)
        ups_tracking = [t for t in tracking_numbers if t and t.startswith("1Z")]
        if not ups_tracking:
            print("⚠️ No valid 1Z tracking numbers to update")
            return

        # Check which ones need updating (older than 2 hours or not in cache)
        if force_refresh:
            to_update = ups_tracking
            print(f"🔄 Force refresh: will update all {len(to_update)} tracking numbers")
        else:
            placeholders = ",".join(["%s"] * len(ups_tracking))
            cursor.execute(f"""
                SELECT tracking_number, updated_at FROM tracking_status_cache
                WHERE tracking_number IN ({placeholders})
            """, ups_tracking)
            cached = {row["tracking_number"]: row["updated_at"] for row in cursor.fetchall()}

            cutoff = datetime.now() - timedelta(hours=2)
            to_update = []
            for tn in ups_tracking:
                if tn not in cached:
                    to_update.append(tn)
                elif cached[tn] < cutoff:
                    to_update.append(tn)

        if not to_update:
            print(f"✓ All {len(ups_tracking)} tracking numbers are cached and fresh")
            return

        print(f"🔄 Updating UPS tracking cache for {len(to_update)} tracking numbers...")
        updated_count = 0
        error_count = 0

        for i, tracking_number in enumerate(to_update[:50]):  # Increased to 50 with rate limiting
            try:
                # Add delay between requests to avoid UPS rate limiting
                if i > 0:
                    time.sleep(0.3)  # 300ms delay between requests

                result = ups_api.get_tracking_status(tracking_number)

                if result.get("status") == "error":
                    print(f"⚠️ UPS API error for {tracking_number}: {result.get('error', 'Unknown error')}")
                    error_count += 1
                    continue

                print(f"✅ UPS {tracking_number}: status={result.get('status')}, est={result.get('estimated_delivery', 'N/A')}")

                cursor.execute("""
                    INSERT INTO tracking_status_cache
                    (tracking_number, carrier, status, status_description, estimated_delivery,
                     last_location, last_activity_date, is_delivered, raw_status_code, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (tracking_number) DO UPDATE SET
                        status = EXCLUDED.status,
                        status_description = EXCLUDED.status_description,
                        estimated_delivery = EXCLUDED.estimated_delivery,
                        last_location = EXCLUDED.last_location,
                        last_activity_date = EXCLUDED.last_activity_date,
                        is_delivered = EXCLUDED.is_delivered,
                        raw_status_code = EXCLUDED.raw_status_code,
                        updated_at = CURRENT_TIMESTAMP
                """, (
                    tracking_number,
                    "UPS",
                    result.get("status", "unknown"),
                    result.get("status_description", "")[:500] if result.get("status_description") else "",
                    result.get("estimated_delivery", ""),
                    result.get("location", ""),
                    result.get("last_activity", ""),
                    result.get("status") == "delivered",
                    result.get("raw_status_code", "")
                ))
                conn.commit()
                updated_count += 1
            except Exception as e:
                print(f"⚠️ Error caching tracking for {tracking_number}: {e}")
                error_count += 1

        print(f"✓ UPS tracking cache update complete: {updated_count} updated, {error_count} errors")

    except Exception as e:
        print(f"❌ Error updating tracking cache: {e}")
    finally:
        cursor.close()
        conn.close()


def update_canadapost_tracking_cache(tracking_numbers, force_refresh=False):
    """
    Update Canada Post tracking cache for given tracking numbers.
    Only updates entries older than 2 hours unless force_refresh=True.
    """
    if not tracking_numbers:
        print("⚠️ update_canadapost_tracking_cache called with no tracking numbers")
        return

    cp_api = get_canadapost_api()
    if not cp_api.enabled:
        print("⚠️ Canada Post API is not enabled (missing credentials?)")
        return

    print(f"📮 update_canadapost_tracking_cache called with {len(tracking_numbers)} numbers, force_refresh={force_refresh}")

    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
    except Exception as e:
        print(f"❌ Failed to get connection in update_canadapost_tracking_cache: {e}")
        return

    try:
        # Filter to Canada Post tracking numbers (typically start with digits, are 12-16 chars)
        # Canada Post PINs are typically numeric and 12, 13, or 16 digits
        cp_tracking = [t for t in tracking_numbers if t and len(t) >= 12 and not t.startswith("1Z")]
        if not cp_tracking:
            print("⚠️ No valid Canada Post tracking numbers to update")
            return

        # Check which ones need updating (older than 2 hours or not in cache)
        if force_refresh:
            to_update = cp_tracking
            print(f"🔄 Force refresh: will update all {len(to_update)} tracking numbers")
        else:
            placeholders = ",".join(["%s"] * len(cp_tracking))
            cursor.execute(f"""
                SELECT tracking_number, updated_at FROM tracking_status_cache
                WHERE tracking_number IN ({placeholders})
            """, cp_tracking)
            cached = {row["tracking_number"]: row["updated_at"] for row in cursor.fetchall()}

            cutoff = datetime.now() - timedelta(hours=2)
            to_update = []
            for tn in cp_tracking:
                if tn not in cached:
                    to_update.append(tn)
                elif cached[tn] and cached[tn].replace(tzinfo=None) < cutoff:
                    to_update.append(tn)

        if not to_update:
            print(f"✓ All {len(cp_tracking)} Canada Post tracking numbers are cached and fresh")
            return

        print(f"🔄 Updating Canada Post tracking cache for {len(to_update)} tracking numbers...")
        updated_count = 0
        error_count = 0

        for i, tracking_number in enumerate(to_update[:30]):  # Limit to 30 at a time
            try:
                # Add delay between requests to avoid rate limiting
                if i > 0:
                    time.sleep(0.5)  # 500ms delay between requests

                result = cp_api.get_tracking_summary(tracking_number)

                if result.get("status") == "error":
                    print(f"⚠️ Canada Post API error for {tracking_number}: {result.get('error', 'Unknown error')}")
                    error_count += 1
                    continue

                print(f"✅ CP {tracking_number}: status={result.get('status')}, desc={result.get('status_description', 'N/A')[:30]}")

                cursor.execute("""
                    INSERT INTO tracking_status_cache
                    (tracking_number, carrier, status, status_description, estimated_delivery,
                     last_location, last_activity_date, is_delivered, raw_status_code, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (tracking_number) DO UPDATE SET
                        status = EXCLUDED.status,
                        status_description = EXCLUDED.status_description,
                        estimated_delivery = EXCLUDED.estimated_delivery,
                        last_location = EXCLUDED.last_location,
                        last_activity_date = EXCLUDED.last_activity_date,
                        is_delivered = EXCLUDED.is_delivered,
                        raw_status_code = EXCLUDED.raw_status_code,
                        updated_at = CURRENT_TIMESTAMP
                """, (
                    tracking_number,
                    "Canada Post",
                    result.get("status", "unknown"),
                    result.get("status_description", "")[:500] if result.get("status_description") else "",
                    result.get("estimated_delivery", ""),
                    result.get("location", ""),
                    result.get("last_activity", ""),
                    result.get("status") == "delivered",
                    result.get("raw_status_code", "")
                ))
                conn.commit()
                updated_count += 1
            except Exception as e:
                print(f"⚠️ Error caching Canada Post tracking for {tracking_number}: {e}")
                error_count += 1

        print(f"✓ Canada Post tracking cache update complete: {updated_count} updated, {error_count} errors")

    except Exception as e:
        print(f"❌ Error updating Canada Post tracking cache: {e}")
    finally:
        cursor.close()
        conn.close()


# Initialize cache tables on startup
init_shipments_cache()
init_tracking_status_cache()
init_orders_tables(get_db_connection)
normalize_table_collations()

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
                    (tracking_number, order_number, customer_name, carrier_code, ship_date, shipstation_batch_number, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (tracking_number) DO UPDATE SET
                        order_number = EXCLUDED.order_number,
                        customer_name = EXCLUDED.customer_name,
                        carrier_code = EXCLUDED.carrier_code,
                        ship_date = EXCLUDED.ship_date,
                        shipstation_batch_number = EXCLUDED.shipstation_batch_number,
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
        cursor = conn.cursor()

        # Find scans that might have concatenated tracking numbers
        cursor.execute("""
            SELECT id, tracking_number, carrier, order_number, customer_name,
                   customer_email, batch_id, scan_date, status, order_id,
                   shipstation_batch_number
            FROM scans
            WHERE (
                LENGTH(tracking_number) = 36 OR   -- Two UPS
                LENGTH(tracking_number) = 56 OR   -- Two Canada Post (28 chars each)
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
        cursor = conn.cursor()

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

def refresh_ups_tracking_background():
    """
    Background function to refresh UPS tracking for non-delivered shipments.
    Only updates shipments from the last 30 days that aren't marked delivered.
    """
    print("🚚 Starting background UPS tracking refresh...")
    try:
        ups_api = get_ups_api()
        if not ups_api.enabled:
            print("⚠️ UPS API not enabled, skipping tracking refresh")
            return

        conn = get_mysql_connection()
        cursor = conn.cursor()

        # Find UPS tracking numbers that need refresh:
        # - Shipped in last 30 days
        # - Not yet delivered OR marked delivered recently (to verify/catch errors)
        # - Haven't been updated in last 2 hours
        cursor.execute("""
            SELECT sc.tracking_number
            FROM shipments_cache sc
            LEFT JOIN tracking_status_cache tc ON tc.tracking_number = sc.tracking_number
            WHERE sc.carrier_code = 'UPS'
              AND sc.ship_date >= CURRENT_DATE - INTERVAL '30 days'
              AND (
                  tc.is_delivered = false
                  OR tc.is_delivered IS NULL
                  OR (tc.is_delivered = true AND tc.updated_at > NOW() - INTERVAL '24 hours')
              )
              AND (tc.updated_at IS NULL OR tc.updated_at < NOW() - INTERVAL '2 hours')
            ORDER BY sc.ship_date DESC
            LIMIT 100
        """)
        to_refresh = [row['tracking_number'] for row in cursor.fetchall()]
        cursor.close()
        conn.close()

        if to_refresh:
            print(f"🚚 Refreshing {len(to_refresh)} UPS tracking statuses in background...")
            update_ups_tracking_cache(to_refresh, force_refresh=True)
            print(f"✅ Background UPS tracking refresh complete")
        else:
            print("✓ No UPS tracking needs refresh")

    except Exception as e:
        print(f"❌ Error in background UPS tracking refresh: {e}")


def refresh_canadapost_tracking_background():
    """
    Background function to refresh Canada Post tracking for non-delivered shipments.
    Only updates shipments from the last 30 days that aren't marked delivered.
    """
    print("📮 Starting background Canada Post tracking refresh...")
    try:
        cp_api = get_canadapost_api()
        if not cp_api.enabled:
            print("⚠️ Canada Post API not enabled, skipping tracking refresh")
            return

        conn = get_mysql_connection()
        cursor = conn.cursor()

        # Find Canada Post tracking numbers that need refresh
        # Canada Post carrier codes might be variations like "canada_post", "canadapost", etc.
        cursor.execute("""
            SELECT sc.tracking_number
            FROM shipments_cache sc
            LEFT JOIN tracking_status_cache tc ON tc.tracking_number = sc.tracking_number
            WHERE LOWER(sc.carrier_code) LIKE '%canada%'
              AND sc.ship_date >= CURRENT_DATE - INTERVAL '30 days'
              AND (
                  tc.is_delivered = false
                  OR tc.is_delivered IS NULL
                  OR (tc.is_delivered = true AND tc.updated_at > NOW() - INTERVAL '24 hours')
              )
              AND (tc.updated_at IS NULL OR tc.updated_at < NOW() - INTERVAL '2 hours')
            ORDER BY sc.ship_date DESC
            LIMIT 50
        """)
        to_refresh = [row['tracking_number'] for row in cursor.fetchall()]
        cursor.close()
        conn.close()

        if to_refresh:
            print(f"📮 Refreshing {len(to_refresh)} Canada Post tracking statuses in background...")
            update_canadapost_tracking_cache(to_refresh, force_refresh=True)
            print(f"✅ Background Canada Post tracking refresh complete")
        else:
            print("✓ No Canada Post tracking needs refresh")

    except Exception as e:
        print(f"❌ Error in background Canada Post tracking refresh: {e}")


def start_background_sync():
    """
    Start background thread that syncs shipments every 5 minutes.
    Also runs UPS and Canada Post tracking refresh every 15 minutes.
    Also runs email backfill and split tracking backfill on startup and once per day.
    Also syncs orders from Shopify every 5 minutes (incremental).
    """
    def sync_loop():
        # Run backfills immediately on startup
        backfill_split_tracking_numbers()  # First, split any concatenated tracking numbers
        backfill_missing_emails()  # Then, fill missing emails for all scans (including newly split ones)

        last_backfill = datetime.now()
        tracking_refresh_counter = 0  # Track cycles for tracking refresh

        while True:
            sync_shipments_from_shipstation()

            # Sync orders from Shopify (incremental sync)
            try:
                orders_sync = get_orders_sync()
                orders_sync.sync_orders(full_sync=False)
            except Exception as e:
                print(f"❌ Orders sync error: {e}")

            # Run tracking refresh every 15 minutes (every 3rd cycle)
            tracking_refresh_counter += 1
            if tracking_refresh_counter >= 3:
                refresh_ups_tracking_background()
                refresh_canadapost_tracking_background()
                tracking_refresh_counter = 0

            # Run backfills once per day
            if (datetime.now() - last_backfill).total_seconds() > 86400:  # 24 hours
                backfill_split_tracking_numbers()
                backfill_missing_emails()
                last_backfill = datetime.now()

            time.sleep(120)  # 2 minutes

    thread = threading.Thread(target=sync_loop, daemon=True)
    thread.start()
    print("✓ Background shipments sync started (every 2 minutes)")
    print("✓ Background orders sync from Shopify started (every 2 minutes)")
    print("✓ Background UPS tracking refresh started (every 15 minutes)")
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
        cursor = conn.cursor()

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
      text-decoration: none;
    }
    .login-container .btn:hover {
      opacity: 0.92;
    }
    .google-btn {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 10px;
      width: 80%;
      margin: 0 auto 20px auto;
      padding: 10px 0;
      font-size: 1rem;
      background-color: #fff;
      color: #333;
      border: 1px solid #ddd;
      border-radius: 4px;
      cursor: pointer;
      text-decoration: none;
      transition: background-color 0.2s;
    }
    .google-btn:hover {
      background-color: #f5f5f5;
    }
    .google-btn svg {
      width: 18px;
      height: 18px;
    }
    .divider {
      display: flex;
      align-items: center;
      margin: 20px 0;
      color: #999;
      font-size: 0.85rem;
    }
    .divider::before, .divider::after {
      content: "";
      flex: 1;
      border-bottom: 1px solid #ddd;
    }
    .divider span {
      padding: 0 10px;
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
    .domain-note {
      font-size: 0.8rem;
      color: #888;
      margin-top: 8px;
    }
  </style>
</head>
<body>
  <div class="login-container">
    <h2>Hemlock &amp; Oak</h2>

    {% with messages = get_flashed_messages(with_categories=true) %}
      {% for category, msg in messages %}
        <div class="flash">{{ msg }}</div>
      {% endfor %}
    {% endwith %}

    <!-- Google Sign-In Button -->
    {% if google_enabled %}
    <a href="{{ url_for('google_login') }}" class="google-btn">
      <svg viewBox="0 0 24 24">
        <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
        <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
        <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
        <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
      </svg>
      Sign in with Google
    </a>
    <p class="domain-note">Use your @hemlockandoak.com email</p>

    <div class="divider"><span>or</span></div>
    {% endif %}

    <!-- Password Login -->
    <form action="{{ url_for('login') }}" method="post">
      <input type="password" name="password" placeholder="Password" required>
      <button type="submit" class="btn">Log In with Password</button>
    </form>
  </div>
</body>
</html>
'''

# ─────────────────────────────────────────────────────────────────────────────
# ── BEFORE REQUEST: require login ──────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@app.before_request
def require_login():
    # always allow login, OAuth routes & static assets
    allowed_endpoints = ("login", "google_login", "google_callback", "static", "favicon")
    if request.endpoint in allowed_endpoints:
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
            session["auth_method"] = "password"
            return redirect(url_for("index"))
        else:
            flash("Invalid password. Please try again.", "error")
    # Show Google button only if OAuth is configured
    google_enabled = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
    return render_template_string(LOGIN_TEMPLATE, google_enabled=google_enabled)


@app.route("/auth/google")
def google_login():
    """Redirect to Google for authentication."""
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        flash("Google authentication is not configured.", "error")
        return redirect(url_for("login"))

    # Build the redirect URI
    redirect_uri = url_for("google_callback", _external=True)
    return google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def google_callback():
    """Handle Google OAuth callback."""
    try:
        token = google.authorize_access_token()
        user_info = token.get('userinfo')

        if not user_info:
            flash("Could not retrieve user information from Google.", "error")
            return redirect(url_for("login"))

        email = user_info.get('email', '')
        name = user_info.get('name', '')

        # Check email domain restriction
        if not email.endswith(f"@{ALLOWED_EMAIL_DOMAIN}"):
            flash(f"Access denied. Only @{ALLOWED_EMAIL_DOMAIN} emails are allowed.", "error")
            print(f"OAuth login denied for email: {email} (not in {ALLOWED_EMAIL_DOMAIN})")
            return redirect(url_for("login"))

        # Successful authentication
        session.clear()
        session["authenticated"] = True
        session["last_active"] = time.time()
        session["auth_method"] = "google"
        session["user_email"] = email
        session["user_name"] = name

        print(f"✓ Google OAuth login successful: {email}")
        flash(f"Welcome, {name}!", "success")
        return redirect(url_for("index"))

    except Exception as e:
        print(f"Google OAuth error: {e}")
        flash("Authentication failed. Please try again.", "error")
        return redirect(url_for("login"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
def index():
    batch_id = session.get("batch_id")
    if not batch_id:
        # No batch open → show "Create New Batch"
        return render_template(
            "new_batch.html",
            current_batch=None,
            scans=[],
            shop_url=SHOP_URL,
            version=__version__,
            active_page="new_batch"
        )

    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()

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
            COALESCE(status, '') as status,
            order_id
          FROM scans
         WHERE batch_id = %s
         ORDER BY scan_date DESC
        """, (batch_id,))
        scans = cursor.fetchall()

        return render_template(
            "new_batch.html",
            current_batch=batch_row,
            scans=scans,
            shop_url=SHOP_URL,
            version=__version__,
            active_page="new_batch"
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

    created_at = now_pst().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
          INSERT INTO batches (created_at, pkg_count, tracking_numbers, carrier)
          VALUES (%s, %s, %s, %s)
          RETURNING id
        """, (created_at, 0, "", carrier))
        result = cursor.fetchone()
        batch_id = result['id'] if result else None
        conn.commit()

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
    except psycopg2.Error as e:
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

        # ── LOCAL DATABASE LOOKUP FIRST (fastest, no API calls) ──
        local_found = False
        try:
            cursor = conn.cursor()

            # First try orders table (Shopify data - has email)
            cursor.execute("""
                SELECT order_number, customer_name, customer_email, shopify_order_id
                FROM orders
                WHERE tracking_number = %s
                LIMIT 1
            """, (tracking_number,))
            local_order = cursor.fetchone()

            if local_order and local_order.get('order_number'):
                local_found = True
                order_number = local_order.get('order_number', 'N/A')
                customer_name = local_order.get('customer_name', 'Not Found')
                customer_email = local_order.get('customer_email', '')
                order_id = local_order.get('shopify_order_id', '')
                print(f"✅ LOCAL DB (orders): Found order {order_number} for {tracking_number}")

            # If not found in orders, try shipments_cache (ShipStation data)
            if not local_found:
                cursor.execute("""
                    SELECT order_number, customer_name, carrier_code, shipstation_batch_number
                    FROM shipments_cache
                    WHERE tracking_number = %s
                    LIMIT 1
                """, (tracking_number,))
                cached_shipment = cursor.fetchone()

                if cached_shipment and cached_shipment.get('order_number'):
                    local_found = True
                    order_number = cached_shipment.get('order_number', 'N/A')
                    customer_name = cached_shipment.get('customer_name', 'Not Found')
                    shipstation_batch_number = cached_shipment.get('shipstation_batch_number', '')
                    carrier_code = cached_shipment.get('carrier_code', '')
                    if carrier_code:
                        carrier_map = {"ups": "UPS", "canadapost": "Canada Post", "canada_post": "Canada Post",
                                       "dhl": "DHL", "dhl_express": "DHL", "purolator": "Purolator"}
                        scan_carrier = carrier_map.get(carrier_code.lower(), scan_carrier)
                    print(f"✅ LOCAL DB (shipments_cache): Found order {order_number} for {tracking_number}")

            cursor.close()
        except Exception as e:
            print(f"Local DB lookup error for {tracking_number}: {e}")

        # ── ShipStation lookup with retry logic (skip if local found) ──
        shipstation_found = False
        if not local_found and SHIPSTATION_API_KEY and SHIPSTATION_API_SECRET:
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

        # ── Shopify lookup (skip if local found) ──
        shopify_found = False
        if not local_found:
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
            elif len(tracking_number) == 16:
                # Canada Post: After normalization, 28-char barcode becomes 16-char tracking number
                scan_carrier = "Canada Post"
                print(f"📮 Detected Canada Post by 16-char length: {tracking_number}")
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
        batch_carrier = (row['carrier'] if row else "") or ""

        # ── STRICT CARRIER VALIDATION (BEFORE normalization) ──
        # Must match the batch carrier's expected format, reject everything else
        validation_error = None

        if batch_carrier == "UPS":
            # UPS: MUST start with "1Z"
            if not code.startswith("1Z"):
                validation_error = f"❌ Not a UPS label! UPS tracking numbers must start with '1Z'. (Scanned: {code[:20]}...)"

        elif batch_carrier == "Canada Post":
            # Canada Post formats:
            # - 28 chars: full barcode from label (most common from scanner)
            # - 22 chars: some label formats
            # - 16 chars: normalized tracking number
            # - 13 chars: international (e.g., RR123456789CA)
            valid_cp_lengths = [28, 22, 16, 13]
            if len(code) not in valid_cp_lengths:
                validation_error = f"❌ Not a Canada Post label! Expected 28, 22, 16, or 13 characters. (Scanned: {code[:20]}... - Length: {len(code)})"

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
            # Canada Post normalization based on barcode length
            if len(code) == 28:
                # Full barcode: extract middle 16 chars
                code = code[7:-5]
                print(f"📮 Canada Post: Normalized 28-char {original_code} -> {code}")
            elif len(code) == 22:
                # Some label formats: extract middle 16 chars
                code = code[3:-3]
                print(f"📮 Canada Post: Normalized 22-char {original_code} -> {code}")
            elif len(code) == 16:
                # Already normalized, use as-is
                print(f"📮 Canada Post: Already 16-char format {code}")
            elif len(code) == 13:
                # International format (RR123456789CA), use as-is
                print(f"📮 Canada Post: International format {code}")
        elif batch_carrier == "Purolator":
            if len(code) == 34:
                code = code[11:-11]

        # ─────────────────────────────────────────────────────────────────
        # ✨ CHECK FOR CANCELLED ORDERS (BEFORE duplicate check)
        # ─────────────────────────────────────────────────────────────────
        cursor = conn.cursor()

        # First, find the order number for this tracking number from multiple sources
        order_number_for_tracking = None

        # Check orders table (synced from Shopify)
        cursor.execute(
            "SELECT order_number FROM orders WHERE tracking_number = %s LIMIT 1",
            (code,)
        )
        order_row = cursor.fetchone()
        if order_row:
            order_number_for_tracking = order_row.get('order_number')

        # If not found, check scans table (from previous scans)
        if not order_number_for_tracking:
            cursor.execute(
                """
                SELECT order_number FROM scans
                WHERE tracking_number = %s AND order_number IS NOT NULL
                  AND order_number NOT IN ('Processing...', 'N/A', '')
                ORDER BY scan_date DESC LIMIT 1
                """,
                (code,)
            )
            scan_row = cursor.fetchone()
            if scan_row:
                order_number_for_tracking = scan_row.get('order_number')

        # Now check if this order is in cancelled_orders table
        cancelled = None
        if order_number_for_tracking:
            cursor.execute(
                "SELECT order_number, reason FROM cancelled_orders WHERE order_number = %s",
                (order_number_for_tracking,)
            )
            cancelled = cursor.fetchone()

        # Also check by tracking number directly in cancelled_orders (in case it's stored there)
        if not cancelled:
            cursor.execute(
                "SELECT order_number, reason FROM cancelled_orders WHERE tracking_number = %s",
                (code,)
            )
            cancelled = cursor.fetchone()

        # Also check orders table for Shopify-cancelled orders
        if not cancelled and order_number_for_tracking:
            cursor.execute(
                """
                SELECT order_number, cancel_reason FROM orders
                WHERE order_number = %s AND cancelled_at IS NOT NULL
                """,
                (order_number_for_tracking,)
            )
            shopify_cancelled = cursor.fetchone()
            if shopify_cancelled:
                cancelled = {
                    'order_number': shopify_cancelled.get('order_number'),
                    'reason': shopify_cancelled.get('cancel_reason') or 'Cancelled in Shopify'
                }

        if cancelled:
            cancel_order = cancelled.get('order_number', 'Unknown')
            cancel_reason = cancelled.get('reason', 'Order cancelled')
            error_msg = f"🚫 CANCELLED ORDER #{cancel_order} - DO NOT SHIP!\nReason: {cancel_reason}"
            print(f"🚫 BLOCKED CANCELLED ORDER: {cancel_order} (tracking: {code})")

            cursor.close()
            conn.close()

            if is_ajax:
                return jsonify({
                    "success": False,
                    "error": error_msg,
                    "cancelled_order": True,
                    "order_number": cancel_order,
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
        
        now_str = now_pst().strftime("%Y-%m-%d %H:%M:%S")

        # ── INSERT IMMEDIATELY (no waiting for APIs) ──
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO scans
              (tracking_number, carrier, order_number, customer_name,
               scan_date, status, order_id, customer_email, batch_id, shipstation_batch_number)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (code, scan_carrier, order_number, customer_name,
             now_str, status, order_id, "", batch_id, "")
        )
        result = cursor.fetchone()
        scan_id = result['id'] if result else None
        conn.commit()
        cursor.close()

        # Invalidate stats cache when new scan is recorded
        invalidate_stats_cache()

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

    except psycopg2.OperationalError as e:
        error_msg = "Database connection pool exhausted - please wait a moment and try again"
        print(f"Pool exhaustion during scan: {e}")
        if is_ajax:
            return jsonify({"success": False, "error": error_msg}), 503
        flash(error_msg, "error")
        return redirect(url_for("index"))
    except psycopg2.Error as e:
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
    except psycopg2.OperationalError:
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
    except psycopg2.Error as e:
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


@app.route("/resolve_duplicate/<int:scan_id>", methods=["POST"])
def resolve_duplicate(scan_id):
    """Mark a duplicate scan as resolved (change status to 'Complete')."""
    try:
        conn = get_mysql_connection()
    except psycopg2.OperationalError:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"success": False, "error": "Database busy, try again"}), 503
        flash("Database connection pool busy - please try again", "error")
        return redirect(url_for("index"))
    except Exception as e:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"success": False, "error": str(e)}), 500
        flash(f"Database error: {e}", "error")
        return redirect(url_for("index"))

    try:
        cursor = conn.cursor()
        # Update the scan status from "Duplicate (Batch #X)" to "Complete"
        cursor.execute(
            """
            UPDATE scans
            SET status = 'Complete'
            WHERE id = %s AND status LIKE 'Duplicate%%'
            """,
            (scan_id,)
        )
        rows_affected = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()

        if rows_affected > 0:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"success": True, "message": "Duplicate resolved"})
            flash("Duplicate resolved - scan marked as Complete", "success")
        else:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({"success": False, "error": "Scan not found or not a duplicate"})
            flash("Scan not found or not a duplicate", "error")

        return redirect(url_for("index"))

    except Exception as e:
        print(f"Error resolving duplicate: {e}")
        try:
            conn.close()
        except:
            pass
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"success": False, "error": str(e)}), 500
        flash(f"Error: {e}", "error")
        return redirect(url_for("index"))


@app.route("/delete_scan", methods=["POST"])
def delete_scan():
    scan_id = request.form.get("scan_id")
    if not scan_id:
        flash("No scan specified for deletion.", "error")
        return redirect(url_for("all_scans"))

    try:
        conn = get_mysql_connection()
    except psycopg2.OperationalError:
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
    except psycopg2.Error as e:
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


@app.route("/mark_batch_picked_up", methods=["POST"])
def mark_batch_picked_up():
    """Mark a batch as picked up from the all_batches page (for old batches without status)."""
    batch_id = request.form.get("batch_id")
    if not batch_id:
        flash("No batch specified.", "error")
        return redirect(url_for("all_batches"))

    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
          SELECT tracking_number
            FROM scans
           WHERE batch_id = %s
        """, (batch_id,))
        rows = cursor.fetchall()
        # Filter out NULL tracking numbers
        tracking_list = [row["tracking_number"] for row in rows if row["tracking_number"]]
        pkg_count = len(tracking_list)
        tracking_csv = ",".join(tracking_list) if tracking_list else ""

        cursor.execute("""
          UPDATE batches
             SET pkg_count = %s,
                 tracking_numbers = %s,
                 status = 'recorded'
           WHERE id = %s
        """, (pkg_count, tracking_csv, batch_id))
        conn.commit()

        flash(f"Batch #{batch_id} marked as picked up.", "success")
        return redirect(url_for("all_batches"))
    except psycopg2.Error as e:
        flash(f"Database Error: {e}", "error")
        return redirect(url_for("all_batches"))
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


@app.route("/bulk_mark_picked_up", methods=["POST"])
def bulk_mark_picked_up():
    """Mark multiple batches as picked up at once."""
    batch_ids = request.form.getlist("batch_ids")
    if not batch_ids:
        flash("No batches selected.", "error")
        return redirect(url_for("all_batches"))

    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()
        marked_count = 0

        for batch_id in batch_ids:
            try:
                # Get tracking numbers for this batch
                cursor.execute("""
                  SELECT tracking_number
                    FROM scans
                   WHERE batch_id = %s
                """, (batch_id,))
                rows = cursor.fetchall()
                # Filter out NULL tracking numbers
                tracking_list = [row["tracking_number"] for row in rows if row["tracking_number"]]
                pkg_count = len(tracking_list)
                tracking_csv = ",".join(tracking_list) if tracking_list else ""

                # Update batch status
                cursor.execute("""
                  UPDATE batches
                     SET pkg_count = %s,
                         tracking_numbers = %s,
                         status = 'recorded'
                   WHERE id = %s
                """, (pkg_count, tracking_csv, batch_id))
                marked_count += 1
            except Exception as e:
                print(f"Error marking batch {batch_id}: {e}")

        conn.commit()
        flash(f"Marked {marked_count} batches as picked up.", "success")
        return redirect(url_for("all_batches"))
    except psycopg2.Error as e:
        flash(f"Database Error: {e}", "error")
        return redirect(url_for("all_batches"))
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


@app.route("/record_batch", methods=["POST"])
def record_batch():
    batch_id = session.get("batch_id")
    if not batch_id:
        flash("No batch open.", "error")
        return redirect(url_for("index"))

    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()
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
    except psycopg2.Error as e:
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
    except psycopg2.Error as e:
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
        cursor = conn.cursor()

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
        now = now_pst().strftime("%Y-%m-%d %H:%M:%S")

        # Initialize Shopify API to fetch line items
        try:
            shopify_api = get_shopify_api()
        except Exception as e:
            print(f"⚠️ Shopify API not available: {e}")
            shopify_api = None

        total_to_process = len(scans)

        for idx, scan in enumerate(scans, 1):
            order_number = scan['order_number']
            customer_email = scan['customer_email']
            tracking_number = scan['tracking_number']
            order_id = scan.get('order_id', '')

            # Progress logging
            print(f"📧 [{idx}/{total_to_process}] Processing {order_number} ({customer_email})")

            # Check if this order was already notified (in ANY batch)
            cursor.execute("""
                SELECT id FROM notifications
                WHERE order_number = %s
                LIMIT 1
            """, (order_number,))

            if cursor.fetchone():
                print(f"   ⏭️  Skipping - already notified")
                skip_count += 1
                continue

            # Fetch line items - try local DB first, then Shopify
            line_items = []
            try:
                # Try local database first (faster, no API call)
                cursor.execute("""
                    SELECT oli.product_title, oli.variant_title, oli.sku, oli.quantity, oli.price
                    FROM order_line_items oli
                    JOIN orders o ON oli.order_id = o.id
                    WHERE o.order_number = %s
                """, (order_number,))
                local_items = cursor.fetchall()
                if local_items:
                    line_items = [
                        {
                            'title': item['product_title'],
                            'variant_title': item['variant_title'],
                            'sku': item['sku'],
                            'quantity': item['quantity'],
                            'price': str(item['price']) if item['price'] else '0'
                        }
                        for item in local_items
                    ]
                    print(f"   ✓ Found {len(line_items)} items from local DB")
            except Exception as e:
                print(f"   ⚠️ Local DB line items error: {e}")

            # Fallback to Shopify API if local DB didn't have items
            if not line_items and shopify_api and tracking_number:
                try:
                    print(f"   📦 Fetching line items from Shopify...")
                    order_data = shopify_api.get_order_by_tracking(tracking_number)
                    line_items = order_data.get('line_items', [])
                    if line_items:
                        print(f"   ✓ Found {len(line_items)} items from Shopify")
                except Exception as e:
                    print(f"   ⚠️ Could not fetch line items from Shopify: {e}")

            # Send Klaviyo event
            print(f"   📤 Sending email to Klaviyo...")
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
                    print(f"   ✅ Email sent successfully")
                else:
                    error_count += 1
                    print(f"   ❌ Failed to send email")
            except psycopg2.IntegrityError:
                # Duplicate entry - order already notified
                skip_count += 1
                print(f"   ⏭️  Already in notifications table")

        print(f"\n{'='*60}")
        print(f"NOTIFICATION SUMMARY")
        print(f"{'='*60}")
        print(f"✅ Sent:    {success_count}")
        print(f"⏭️  Skipped: {skip_count}")
        print(f"❌ Failed:  {error_count}")
        print(f"{'='*60}\n")

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
        cursor = conn.cursor()
        # Calculate pkg_count dynamically from scans table (so it updates immediately)
        # Fetch active batches (in_progress status) separately
        cursor.execute("""
          SELECT b.id, b.carrier, b.created_at, b.tracking_numbers, b.status, b.notified_at, b.notes,
                 COUNT(s.id) as pkg_count
            FROM batches b
            LEFT JOIN scans s ON s.batch_id = b.id
           WHERE b.status = 'in_progress' OR b.status IS NULL
           GROUP BY b.id, b.carrier, b.created_at, b.tracking_numbers, b.status, b.notified_at, b.notes
           ORDER BY b.id DESC
        """)
        active_batches = cursor.fetchall()

        # Fetch all batches (for "All Batches" section - completed/notified ones)
        cursor.execute("""
          SELECT b.id, b.carrier, b.created_at, b.tracking_numbers, b.status, b.notified_at, b.notes,
                 COUNT(s.id) as pkg_count
            FROM batches b
            LEFT JOIN scans s ON s.batch_id = b.id
           WHERE b.status IN ('recorded', 'notified')
           GROUP BY b.id, b.carrier, b.created_at, b.tracking_numbers, b.status, b.notified_at, b.notes
           ORDER BY b.id DESC
        """)
        completed_batches = cursor.fetchall()

        return render_template(
            "all_batches.html",
            active_batches=active_batches,
            completed_batches=completed_batches,
            shop_url=SHOP_URL,
            version=__version__,
            active_page="all_batches"
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
        cursor = conn.cursor()
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
                 COALESCE(status, '') as status,
                 order_id
            FROM scans
           WHERE batch_id = %s
           ORDER BY scan_date DESC
        """, (batch_id,))
        scans = cursor.fetchall()

        return render_template(
            "batch_view.html",
            batch=batch,
            scans=scans,
            shop_url=SHOP_URL,
            version=__version__,
            active_page="all_batches"
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
        cursor = conn.cursor()
        
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
        cursor = conn.cursor()

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

    # Safely parse page parameter with validation
    try:
        page = int(request.args.get("page", 1))
        if page < 1:
            page = 1
    except (ValueError, TypeError):
        page = 1

    per_page = 100
    offset = (page - 1) * per_page

    conn = None
    cursor = None
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()

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
        total_pages = max(1, (total_scans + per_page - 1) // per_page)  # Ensure at least 1 page

        # Validate page is within bounds
        if page > total_pages and total_scans > 0:
            page = total_pages
            offset = (page - 1) * per_page

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
                COALESCE(status, '') as status,
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
                COALESCE(status, '') as status,
                order_id,
                batch_id
              FROM scans
              ORDER BY scan_date DESC
              LIMIT %s OFFSET %s
            """, (per_page, offset))

        scans = cursor.fetchall()

        return render_template(
            "all_scans.html",
            scans=scans,
            shop_url=SHOP_URL,
            version=__version__,
            page=page,
            total_pages=total_pages,
            total_scans=total_scans,
            order_search=order_search,
            active_page="all_scans"
        )
    except psycopg2.OperationalError as e:
        print(f"MySQL connection error in all_scans: {e}")
        import traceback
        traceback.print_exc()
        flash("Database connection error. Please try again in a moment.", "error")
        return redirect(url_for("index"))
    except psycopg2.Error as e:
        print(f"MySQL error in all_scans: {e}")
        import traceback
        traceback.print_exc()
        flash("Database error occurred. Please contact support if this persists.", "error")
        return redirect(url_for("index"))
    except Exception as e:
        print(f"Unexpected error in all_scans: {e}")
        import traceback
        traceback.print_exc()
        flash(f"An error occurred while loading scans: {str(e)}", "error")
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
                            cursor = conn.cursor()
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

    return render_template(
        "pick_and_pack.html",
        order_data=order_data,
        error_message=error_message,
        already_verified=already_verified,
        search_identifier=search_identifier,
        shop_url=SHOP_URL,
        version=__version__,
        active_page="pick_and_pack"
    )


@app.route("/item_locations", methods=["GET"])
def item_locations():
    """
    Item locations admin page.
    Displays all location rules and allows adding/deleting rules.
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, aisle, shelf, rule_type, rule_value, created_at
            FROM item_location_rules
            ORDER BY aisle, shelf, rule_type, rule_value
        """)
        rules = cursor.fetchall()

        return render_template(
            "item_locations.html",
            rules=rules,
            shop_url=SHOP_URL,
            version=__version__,
            active_page="item_locations"
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
        cursor = conn.cursor()
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

        return render_template(
            "stuck_orders.html",
            stuck_scans=stuck_scans,
            version=__version__,
            active_page="stuck_orders"
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
            cursor = conn.cursor()
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

            # ── LOCAL DATABASE LOOKUP FIRST ──
            local_found = False
            try:
                # First try orders table (Shopify data - has email)
                cursor.execute("""
                    SELECT order_number, customer_name, customer_email, shopify_order_id
                    FROM orders
                    WHERE tracking_number = %s
                    LIMIT 1
                """, (tracking_number,))
                local_order = cursor.fetchone()

                if local_order and local_order.get('order_number'):
                    local_found = True
                    order_number = local_order.get('order_number', 'N/A')
                    customer_name = local_order.get('customer_name', 'Not Found')
                    customer_email = local_order.get('customer_email', '')
                    order_id = local_order.get('shopify_order_id', '')
                    print(f"✅ LOCAL DB (orders): Found order {order_number} for {tracking_number}")

                # If not found in orders, try shipments_cache (ShipStation data)
                if not local_found:
                    cursor.execute("""
                        SELECT order_number, customer_name, carrier_code, shipstation_batch_number
                        FROM shipments_cache
                        WHERE tracking_number = %s
                        LIMIT 1
                    """, (tracking_number,))
                    cached_shipment = cursor.fetchone()

                    if cached_shipment and cached_shipment.get('order_number'):
                        local_found = True
                        order_number = cached_shipment.get('order_number', 'N/A')
                        customer_name = cached_shipment.get('customer_name', 'Not Found')
                        shipstation_batch_number = cached_shipment.get('shipstation_batch_number', '')
                        carrier_code = cached_shipment.get('carrier_code', '')
                        if carrier_code:
                            carrier_map = {"ups": "UPS", "canadapost": "Canada Post", "canada_post": "Canada Post",
                                           "dhl": "DHL", "dhl_express": "DHL", "purolator": "Purolator"}
                            scan_carrier = carrier_map.get(carrier_code.lower(), scan_carrier)
                        print(f"✅ LOCAL DB (shipments_cache): Found order {order_number} for {tracking_number}")
            except Exception as e:
                print(f"Local DB lookup error: {e}")

            # ── ShipStation lookup (skip if local found) ──
            shopify_found = False
            try:
                if not local_found and SHIPSTATION_API_KEY and SHIPSTATION_API_SECRET:
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

            # ── Shopify lookup (skip if local found) ──
            if not local_found:
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
    Live Tracking page with tabs for Recent Batches and All Shipments.
    """
    # Tab handling
    current_tab = request.args.get("tab", "batches")  # Default to batches tab
    search_query = request.args.get("search", "").strip()
    page = int(request.args.get("page", 1))
    # Configurable items per page (default 100, max 500)
    per_page = min(int(request.args.get("per_page", 100)), 500)
    refresh_tracking = request.args.get("refresh", "") == "1"

    # Get Shopify store URL for customer links
    shop_url = os.environ.get("SHOP_URL", "")

    # Batch tab parameters
    batch_status = request.args.get("status", "completed")
    batch_page = int(request.args.get("page", 1)) if current_tab == "batches" else 1

    # Initialize data
    batches = []
    batch_pages = 1
    batch_error = None
    shipments = []
    total_shipments = 0
    total_pages = 1

    try:
        # ══════════════════════════════════════════════════════════════════════
        # FETCH BATCHES DATA (for batches tab)
        # ══════════════════════════════════════════════════════════════════════
        batch_result = get_shipstation_batches(status=batch_status, page=batch_page, page_size=25)
        raw_batches = batch_result.get("batches", [])
        batch_pages = batch_result.get("pages", 1)
        batch_error = batch_result.get("error")

        # Normalize batch field names (API might return camelCase)
        batches = []
        for b in raw_batches:
            # Debug: log first batch structure
            if not batches:
                print(f"📦 Batch API fields: {list(b.keys())}")
            batches.append({
                "batch_id": b.get("batch_id") or b.get("batchId") or "",
                "batch_number": b.get("batch_number") or b.get("batchNumber") or "",
                "batch_notes": b.get("batch_notes") or b.get("batchNotes") or b.get("notes") or "",
                "created_at": b.get("created_at") or b.get("createdAt") or "",
                "status": b.get("status") or "",
                "count": b.get("count") or b.get("label_count") or b.get("labelCount") or 0,
                "errors": b.get("errors") or b.get("error_count") or b.get("errorCount") or 0
            })

        # ══════════════════════════════════════════════════════════════════════
        # FETCH SHIPMENTS DATA (for shipments tab)
        # ══════════════════════════════════════════════════════════════════════
        print(f"📦 Loading shipments (page {page})...")

        conn = get_mysql_connection()
        cursor = conn.cursor()

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

        # Count for pagination
        count_query = f"""
            SELECT COUNT(DISTINCT sc.tracking_number) as total
            FROM shipments_cache sc
            LEFT JOIN scans s ON s.tracking_number = sc.tracking_number
            LEFT JOIN tracking_status_cache tc ON tc.tracking_number = sc.tracking_number
            WHERE sc.ship_date >= CURRENT_DATE - INTERVAL '90 days'
            {search_condition}
        """
        cursor.execute(count_query, search_params)
        total_shipments = cursor.fetchone()['total']
        total_pages = max(1, (total_shipments + per_page - 1) // per_page)

        # Get paginated shipments with cached tracking data (FAST!)
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
                tc.status as ups_status,
                tc.status_description as ups_status_text,
                tc.estimated_delivery,
                tc.last_location,
                tc.last_activity_date,
                tc.is_delivered,
                tc.updated_at as tracking_updated_at,
                co.id as cancelled_id,
                co.reason as cancel_reason
            FROM shipments_cache sc
            LEFT JOIN scans s ON s.tracking_number = sc.tracking_number
            LEFT JOIN tracking_status_cache tc ON tc.tracking_number = sc.tracking_number
            LEFT JOIN cancelled_orders co ON co.order_number = sc.order_number
            WHERE sc.ship_date >= CURRENT_DATE - INTERVAL '90 days'
            {search_condition}
            GROUP BY sc.tracking_number, sc.order_number, sc.customer_name,
                     sc.carrier_code, sc.ship_date, sc.shipstation_batch_number,
                     tc.status, tc.status_description, tc.estimated_delivery,
                     tc.last_location, tc.last_activity_date, tc.is_delivered, tc.updated_at,
                     co.id, co.reason
            ORDER BY sc.ship_date DESC
            LIMIT %s OFFSET %s
        """
        cursor.execute(query, search_params + [per_page, offset])
        cached_shipments = cursor.fetchall()

        print(f"✓ Found {len(cached_shipments)} shipments on page {page} of {total_pages} ({total_shipments} total)")

        # Collect tracking numbers that need UPS refresh
        tracking_to_refresh = []

        # Process each shipment
        for cached_ship in cached_shipments:
            tracking_number = cached_ship.get("tracking_number", "")
            order_number = cached_ship.get("order_number", "")
            carrier_code = cached_ship.get("carrier_code", "").upper()
            ship_date = str(cached_ship.get("ship_date", ""))
            customer_name = cached_ship.get("customer_name", "Unknown")
            batch_number = cached_ship.get("shipstation_batch_number", "")

            # Check if cancelled
            is_cancelled = cached_ship.get("cancelled_id") is not None
            cancel_reason = cached_ship.get("cancel_reason", "")

            # Check if scanned
            scan_date_obj = cached_ship.get("scan_date")
            scanned = scan_date_obj is not None
            scan_date = ""
            if scan_date_obj:
                scan_date = scan_date_obj.strftime("%Y-%m-%d") if hasattr(scan_date_obj, 'strftime') else str(scan_date_obj)[:10]

            # Get UPS tracking status from CACHE (not live API!)
            ups_status = cached_ship.get("ups_status") or "unknown"
            ups_status_text = cached_ship.get("ups_status_text") or ""
            estimated_delivery = cached_ship.get("estimated_delivery") or ""
            last_location = cached_ship.get("last_location") or ""
            is_delivered = cached_ship.get("is_delivered") or False
            tracking_updated = cached_ship.get("tracking_updated_at")

            # Build tracking URL and check if refresh needed
            tracking_url = ""
            is_canada_post = "canada" in carrier_code.lower() if carrier_code else False
            is_ups = carrier_code == "UPS" or tracking_number.startswith("1Z")

            if is_ups:
                tracking_url = f"https://www.ups.com/track?loc=en_US&tracknum={tracking_number}"
            elif is_canada_post:
                tracking_url = f"https://www.canadapost-postescanada.ca/track-reperage/en#/search?searchFor={tracking_number}"

            # Check if tracking needs refresh (older than 2 hours or missing)
            if is_ups or is_canada_post:
                if not tracking_updated:
                    tracking_to_refresh.append(tracking_number)
                else:
                    # Handle timezone-aware timestamps from PostgreSQL
                    now = datetime.now()
                    if hasattr(tracking_updated, 'tzinfo') and tracking_updated.tzinfo is not None:
                        tracking_updated = tracking_updated.replace(tzinfo=None)
                    if (now - tracking_updated).total_seconds() > 7200:
                        tracking_to_refresh.append(tracking_number)

            # Save original status for flag logic (before we modify it for display)
            original_ups_status = ups_status

            # Format status display with user-friendly messages
            if ups_status == "delivered":
                ups_status_text = "✅ Delivered"
                ups_status = "delivered"
            elif ups_status == "in_transit":
                # Check if it's "almost there" (out for delivery or has estimated delivery today/tomorrow)
                status_lower = (cached_ship.get("ups_status_text") or "").lower()
                if "out for delivery" in status_lower:
                    ups_status_text = "🏃 Almost There!"
                    ups_status = "almost_there"
                elif estimated_delivery:
                    # Check if delivery is today or tomorrow
                    try:
                        est_lower = estimated_delivery.lower()
                        today = datetime.now()
                        if "today" in est_lower or today.strftime("%B %d").lower() in est_lower:
                            ups_status_text = f"🏃 Almost There! (Today)"
                            ups_status = "almost_there"
                        elif "tomorrow" in est_lower:
                            ups_status_text = f"🚚 On the Way (Tomorrow)"
                            ups_status = "in_transit"
                        else:
                            ups_status_text = f"🚚 On the Way"
                            if estimated_delivery:
                                ups_status_text += f" - Est: {estimated_delivery}"
                    except:
                        ups_status_text = f"🚚 On the Way"
                        if estimated_delivery:
                            ups_status_text += f" - Est: {estimated_delivery}"
                else:
                    ups_status_text = "🚚 On the Way"
            elif ups_status == "label_created":
                # Check how long since label was created
                try:
                    ship_datetime = datetime.strptime(ship_date, "%Y-%m-%d")
                    days_since = (datetime.now() - ship_datetime).days
                    if days_since >= 3:
                        # Check if cache is stale before showing "hasn't moved"
                        # If cache is > 4 hours old, the status might be outdated
                        cache_is_stale = False
                        if tracking_updated:
                            tracking_updated_check = tracking_updated
                            if hasattr(tracking_updated_check, 'tzinfo') and tracking_updated_check.tzinfo is not None:
                                tracking_updated_check = tracking_updated_check.replace(tzinfo=None)
                            cache_age_hours = (datetime.now() - tracking_updated_check).total_seconds() / 3600
                            cache_is_stale = cache_age_hours > 4
                        else:
                            cache_is_stale = True  # No cache timestamp = definitely stale

                        if cache_is_stale:
                            # Cache is stale - show checking status and ensure refresh is queued
                            ups_status_text = "🔄 Checking Status..."
                            ups_status = "checking"
                            if tracking_number not in tracking_to_refresh:
                                tracking_to_refresh.append(tracking_number)
                        else:
                            # Cache is fresh - trust the "hasn't moved" status
                            ups_status_text = "😴 Hasn't Moved"
                            ups_status = "hasnt_moved"
                    else:
                        ups_status_text = "📦 Label Created"
                except:
                    ups_status_text = "📦 Label Created"
            elif ups_status == "exception":
                ups_status_text = "⚠️ Exception/Delay"
            elif not is_ups and not is_canada_post:
                # Other carriers we don't track
                ups_status_text = "N/A (Other Carrier)"
                ups_status = "non_ups"
            elif not ups_status_text or ups_status_text == "-" or ups_status == "unknown":
                # No cached data - needs refresh
                ups_status_text = "🔄 Loading..."
                ups_status = "unknown"
                if is_ups or is_canada_post:
                    tracking_to_refresh.append(tracking_number)

            # Determine if shipment should be flagged
            flag = False
            flag_reason = ""
            flag_severity = "normal"

            if is_ups or is_canada_post:
                try:
                    ship_datetime = datetime.strptime(ship_date, "%Y-%m-%d")
                    days_since_ship = (datetime.now() - ship_datetime).days

                    if not scanned and days_since_ship >= 7:
                        flag = True
                        flag_severity = "critical"
                        flag_reason = f"🚨 CRITICAL: Label created {days_since_ship} days ago but NEVER SCANNED!"
                    elif scanned and original_ups_status == "label_created" and days_since_ship >= 3:
                        flag = True
                        flag_severity = "critical"
                        flag_reason = f"🚨 Scanned {days_since_ship} days ago but UPS shows no pickup."
                    elif not scanned and days_since_ship >= 3:
                        flag = True
                        flag_severity = "warning"
                        flag_reason = f"⚠️ Not scanned after {days_since_ship} days."
                    elif original_ups_status == "exception":
                        flag = True
                        flag_severity = "warning"
                        flag_reason = "⚠️ Shipment exception or delay."
                except:
                    pass

            shipments.append({
                "order_number": order_number,
                "customer_name": customer_name,
                "tracking_number": tracking_number,
                "carrier": normalize_carrier(carrier_code),
                "ship_date": ship_date,
                "scanned": scanned,
                "scan_date": scan_date,
                "ups_status": ups_status,
                "ups_status_text": ups_status_text,
                "ups_last_activity": last_location or "—",
                "estimated_delivery": estimated_delivery,
                "tracking_url": tracking_url,
                "flag": flag,
                "flag_reason": flag_reason,
                "flag_severity": flag_severity,
                "is_cancelled": is_cancelled,
                "cancel_reason": cancel_reason,
                "batch_number": batch_number
            })

        cursor.close()
        conn.close()

        # Refresh tracking cache in background (don't block page load)
        # If user clicked "Refresh Tracking" button, force refresh all visible shipments
        if refresh_tracking:
            # Get all tracking numbers from current page for force refresh
            all_tracking = [s["tracking_number"] for s in shipments if s.get("tracking_number")]
            if all_tracking:
                print(f"🔄 User requested refresh: force-refreshing {len(all_tracking)} tracking statuses...")
                import threading
                # Split into UPS and Canada Post
                ups_tracking = [t for t in all_tracking if t.startswith("1Z")]
                cp_tracking = [t for t in all_tracking if not t.startswith("1Z")]
                if ups_tracking:
                    threading.Thread(target=update_ups_tracking_cache, args=(ups_tracking[:50], True)).start()
                if cp_tracking:
                    threading.Thread(target=update_canadapost_tracking_cache, args=(cp_tracking[:30], True)).start()
        elif tracking_to_refresh and len(tracking_to_refresh) <= 20:
            # Auto-refresh stale/missing tracking data (small batches only)
            print(f"🔄 Auto-refreshing {len(tracking_to_refresh)} stale tracking statuses...")
            import threading
            # Split into UPS and Canada Post
            ups_tracking = [t for t in tracking_to_refresh if t.startswith("1Z")]
            cp_tracking = [t for t in tracking_to_refresh if not t.startswith("1Z")]
            if ups_tracking:
                threading.Thread(target=update_ups_tracking_cache, args=(ups_tracking[:50], False)).start()
            if cp_tracking:
                threading.Thread(target=update_canadapost_tracking_cache, args=(cp_tracking[:30], False)).start()

        # Pagination URLs for shipments tab
        has_prev = page > 1
        has_next = page < total_pages
        prev_url = url_for("check_shipments", tab="shipments", page=page-1, search=search_query, per_page=per_page) if has_prev else "#"
        next_url = url_for("check_shipments", tab="shipments", page=page+1, search=search_query, per_page=per_page) if has_next else "#"

        return render_template(
            "check_shipments.html",
            # Tab state
            current_tab=current_tab,
            # Batches tab data
            batches=batches,
            batch_status=batch_status,
            batch_page=batch_page,
            batch_pages=batch_pages,
            batch_error=batch_error,
            # Shipments tab data
            shipments=shipments,
            search_query=search_query,
            loading=False,
            page=page,
            per_page=per_page,
            total_pages=total_pages,
            total_shipments=total_shipments,
            has_prev=has_prev,
            has_next=has_next,
            prev_url=prev_url,
            next_url=next_url,
            # Shopify
            shop_url=shop_url,
            version=__version__,
            active_page="check_shipments"
        )

    except Exception as e:
        print(f"❌ Error in check_shipments: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Error loading shipments: {str(e)}", "error")
        return render_template(
            "check_shipments.html",
            current_tab=current_tab,
            batches=[],
            batch_status="completed",
            batch_page=1,
            batch_pages=1,
            batch_error=str(e),
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
            version=__version__,
            active_page="check_shipments"
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


# ─────────────────────────────────────────────────────────────────────────────
# ── SHIPSTATION BATCH DETAIL ROUTE ────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/ss_batches/<batch_id>", methods=["GET"])
def ss_batch_detail(batch_id):
    """View details of a specific ShipStation batch with tracking status."""

    # Get batch info
    batch = None
    try:
        response = requests.get(
            f"https://api.shipstation.com/v2/batches/{batch_id}",
            headers={"API-Key": SHIPSTATION_V2_API_KEY},
            timeout=30
        )
        if response.status_code == 200:
            batch = response.json()
    except Exception as e:
        print(f"Error fetching batch {batch_id}: {e}")

    # Get shipments for this batch
    shipments_raw = get_shipstation_batch_shipments(batch_id)

    # Enrich shipments with tracking data from our cache
    shipments = []
    stats = {"delivered": 0, "in_transit": 0, "not_moving": 0, "scanned": 0}
    batch_ship_date = None  # Will be set from first shipment

    if shipments_raw:
        # Debug: Log first shipment structure to understand API response
        import json
        if shipments_raw:
            print(f"📦 Sample shipment keys: {list(shipments_raw[0].keys())}")
            print(f"📦 Full first shipment: {json.dumps(shipments_raw[0], indent=2, default=str)[:2000]}")

        # Helper to extract field from nested V2 API response
        def get_nested(obj, *keys):
            """Try multiple keys/paths to find a value."""
            for key in keys:
                if isinstance(key, tuple):
                    # Nested path like ("label_data", "tracking_number")
                    val = obj
                    for k in key:
                        if isinstance(val, dict):
                            val = val.get(k)
                        else:
                            val = None
                            break
                    if val:
                        return val
                else:
                    val = obj.get(key) if isinstance(obj, dict) else None
                    if val:
                        return val
            return ""

        # Build a lookup from shipments_cache by customer name (to get tracking/order info)
        # First collect all customer names from the batch
        customer_names = []
        for s in shipments_raw:
            ship_to = s.get("ship_to") or s.get("shipTo") or {}
            name = ship_to.get("name", "")
            if name:
                customer_names.append(name)

        # Fetch matching shipments from our cache
        shipments_cache_lookup = {}
        if customer_names:
            try:
                conn = get_mysql_connection()
                cursor = conn.cursor()
                # Get recent shipments matching these customer names
                placeholders = ",".join(["%s"] * len(customer_names))
                cursor.execute(f"""
                    SELECT tracking_number, order_number, customer_name, carrier_code, ship_date,
                           shipstation_batch_number
                    FROM shipments_cache
                    WHERE customer_name IN ({placeholders})
                    AND ship_date >= CURRENT_DATE - INTERVAL '30 days'
                """, customer_names)
                for row in cursor.fetchall():
                    # Key by customer name (might have duplicates, but usually unique per batch)
                    shipments_cache_lookup[row["customer_name"]] = row
                cursor.close()
                conn.close()
                print(f"📦 Found {len(shipments_cache_lookup)} matching shipments in cache by customer name")
            except Exception as e:
                print(f"Error fetching shipments cache: {e}")

        # Get tracking numbers for tracking_status_cache lookup
        tracking_numbers = [shipments_cache_lookup.get(
            (s.get("ship_to") or s.get("shipTo") or {}).get("name", ""), {}
        ).get("tracking_number", "") for s in shipments_raw]
        tracking_numbers = [t for t in tracking_numbers if t]
        print(f"📦 Found {len(tracking_numbers)} tracking numbers from cache lookup")

        # Fetch cached tracking status data
        tracking_cache = {}
        scans_cache = {}  # Track which shipments have been scanned
        if tracking_numbers:
            try:
                conn = get_mysql_connection()
                cursor = conn.cursor()
                placeholders = ",".join(["%s"] * len(tracking_numbers))

                # Get tracking status
                cursor.execute(f"""
                    SELECT tracking_number, status, status_description, last_location, is_delivered
                    FROM tracking_status_cache
                    WHERE tracking_number IN ({placeholders})
                """, tracking_numbers)
                for row in cursor.fetchall():
                    tracking_cache[row["tracking_number"]] = row

                # Get scan status
                cursor.execute(f"""
                    SELECT tracking_number, MAX(scan_date) as scan_date
                    FROM scans
                    WHERE tracking_number IN ({placeholders})
                    GROUP BY tracking_number
                """, tracking_numbers)
                for row in cursor.fetchall():
                    scans_cache[row["tracking_number"]] = row["scan_date"]

                cursor.close()
                conn.close()
                print(f"📦 Found {len(tracking_cache)} cached tracking status records, {len(scans_cache)} scanned")
            except Exception as e:
                print(f"Error fetching tracking cache: {e}")

        # Process shipments
        for s in shipments_raw:
            # Get customer name from ship_to
            ship_to = s.get("ship_to") or s.get("shipTo") or {}
            customer_name = ship_to.get("name", "")

            # Look up from our shipments_cache by customer name
            cached_shipment = shipments_cache_lookup.get(customer_name, {})

            # Get tracking number from our cache (more reliable than API)
            tracking_number = cached_shipment.get("tracking_number", "")

            # Get order number from our cache
            order_number = cached_shipment.get("order_number", "")

            # Get carrier from our cache
            carrier_code = cached_shipment.get("carrier_code", "")

            # Get ship date - prefer our cache, fallback to API
            ship_date = cached_shipment.get("ship_date", "")
            if not ship_date:
                ship_date = get_nested(s, "ship_date", "shipDate", "created_at", "createdAt",
                                      ("label_data", "ship_date"), ("labelData", "shipDate"))
            # Convert date object to string for template
            if ship_date and hasattr(ship_date, 'strftime'):
                ship_date = ship_date.strftime('%Y-%m-%d')

            # Get tracking status from tracking_status_cache
            cached_tracking = tracking_cache.get(tracking_number, {})
            tracking_status = cached_tracking.get("status", "unknown")
            tracking_status_text = cached_tracking.get("status_description", "Unknown")
            last_location = cached_tracking.get("last_location", "")
            is_delivered = cached_tracking.get("is_delivered", False)

            # Format status text
            if tracking_status == "delivered" or is_delivered:
                tracking_status = "delivered"
                tracking_status_text = "Delivered"
                stats["delivered"] += 1
            elif tracking_status == "in_transit":
                tracking_status_text = "In Transit"
                stats["in_transit"] += 1
            elif tracking_status == "label_created":
                tracking_status_text = "Label Created"
                stats["not_moving"] += 1
            elif tracking_status == "exception":
                tracking_status_text = "Exception"
                stats["not_moving"] += 1
            else:
                stats["not_moving"] += 1

            # Check if scanned
            scanned = tracking_number in scans_cache
            if scanned:
                stats["scanned"] += 1

            # Capture batch ship date from first shipment
            if batch_ship_date is None and ship_date:
                batch_ship_date = ship_date

            shipments.append({
                "tracking_number": tracking_number,
                "order_number": order_number,
                "customer_name": customer_name,
                "carrier_code": normalize_carrier(carrier_code),
                "ship_date": ship_date,
                "tracking_status": tracking_status,
                "tracking_status_text": tracking_status_text,
                "last_location": last_location,
                "scanned": scanned
            })

    # Get Shopify store URL for customer links
    shop_url = os.environ.get("SHOP_URL", "")

    return render_template(
        "ss_batch_detail.html",
        batch_id=batch_id,
        batch=batch,
        shipments=shipments,
        stats=stats,
        batch_ship_date=batch_ship_date,
        shop_url=shop_url,
        version=__version__,
        active_page="ss_batches"
    )


# ─────────────────────────────────────────────────────────────────────────────
# ── DEBUG: CHECK TRACKING STATUS ─────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/debug_tracking/<tracking_number>", methods=["GET"])
def debug_tracking(tracking_number):
    """Debug endpoint to check what UPS/Canada Post API returns for a tracking number."""
    import json

    result = {
        "tracking_number": tracking_number,
        "cached_status": None,
        "live_api_result": None,
        "error": None
    }

    # Check what's in the cache
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT status, status_description, is_delivered, last_location,
                   estimated_delivery, raw_status_code, updated_at
            FROM tracking_status_cache
            WHERE tracking_number = %s
        """, (tracking_number,))
        row = cursor.fetchone()
        if row:
            result["cached_status"] = {
                "status": row["status"],
                "status_description": row["status_description"],
                "is_delivered": row["is_delivered"],
                "last_location": row["last_location"],
                "estimated_delivery": row["estimated_delivery"],
                "raw_status_code": row["raw_status_code"],
                "updated_at": str(row["updated_at"])
            }
        cursor.close()
        conn.close()
    except Exception as e:
        result["error"] = f"Cache lookup error: {e}"

    # Get live status from API
    try:
        if tracking_number.startswith("1Z"):
            # UPS tracking
            ups_api = get_ups_api()
            if ups_api.enabled:
                result["live_api_result"] = ups_api.get_tracking_status(tracking_number)
            else:
                result["live_api_result"] = {"error": "UPS API not enabled"}
        else:
            # Canada Post tracking
            cp_api = get_canadapost_api()
            if cp_api.enabled:
                result["live_api_result"] = cp_api.get_tracking_status(tracking_number)
            else:
                result["live_api_result"] = {"error": "Canada Post API not enabled"}
    except Exception as e:
        result["live_api_result"] = {"error": str(e)}

    # Add status code mapping reference
    ups_code_mappings = {
        "delivered": ["011", "KB", "KM"],
        "in_transit": ["M", "MP", "P", "J", "W", "A", "AR", "AF", "OR", "DP", "OT", "IT", "005", "012", "021", "022"],
        "label_created": ["I", "MV", "NA"],
        "exception": ["X", "RS", "DJ", "D", "RD"]
    }

    def lookup_code(code):
        if not code:
            return "no_code"
        for status, codes in ups_code_mappings.items():
            if code in codes:
                return status
        return "unknown (not in mapping)"

    cached_code = result.get("cached_status", {}).get("raw_status_code") if result.get("cached_status") else None
    live_code = result.get("live_api_result", {}).get("raw_status_code") if result.get("live_api_result") else None

    result["status_code_mapping"] = {
        "reference": ups_code_mappings,
        "cached_code": cached_code,
        "cached_code_maps_to": lookup_code(cached_code),
        "live_code": live_code,
        "live_code_maps_to": lookup_code(live_code),
        "note": "If cached_code_maps_to != cached status, the mapping was updated after this was cached"
    }

    return f"<pre>{json.dumps(result, indent=2, default=str)}</pre>"


# ============================================================================
# ALL ORDERS PAGE & ORDERS SYNC API
# ============================================================================

@app.route("/all_orders", methods=["GET"])
def all_orders():
    """
    Display all orders from local database with search and filtering.
    Default filter: unfulfilled orders.
    Supports multiple stacked advanced filters via JSON.
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()

        # Get query parameters
        search_query = request.args.get('q', '').strip()
        fulfillment_filter = request.args.get('filter', 'unfulfilled')  # unfulfilled, fulfilled, all
        page = int(request.args.get('page', 1))
        per_page = 250

        # Get sort parameters for server-side sorting
        sort_by = request.args.get('sort', 'age')  # default sort by age (created date)
        sort_dir = request.args.get('dir', 'desc')  # default newest first

        # Validate sort parameters to prevent SQL injection
        allowed_sorts = {
            'order': 'order_number',
            'age': 'shopify_created_at',
            'customer': 'customer_name',
            'email': 'customer_email',
            'status': 'fulfillment_status'
        }
        sort_column = allowed_sorts.get(sort_by, 'shopify_created_at')
        sort_direction = 'ASC' if sort_dir.lower() == 'asc' else 'DESC'

        # Parse multiple filters from JSON
        filters_json = request.args.get('filters', '').strip()
        filters = []
        print(f"[DEBUG] filters_json from request: {repr(filters_json)}")
        if filters_json:
            try:
                filters = json.loads(filters_json)
                print(f"[DEBUG] parsed filters: {filters}")
                if not isinstance(filters, list):
                    filters = []
            except Exception as e:
                print(f"[DEBUG] JSON parse error: {e}")
                filters = []

        # Determine if we need to join with line items or options based on ALL filters
        need_line_items_join = any(f.get('field') in ('item_name', 'item_sku') for f in filters)
        need_options_join = any(f.get('field') == 'item_options' for f in filters)

        # Build base query with line item counts and country extraction
        if need_line_items_join or need_options_join:
            base_query = """
                SELECT DISTINCT o.id, o.shopify_order_id, o.order_number, o.customer_name, o.customer_email,
                       o.tracking_number, o.fulfillment_status, o.financial_status, o.total_price,
                       o.currency, o.shopify_created_at, o.scanned_status, o.cancelled_at, o.shipping_address, o.note,
                       o.total_weight_grams,
                       COALESCE(o.shipping_address::jsonb ->> 'country', o.shipping_address::jsonb ->> 'country_code', '') as ship_country,
                       (SELECT COALESCE(SUM(oli.quantity), 0) FROM order_line_items oli WHERE oli.order_id = o.id) as item_qty,
                       (SELECT COUNT(*) FROM order_line_items oli WHERE oli.order_id = o.id) as line_item_count
                FROM orders o
                INNER JOIN order_line_items li ON li.order_id = o.id
            """
            if need_options_join:
                base_query += " LEFT JOIN order_line_item_options lio ON lio.line_item_id = li.id"
            base_query += " WHERE o.cancelled_at IS NULL"
            col_prefix = "o."
        else:
            base_query = """
                SELECT id, shopify_order_id, order_number, customer_name, customer_email,
                       tracking_number, fulfillment_status, financial_status, total_price,
                       currency, shopify_created_at, scanned_status, cancelled_at, shipping_address, note,
                       total_weight_grams,
                       COALESCE(shipping_address::jsonb ->> 'country', shipping_address::jsonb ->> 'country_code', '') as ship_country,
                       (SELECT COALESCE(SUM(oli.quantity), 0) FROM order_line_items oli WHERE oli.order_id = orders.id) as item_qty,
                       (SELECT COUNT(*) FROM order_line_items oli WHERE oli.order_id = orders.id) as line_item_count
                FROM orders
                WHERE cancelled_at IS NULL
            """
            col_prefix = ""
        params = []

        # Add search filter
        if search_query:
            base_query += f""" AND (
                {col_prefix}order_number ILIKE %s OR
                {col_prefix}customer_name ILIKE %s OR
                {col_prefix}customer_email ILIKE %s OR
                {col_prefix}tracking_number ILIKE %s
            )"""
            search_term = f"%{search_query}%"
            params.extend([search_term, search_term, search_term, search_term])

        # Add fulfillment filter
        if fulfillment_filter == 'fulfilled':
            base_query += f" AND {col_prefix}fulfillment_status = 'fulfilled'"
        elif fulfillment_filter == 'unfulfilled':
            base_query += f" AND ({col_prefix}fulfillment_status IS NULL OR {col_prefix}fulfillment_status = '' OR {col_prefix}fulfillment_status = 'unfulfilled' OR {col_prefix}fulfillment_status = 'partial')"

        # Apply each advanced filter (combined with AND)
        for flt in filters:
            adv_field = flt.get('field', '')
            adv_condition = flt.get('condition', 'contains')
            adv_value = flt.get('value', '')

            if not adv_field or not adv_value:
                continue

            keywords = [kw.strip() for kw in adv_value.split(',') if kw.strip()]
            is_contains = adv_condition in ('contains', 'equals')
            is_exact = adv_condition in ('equals', 'not_equals')

            if adv_field == 'item_name':
                if is_contains:
                    conds = ["li.product_title ILIKE %s" for _ in keywords]
                    for kw in keywords:
                        params.append(f"%{kw}%" if not is_exact else kw)
                    base_query += f" AND ({' OR '.join(conds)})"
                else:
                    for kw in keywords:
                        base_query += " AND NOT EXISTS (SELECT 1 FROM order_line_items li2 WHERE li2.order_id = " + (col_prefix + "id" if col_prefix else "orders.id") + " AND li2.product_title ILIKE %s)"
                        params.append(f"%{kw}%")

            elif adv_field == 'item_options':
                if is_contains:
                    conds = ["(lio.name ILIKE %s OR lio.value ILIKE %s)" for _ in keywords]
                    for kw in keywords:
                        params.append(f"%{kw}%")
                        params.append(f"%{kw}%")
                    base_query += f" AND ({' OR '.join(conds)})"
                else:
                    for kw in keywords:
                        base_query += " AND NOT EXISTS (SELECT 1 FROM order_line_items li2 JOIN order_line_item_options lio2 ON lio2.line_item_id = li2.id WHERE li2.order_id = " + (col_prefix + "id" if col_prefix else "orders.id") + " AND (lio2.name ILIKE %s OR lio2.value ILIKE %s))"
                        params.append(f"%{kw}%")
                        params.append(f"%{kw}%")

            elif adv_field == 'item_sku':
                if is_contains:
                    conds = ["li.sku ILIKE %s" for _ in keywords]
                    for kw in keywords:
                        params.append(f"%{kw}%" if not is_exact else kw)
                    base_query += f" AND ({' OR '.join(conds)})"
                else:
                    for kw in keywords:
                        base_query += " AND NOT EXISTS (SELECT 1 FROM order_line_items li2 WHERE li2.order_id = " + (col_prefix + "id" if col_prefix else "orders.id") + " AND li2.sku ILIKE %s)"
                        params.append(f"%{kw}%")

            elif adv_field == 'customer_name':
                if is_contains:
                    conds = [f"{col_prefix}customer_name ILIKE %s" for _ in keywords]
                    for kw in keywords:
                        params.append(f"%{kw}%")
                    base_query += f" AND ({' OR '.join(conds)})"
                else:
                    for kw in keywords:
                        base_query += f" AND ({col_prefix}customer_name IS NULL OR {col_prefix}customer_name NOT ILIKE %s)"
                        params.append(f"%{kw}%")

            elif adv_field == 'customer_email':
                if is_contains:
                    conds = [f"{col_prefix}customer_email ILIKE %s" for _ in keywords]
                    for kw in keywords:
                        params.append(f"%{kw}%")
                    base_query += f" AND ({' OR '.join(conds)})"
                else:
                    for kw in keywords:
                        base_query += f" AND ({col_prefix}customer_email IS NULL OR {col_prefix}customer_email NOT ILIKE %s)"
                        params.append(f"%{kw}%")

            elif adv_field == 'order_note':
                if is_contains:
                    conds = [f"{col_prefix}note ILIKE %s" for _ in keywords]
                    for kw in keywords:
                        params.append(f"%{kw}%")
                    base_query += f" AND ({' OR '.join(conds)})"
                else:
                    for kw in keywords:
                        base_query += f" AND ({col_prefix}note IS NULL OR {col_prefix}note NOT ILIKE %s)"
                        params.append(f"%{kw}%")

            elif adv_field.startswith('ship_'):
                json_field_map = {
                    'ship_country': 'country',
                    'ship_province': 'province',
                    'ship_city': 'city',
                    'ship_address1': 'address1',
                    'ship_address2': 'address2',
                    'ship_zip': 'zip'
                }
                json_key = json_field_map.get(adv_field, 'country')
                # Use PostgreSQL JSON extraction operator ->> for proper JSON field access
                if is_contains:
                    conds = [f"({col_prefix}shipping_address::jsonb ->> %s) ILIKE %s" for _ in keywords]
                    for kw in keywords:
                        params.append(json_key)
                        params.append(f'%{kw}%' if not is_exact else kw)
                    base_query += f" AND ({' OR '.join(conds)})"
                else:
                    for kw in keywords:
                        base_query += f" AND ({col_prefix}shipping_address IS NULL OR ({col_prefix}shipping_address::jsonb ->> %s) NOT ILIKE %s)"
                        params.append(json_key)
                        params.append(f'%{kw}%')

            elif adv_field == 'is_international':
                # Use proper JSON extraction to check country field
                if adv_condition in ('contains', 'equals'):
                    # International = NOT Canada
                    base_query += f" AND ({col_prefix}shipping_address IS NULL OR (({col_prefix}shipping_address::jsonb ->> 'country') NOT ILIKE %s AND ({col_prefix}shipping_address::jsonb ->> 'country') NOT ILIKE %s AND ({col_prefix}shipping_address::jsonb ->> 'country_code') NOT IN ('CA', 'CAN')))"
                    params.append('%Canada%')
                    params.append('%CA%')
                else:
                    # Domestic = Canada
                    base_query += f" AND (({col_prefix}shipping_address::jsonb ->> 'country') ILIKE %s OR ({col_prefix}shipping_address::jsonb ->> 'country') ILIKE %s OR ({col_prefix}shipping_address::jsonb ->> 'country_code') IN ('CA', 'CAN'))"
                    params.append('%Canada%')
                    params.append('%CA%')

            elif adv_field == 'weight_over':
                # Filter orders with total weight over X grams
                try:
                    weight_threshold = int(adv_value)
                    base_query += f" AND COALESCE({col_prefix}total_weight_grams, 0) > %s"
                    params.append(weight_threshold)
                except ValueError:
                    pass  # Invalid weight value, skip filter

            elif adv_field == 'weight_under':
                # Filter orders with total weight under X grams
                try:
                    weight_threshold = int(adv_value)
                    base_query += f" AND COALESCE({col_prefix}total_weight_grams, 0) < %s"
                    params.append(weight_threshold)
                except ValueError:
                    pass  # Invalid weight value, skip filter

        # Get total count for pagination
        count_query = f"SELECT COUNT(*) as count FROM ({base_query}) as subquery"
        cursor.execute(count_query, params)
        total_count = cursor.fetchone()['count']

        # Add ordering and pagination (server-side sorting)
        base_query += f" ORDER BY {col_prefix}{sort_column} {sort_direction}"
        offset = (page - 1) * per_page
        base_query += f" LIMIT {per_page} OFFSET {offset}"

        cursor.execute(base_query, params)
        orders = cursor.fetchall()

        # Calculate pagination info
        total_pages = (total_count + per_page - 1) // per_page

        # Get sync status
        sync_status = get_orders_sync().get_sync_status()

        return render_template(
            "all_orders.html",
            orders=orders,
            search_query=search_query,
            fulfillment_filter=fulfillment_filter,
            page=page,
            per_page=per_page,
            total_count=total_count,
            total_pages=total_pages,
            sync_status=sync_status,
            shop_url=SHOP_URL,
            version=__version__,
            active_page="all_orders",
            # Multiple filters for template
            filters=filters,
            filters_json=filters_json,
            # Sort parameters for server-side sorting
            sort_by=sort_by,
            sort_dir=sort_dir
        )
    except Exception as e:
        print(f"Error loading all orders: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Error loading orders: {e}", "error")
        return render_template(
            "all_orders.html",
            orders=[],
            search_query='',
            fulfillment_filter='unfulfilled',
            page=1,
            per_page=250,
            total_count=0,
            total_pages=0,
            sync_status={},
            shop_url=SHOP_URL,
            version=__version__,
            active_page="all_orders",
            filters=[],
            filters_json='',
            sort_by='age',
            sort_dir='desc'
        )
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# Order Batches - Group orders for fulfillment prep
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/create_order_batch", methods=["POST"])
def create_order_batch():
    """
    Create a new order batch from selected orders.
    Expects JSON array of order numbers in 'order_numbers' field.
    """
    try:
        order_numbers_json = request.form.get('order_numbers', '[]')
        order_numbers = json.loads(order_numbers_json)

        if not order_numbers:
            flash("No orders selected", "error")
            return redirect(url_for('all_orders'))

        conn = get_mysql_connection()
        cursor = conn.cursor()

        # Create the batch
        batch_name = f"Batch {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        cursor.execute("""
            INSERT INTO order_batches (name, status, created_by)
            VALUES (%s, 'pending', %s)
            RETURNING id
        """, (batch_name, session.get('user_email', 'unknown')))
        batch_id = cursor.fetchone()['id']

        # Add orders to the batch
        added_count = 0
        for order_num in order_numbers:
            # Get order ID from order number
            cursor.execute("SELECT id FROM orders WHERE order_number = %s", (order_num,))
            order_row = cursor.fetchone()
            if order_row:
                try:
                    cursor.execute("""
                        INSERT INTO order_batch_items (batch_id, order_id, order_number)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (batch_id, order_id) DO NOTHING
                    """, (batch_id, order_row['id'], order_num))
                    added_count += 1
                except Exception as e:
                    print(f"Error adding order {order_num} to batch: {e}")

        conn.commit()
        cursor.close()
        conn.close()

        flash(f"Created batch with {added_count} orders", "success")
        return redirect(url_for('view_order_batch', batch_id=batch_id))

    except Exception as e:
        print(f"Error creating order batch: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Error creating batch: {str(e)}", "error")
        return redirect(url_for('all_orders'))


@app.route("/order_batches")
def order_batches():
    """List all order batches."""
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT ob.*,
                   COUNT(obi.id) as order_count,
                   SUM(CASE WHEN obi.status = 'completed' THEN 1 ELSE 0 END) as completed_count
            FROM order_batches ob
            LEFT JOIN order_batch_items obi ON ob.id = obi.batch_id
            GROUP BY ob.id
            ORDER BY ob.created_at DESC
        """)
        batches = cursor.fetchall()

        cursor.close()
        conn.close()

        return render_template(
            'order_batches.html',
            batches=batches,
            version=__version__,
            active_page='order_batches'
        )

    except Exception as e:
        print(f"Error loading order batches: {e}")
        flash(f"Error loading batches: {str(e)}", "error")
        return redirect(url_for('all_orders'))


@app.route("/order_batch/<int:batch_id>")
def view_order_batch(batch_id):
    """View a single order batch with all its orders and line items."""
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()

        # Get batch info
        cursor.execute("SELECT * FROM order_batches WHERE id = %s", (batch_id,))
        batch = cursor.fetchone()
        if not batch:
            flash("Batch not found", "error")
            return redirect(url_for('order_batches'))

        # Get all orders in this batch with their line items
        cursor.execute("""
            SELECT obi.*, o.customer_name, o.customer_email, o.shipping_address,
                   o.fulfillment_status, o.total_price, o.currency, o.note,
                   o.shopify_created_at, o.tracking_number
            FROM order_batch_items obi
            JOIN orders o ON obi.order_id = o.id
            WHERE obi.batch_id = %s
            ORDER BY o.shopify_created_at DESC
        """, (batch_id,))
        batch_orders = cursor.fetchall()

        # Get line items for each order
        orders_with_items = []
        for order in batch_orders:
            cursor.execute("""
                SELECT li.*, array_agg(CONCAT(lio.name, ': ', lio.value)) as options
                FROM order_line_items li
                LEFT JOIN order_line_item_options lio ON li.id = lio.line_item_id
                WHERE li.order_id = %s
                GROUP BY li.id
                ORDER BY li.id
            """, (order['order_id'],))
            line_items = cursor.fetchall()

            order_dict = dict(order)
            order_dict['line_items'] = line_items

            # Parse shipping address JSON
            if order_dict.get('shipping_address'):
                try:
                    order_dict['shipping_address'] = json.loads(order_dict['shipping_address'])
                except:
                    pass

            orders_with_items.append(order_dict)

        cursor.close()
        conn.close()

        return render_template(
            'order_batch_detail.html',
            batch=batch,
            orders=orders_with_items,
            version=__version__,
            active_page='order_batches'
        )

    except Exception as e:
        print(f"Error viewing order batch: {e}")
        import traceback
        traceback.print_exc()
        flash(f"Error loading batch: {str(e)}", "error")
        return redirect(url_for('order_batches'))


@app.route("/order_batch/<int:batch_id>/delete", methods=["POST"])
def delete_order_batch(batch_id):
    """Delete an order batch."""
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM order_batches WHERE id = %s", (batch_id,))
        conn.commit()
        cursor.close()
        conn.close()
        flash("Batch deleted", "success")
    except Exception as e:
        flash(f"Error deleting batch: {str(e)}", "error")
    return redirect(url_for('order_batches'))


# ══════════════════════════════════════════════════════════════════════════════
# Settings & Packing Slip Builder
# ══════════════════════════════════════════════════════════════════════════════

def get_setting(key, default=None):
    """Get a single setting value from the database."""
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT setting_value FROM app_settings WHERE setting_key = %s", (key,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        return result['setting_value'] if result else default
    except:
        return default


def get_all_settings():
    """Get all settings as a dictionary."""
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT setting_key, setting_value, setting_type FROM app_settings")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return {row['setting_key']: row['setting_value'] for row in rows}
    except:
        return {}


def save_setting(key, value, setting_type='text'):
    """Save a setting to the database."""
    try:
        conn = get_mysql_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO app_settings (setting_key, setting_value, setting_type, updated_at)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (setting_key) DO UPDATE
            SET setting_value = EXCLUDED.setting_value,
                setting_type = EXCLUDED.setting_type,
                updated_at = CURRENT_TIMESTAMP
        """, (key, value, setting_type))
        conn.commit()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Error saving setting {key}: {e}")
        return False


@app.route("/settings")
def settings_page():
    """Admin settings page with packing slip builder."""
    settings = get_all_settings()

    # Get default templates from orders_sync if not in DB
    from orders_sync import DEFAULT_PACKING_SLIP_HTML, DEFAULT_PACKING_SLIP_CSS

    return render_template(
        "settings.html",
        settings=settings,
        default_html=DEFAULT_PACKING_SLIP_HTML,
        default_css=DEFAULT_PACKING_SLIP_CSS,
        version=__version__,
        active_page="settings"
    )


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    """Save settings from the settings page."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        saved = []
        for key, value in data.items():
            # Determine setting type based on key
            if key.endswith('_html'):
                stype = 'html'
            elif key.endswith('_css'):
                stype = 'css'
            elif key.endswith('_js'):
                stype = 'js'
            elif key.endswith('_url'):
                stype = 'url'
            elif key.endswith('_width') or key.endswith('_height'):
                stype = 'number'
            else:
                stype = 'text'

            if save_setting(key, value, stype):
                saved.append(key)

        return jsonify({"success": True, "saved": saved})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/settings/logo", methods=["POST"])
def api_upload_logo():
    """Upload company logo."""
    try:
        if 'logo' not in request.files:
            return jsonify({"success": False, "error": "No file provided"}), 400

        file = request.files['logo']
        if file.filename == '':
            return jsonify({"success": False, "error": "No file selected"}), 400

        # Validate file type
        allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'}
        ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
        if ext not in allowed_extensions:
            return jsonify({"success": False, "error": f"File type not allowed. Use: {', '.join(allowed_extensions)}"}), 400

        # Save to static/uploads folder
        import os
        upload_dir = os.path.join(app.static_folder, 'uploads')
        os.makedirs(upload_dir, exist_ok=True)

        # Generate unique filename
        filename = f"logo_{datetime.now().strftime('%Y%m%d%H%M%S')}.{ext}"
        filepath = os.path.join(upload_dir, filename)
        file.save(filepath)

        # Save URL to settings
        logo_url = url_for('static', filename=f'uploads/{filename}')
        save_setting('company_logo_url', logo_url, 'url')

        return jsonify({"success": True, "url": logo_url})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/settings/reset-template", methods=["POST"])
def api_reset_packing_slip():
    """Reset packing slip template to defaults."""
    try:
        from orders_sync import DEFAULT_PACKING_SLIP_HTML, DEFAULT_PACKING_SLIP_CSS

        save_setting('packing_slip_html', DEFAULT_PACKING_SLIP_HTML, 'html')
        save_setting('packing_slip_css', DEFAULT_PACKING_SLIP_CSS, 'css')
        save_setting('packing_slip_js', '', 'js')

        return jsonify({
            "success": True,
            "html": DEFAULT_PACKING_SLIP_HTML,
            "css": DEFAULT_PACKING_SLIP_CSS,
            "js": ""
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/packing-slip/preview")
def api_packing_slip_preview():
    """Generate a preview of the packing slip with sample data."""
    settings = get_all_settings()

    # Sample data for preview
    sample_data = {
        "company_name": settings.get('company_name', 'Hemlock & Oak'),
        "company_logo": settings.get('company_logo_url', ''),
        "company_address": settings.get('company_address', ''),
        "order_number": "12345",
        "order_date": datetime.now().strftime("%B %d, %Y"),
        "shipping_name": "Jane Smith",
        "shipping_first_name": "Jane",
        "shipping_address1": "123 Main Street",
        "shipping_address2": "Apt 4B",
        "shipping_city": "Vancouver",
        "shipping_province": "BC",
        "shipping_zip": "V6B 1A1",
        "shipping_country": "Canada",
        "billing_name": "Jane Smith",
        "billing_first_name": "Jane",
        "billing_address1": "123 Main Street",
        "billing_address2": "",
        "billing_city": "Vancouver",
        "billing_province": "BC",
        "billing_zip": "V6B 1A1",
        "order_note": "Please leave at back door. Thank you!",
        "tracking_number": "1Z999AA10123456784",
        "line_items": [
            {
                "quantity": 2,
                "title": "2025 Planner - Botanical Edition",
                "variant_title": "Green / Weekly",
                "sku": "PLN-BOT-GRN-W",
                "properties": [
                    {"name": "Start Month", "value": "January 2025"},
                    {"name": "Personalization", "value": "Jane S."}
                ]
            },
            {
                "quantity": 1,
                "title": "Matching Pen Set",
                "variant_title": "Gold",
                "sku": "PEN-GOLD-3PK",
                "properties": []
            },
            {
                "quantity": 3,
                "title": "Sticky Notes - Floral Pack",
                "variant_title": "",
                "sku": "STK-FLR-100",
                "properties": []
            }
        ]
    }

    return jsonify({
        "success": True,
        "data": sample_data,
        "html": settings.get('packing_slip_html', ''),
        "css": settings.get('packing_slip_css', ''),
        "js": settings.get('packing_slip_js', ''),
        "label_width": settings.get('packing_slip_label_width', '4'),
        "label_height": settings.get('packing_slip_label_height', '6')
    })


# Available template variables for the packing slip
PACKING_SLIP_VARIABLES = {
    "Company Info": [
        ("{{company_name}}", "Company name"),
        ("{{company_logo}}", "Company logo URL"),
        ("{{company_address}}", "Company address"),
        ("{{company_phone}}", "Company phone"),
        ("{{company_email}}", "Company email"),
    ],
    "Order Info": [
        ("{{order_number}}", "Order number"),
        ("{{order_date}}", "Order date"),
        ("{{order_note}}", "Customer notes"),
        ("{{tracking_number}}", "Tracking number"),
    ],
    "Shipping Address": [
        ("{{shipping_name}}", "Recipient full name"),
        ("{{shipping_first_name}}", "Recipient first name only"),
        ("{{shipping_address1}}", "Address line 1"),
        ("{{shipping_address2}}", "Address line 2"),
        ("{{shipping_city}}", "City"),
        ("{{shipping_province}}", "Province/State"),
        ("{{shipping_zip}}", "Postal/ZIP code"),
        ("{{shipping_country}}", "Country"),
    ],
    "Billing Address": [
        ("{{billing_name}}", "Billing full name"),
        ("{{billing_first_name}}", "Billing first name only"),
        ("{{billing_address1}}", "Billing address 1"),
        ("{{billing_address2}}", "Billing address 2"),
        ("{{billing_city}}", "Billing city"),
        ("{{billing_province}}", "Billing province"),
        ("{{billing_zip}}", "Billing ZIP"),
    ],
    "Line Items (use in {{#each line_items}})": [
        ("{{this.quantity}}", "Item quantity"),
        ("{{this.quantity_circled}}", "Quantity circled if > 1"),
        ("{{this.title}}", "Product title"),
        ("{{this.variant_title}}", "Variant title"),
        ("{{this.sku}}", "Item SKU"),
        ("{{this.properties}}", "Customizations (loop with {{#each this.properties}})"),
    ],
    "Conditionals": [
        ("{{#if variable}}...{{/if}}", "Show content if variable exists"),
        ("{{#each array}}...{{/each}}", "Loop through array items"),
        ("{{#if this.quantity_gt_1}}...{{/if}}", "Show if quantity > 1"),
    ],
}


@app.route("/api/packing-slip/variables")
def api_packing_slip_variables():
    """Get available template variables."""
    return jsonify({"success": True, "variables": PACKING_SLIP_VARIABLES})


@app.route("/api/orders/sync", methods=["POST"])
def api_orders_sync():
    """
    Trigger a manual orders sync from Shopify.
    Query params:
        - full: If 'true', do full 90-day sync. Otherwise incremental.
        - async: If 'true', run in background (default for full sync)
        - resume: If 'true', try to resume an interrupted sync
    """
    full_sync = request.args.get('full', 'false').lower() == 'true'
    run_async = request.args.get('async', 'true' if full_sync else 'false').lower() == 'true'
    resume = request.args.get('resume', 'false').lower() == 'true'

    try:
        orders_sync = get_orders_sync()

        # Check if sync is already running
        status = orders_sync.get_sync_status()
        if status.get('status') == 'running':
            # If resume requested and sync is stale, allow it
            if resume and (status.get('can_resume') or status.get('status_hint') == 'interrupted'):
                print("Resuming interrupted sync...")
            else:
                return jsonify({
                    "success": False,
                    "error": "Sync already in progress. Please wait for it to complete.",
                    "status": "running",
                    "can_resume": status.get('can_resume', False)
                }), 409

        if run_async:
            # Run sync in background thread
            def run_sync():
                try:
                    orders_sync.sync_orders(full_sync=full_sync, days_back=90, resume=resume)
                except Exception as e:
                    print(f"Background orders sync error: {e}")
                    import traceback
                    traceback.print_exc()

            import threading
            sync_thread = threading.Thread(target=run_sync, daemon=True)
            sync_thread.start()

            return jsonify({
                "success": True,
                "synced_count": 0,
                "message": "Sync started in background. Check status for progress.",
                "async": True,
                "resume": resume
            })
        else:
            # Synchronous sync (for small incremental syncs)
            count, message = orders_sync.sync_orders(full_sync=full_sync, days_back=90, resume=resume)
            return jsonify({
                "success": True,
                "synced_count": count,
                "message": message
            })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@app.route("/api/orders/sync/status", methods=["GET"])
def api_orders_sync_status():
    """Get current orders sync status."""
    try:
        status = get_orders_sync().get_sync_status()
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/orders/<order_number>/details", methods=["GET"])
def api_get_order_details(order_number):
    """
    Get order details including line items, shipping address, customs info, and rate zones.
    Used for the order popup modal.
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()

        # Get the order with additional fields
        cursor.execute("""
            SELECT id, shopify_order_id, order_number, customer_name, customer_email,
                   customer_phone, shipping_address, total_price, subtotal_price,
                   total_tax, financial_status, fulfillment_status, tracking_number,
                   note, shopify_created_at, cancelled_at, total_weight_grams,
                   currency
            FROM orders
            WHERE order_number = %s
        """, (order_number,))
        order = cursor.fetchone()

        if not order:
            cursor.close()
            conn.close()
            return jsonify({"success": False, "error": "Order not found"}), 404

        # Get line items with customs info
        cursor.execute("""
            SELECT oli.id, oli.product_title, oli.variant_title, oli.sku, oli.quantity, oli.price, oli.grams,
                   COALESCE(pci.hs_code, oli.hs_code, '') as hs_code,
                   COALESCE(pci.customs_description, oli.customs_description, '') as customs_description,
                   COALESCE(pci.country_of_origin, oli.country_of_origin, 'CA') as country_of_origin,
                   COALESCE(pci.weight_grams, oli.grams, 0) as weight_grams
            FROM order_line_items oli
            LEFT JOIN product_customs_info pci ON pci.sku = oli.sku
            WHERE oli.order_id = %s
            ORDER BY oli.id
        """, (order['id'],))
        line_items = cursor.fetchall()

        # Get properties for each line item
        line_items_with_props = []
        total_items_qty = 0
        for item in line_items:
            cursor.execute("""
                SELECT name, value
                FROM order_line_item_options
                WHERE line_item_id = %s
                ORDER BY id
            """, (item['id'],))
            properties = cursor.fetchall()
            qty = item['quantity'] or 1
            total_items_qty += qty

            line_items_with_props.append({
                "title": item['product_title'] or '',
                "variant": item['variant_title'] or '',
                "sku": item['sku'] or '',
                "quantity": qty,
                "price": float(item['price']) if item.get('price') else 0,
                "weight_grams": item.get('weight_grams') or 0,
                "hs_code": item.get('hs_code') or '',
                "customs_description": item.get('customs_description') or '',
                "country_of_origin": item.get('country_of_origin') or 'CA',
                "properties": [{"name": p['name'], "value": p['value']} for p in properties]
            })

        cursor.close()
        conn.close()

        # Parse shipping address (stored as JSON string)
        shipping_address = {}
        if order.get('shipping_address'):
            try:
                shipping_address = json.loads(order['shipping_address'])
            except:
                shipping_address = {"raw": order['shipping_address']}

        # Determine rate zone based on destination country
        country = shipping_address.get('country', '') or shipping_address.get('country_code', '')
        country_upper = country.upper() if country else ''
        province = shipping_address.get('province_code', '') or shipping_address.get('province', '')

        # Calculate rate zone similar to ShipStation
        rate_zone = "Unknown"
        rate_zone_detail = ""
        if country_upper in ('CANADA', 'CA'):
            rate_zone = "Domestic"
            rate_zone_detail = f"Canada - {province}" if province else "Canada"
        elif country_upper in ('UNITED STATES', 'US', 'USA'):
            rate_zone = "USA"
            rate_zone_detail = f"United States - {province}" if province else "United States"
        elif country_upper:
            # International zones
            intl_zones = {
                # North America
                'MX': ('International - Zone A', 'Mexico'),
                'MEXICO': ('International - Zone A', 'Mexico'),
                # Europe
                'GB': ('International - Zone B', 'United Kingdom'),
                'UK': ('International - Zone B', 'United Kingdom'),
                'UNITED KINGDOM': ('International - Zone B', 'United Kingdom'),
                'DE': ('International - Zone B', 'Germany'),
                'GERMANY': ('International - Zone B', 'Germany'),
                'FR': ('International - Zone B', 'France'),
                'FRANCE': ('International - Zone B', 'France'),
                'IT': ('International - Zone B', 'Italy'),
                'ITALY': ('International - Zone B', 'Italy'),
                'ES': ('International - Zone B', 'Spain'),
                'SPAIN': ('International - Zone B', 'Spain'),
                'NL': ('International - Zone B', 'Netherlands'),
                'NETHERLANDS': ('International - Zone B', 'Netherlands'),
                'BE': ('International - Zone B', 'Belgium'),
                'BELGIUM': ('International - Zone B', 'Belgium'),
                'AT': ('International - Zone B', 'Austria'),
                'AUSTRIA': ('International - Zone B', 'Austria'),
                'CH': ('International - Zone B', 'Switzerland'),
                'SWITZERLAND': ('International - Zone B', 'Switzerland'),
                # Australia/NZ
                'AU': ('International - Zone C', 'Australia'),
                'AUSTRALIA': ('International - Zone C', 'Australia'),
                'NZ': ('International - Zone C', 'New Zealand'),
                'NEW ZEALAND': ('International - Zone C', 'New Zealand'),
                # Asia
                'JP': ('International - Zone C', 'Japan'),
                'JAPAN': ('International - Zone C', 'Japan'),
                'KR': ('International - Zone C', 'South Korea'),
                'SOUTH KOREA': ('International - Zone C', 'South Korea'),
                'SG': ('International - Zone C', 'Singapore'),
                'SINGAPORE': ('International - Zone C', 'Singapore'),
                'HK': ('International - Zone C', 'Hong Kong'),
                'HONG KONG': ('International - Zone C', 'Hong Kong'),
            }
            if country_upper in intl_zones:
                rate_zone, rate_zone_detail = intl_zones[country_upper]
            else:
                rate_zone = "International - Zone D"
                rate_zone_detail = country

        # Calculate total weight
        total_weight = order.get('total_weight_grams') or sum(item.get('weight_grams', 0) * item.get('quantity', 1) for item in line_items_with_props)

        return jsonify({
            "success": True,
            "order": {
                "order_number": order['order_number'],
                "customer_name": order['customer_name'],
                "customer_email": order['customer_email'],
                "customer_phone": order.get('customer_phone') or '',
                "shipping_address": shipping_address,
                "total_price": float(order['total_price']) if order.get('total_price') else 0,
                "subtotal_price": float(order['subtotal_price']) if order.get('subtotal_price') else 0,
                "total_tax": float(order['total_tax']) if order.get('total_tax') else 0,
                "currency": order.get('currency') or 'CAD',
                "financial_status": order.get('financial_status') or '',
                "fulfillment_status": order.get('fulfillment_status') or 'unfulfilled',
                "tracking_number": order.get('tracking_number') or '',
                "note": order.get('note') or '',
                "created_at": order['shopify_created_at'].isoformat() if order.get('shopify_created_at') else '',
                "cancelled": order.get('cancelled_at') is not None,
                "total_weight_grams": total_weight,
                "total_items_qty": total_items_qty
            },
            "line_items": line_items_with_props,
            "rate_zone": {
                "zone": rate_zone,
                "detail": rate_zone_detail,
                "is_international": rate_zone != "Domestic"
            }
        })

    except Exception as e:
        try:
            conn.close()
        except:
            pass
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/orders/<order_number>/packing-slip", methods=["GET"])
def api_get_packing_slip(order_number):
    """
    Generate a printable packing slip for an order.
    Returns HTML that can be printed in a new window.
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()

        # Get the order
        cursor.execute("""
            SELECT id, shopify_order_id, order_number, customer_name, customer_email,
                   customer_phone, shipping_address, total_price, subtotal_price,
                   total_tax, financial_status, fulfillment_status, tracking_number,
                   note, shopify_created_at, currency
            FROM orders
            WHERE order_number = %s
        """, (order_number,))
        order = cursor.fetchone()

        if not order:
            cursor.close()
            conn.close()
            return "<html><body><h1>Order not found</h1></body></html>", 404

        # Get line items
        cursor.execute("""
            SELECT id, product_title, variant_title, sku, quantity, price
            FROM order_line_items
            WHERE order_id = %s
            ORDER BY id
        """, (order['id'],))
        line_items = cursor.fetchall()

        # Get properties for each line item
        line_items_with_props = []
        for item in line_items:
            cursor.execute("""
                SELECT name, value
                FROM order_line_item_options
                WHERE line_item_id = %s
                ORDER BY id
            """, (item['id'],))
            properties = cursor.fetchall()

            line_items_with_props.append({
                "title": item['product_title'] or '',
                "variant": item['variant_title'] or '',
                "sku": item['sku'] or '',
                "quantity": item['quantity'] or 1,
                "price": float(item['price']) if item.get('price') else 0,
                "properties": [{"name": p['name'], "value": p['value']} for p in properties]
            })

        cursor.close()
        conn.close()

        # Parse shipping address
        shipping_address = {}
        if order.get('shipping_address'):
            try:
                import json
                shipping_address = json.loads(order['shipping_address'])
            except:
                shipping_address = {"raw": order['shipping_address']}

        # Format the address for display
        address_lines = []
        if shipping_address:
            if shipping_address.get('raw'):
                address_lines = [shipping_address['raw']]
            else:
                if shipping_address.get('name'):
                    address_lines.append(shipping_address['name'])
                if shipping_address.get('address1'):
                    address_lines.append(shipping_address['address1'])
                if shipping_address.get('address2'):
                    address_lines.append(shipping_address['address2'])
                city_line = []
                if shipping_address.get('city'):
                    city_line.append(shipping_address['city'])
                if shipping_address.get('province_code') or shipping_address.get('province'):
                    city_line.append(shipping_address.get('province_code') or shipping_address.get('province'))
                if shipping_address.get('zip'):
                    city_line.append(shipping_address['zip'])
                if city_line:
                    address_lines.append(', '.join(city_line))
                if shipping_address.get('country'):
                    address_lines.append(shipping_address['country'])

        # Format order date
        order_date = ''
        if order.get('shopify_created_at'):
            order_date = order['shopify_created_at'].strftime('%B %d, %Y at %I:%M %p')

        # Build line items HTML
        items_html = ''
        for item in line_items_with_props:
            props_html = ''
            if item['properties']:
                props_html = '<div class="item-props">'
                for p in item['properties']:
                    props_html += f'<div class="prop"><span class="prop-name">{p["name"]}:</span> {p["value"]}</div>'
                props_html += '</div>'

            # Highlight quantities > 1 with a circle
            qty = item["quantity"]
            qty_class = "item-qty highlight-qty" if qty > 1 else "item-qty"

            items_html += f'''
            <tr>
                <td class="{qty_class}"><span class="qty-number">{qty}</span></td>
                <td class="item-details">
                    <div class="item-title">{item["title"]}</div>
                    {f'<div class="item-variant">{item["variant"]}</div>' if item["variant"] else ''}
                    {f'<div class="item-sku">SKU: {item["sku"]}</div>' if item["sku"] else ''}
                    {props_html}
                </td>
            </tr>
            '''

        # Generate the packing slip HTML
        html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Packing Slip - Order #{order['order_number']}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            font-size: 12pt;
            line-height: 1.4;
            padding: 20px;
            max-width: 800px;
            margin: 0 auto;
        }}
        @media print {{
            body {{
                padding: 0;
            }}
            .no-print {{
                display: none !important;
            }}
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            border-bottom: 2px solid #333;
            padding-bottom: 15px;
            margin-bottom: 20px;
        }}
        .company-name {{
            font-size: 24pt;
            font-weight: bold;
            color: #333;
        }}
        .order-info {{
            text-align: right;
        }}
        .order-number {{
            font-size: 18pt;
            font-weight: bold;
            color: #534bc4;
        }}
        .order-date {{
            color: #666;
            margin-top: 4px;
        }}
        .addresses {{
            display: flex;
            gap: 40px;
            margin-bottom: 25px;
        }}
        .address-block {{
            flex: 1;
        }}
        .address-label {{
            font-weight: bold;
            color: #666;
            text-transform: uppercase;
            font-size: 10pt;
            margin-bottom: 8px;
            border-bottom: 1px solid #ddd;
            padding-bottom: 4px;
        }}
        .address-content {{
            line-height: 1.6;
        }}
        .items-table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 20px;
        }}
        .items-table th {{
            background: #f5f5f5;
            padding: 10px 12px;
            text-align: left;
            font-weight: 600;
            border-bottom: 2px solid #ddd;
        }}
        .items-table td {{
            padding: 12px;
            border-bottom: 1px solid #eee;
            vertical-align: top;
        }}
        .item-qty {{
            width: 60px;
            text-align: center;
            font-weight: bold;
            font-size: 14pt;
        }}
        .item-qty .qty-number {{
            display: inline-block;
            min-width: 28px;
            padding: 4px 8px;
        }}
        .highlight-qty .qty-number {{
            border: 3px solid #c62828;
            border-radius: 50%;
            background: #ffebee;
            color: #c62828;
            font-size: 16pt;
            min-width: 36px;
            padding: 6px 10px;
        }}
        .item-title {{
            font-weight: 600;
            margin-bottom: 2px;
        }}
        .item-variant {{
            color: #666;
            font-size: 11pt;
        }}
        .item-sku {{
            color: #999;
            font-size: 10pt;
            margin-top: 2px;
        }}
        .item-props {{
            margin-top: 8px;
            padding: 8px;
            background: #f9f9f9;
            border-radius: 4px;
            font-size: 10pt;
        }}
        .prop {{
            margin-bottom: 3px;
        }}
        .prop-name {{
            color: #888;
        }}
        .notes {{
            background: #fffbeb;
            border: 1px solid #fcd34d;
            border-radius: 6px;
            padding: 12px;
            margin-bottom: 20px;
        }}
        .notes-label {{
            font-weight: bold;
            margin-bottom: 4px;
        }}
        .footer {{
            margin-top: 30px;
            padding-top: 15px;
            border-top: 1px solid #ddd;
            text-align: center;
            color: #666;
            font-size: 10pt;
        }}
        .print-btn {{
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 12px 24px;
            background: #534bc4;
            color: white;
            border: none;
            border-radius: 6px;
            font-size: 14px;
            cursor: pointer;
        }}
        .print-btn:hover {{
            background: #4338b8;
        }}
    </style>
</head>
<body>
    <button class="print-btn no-print" onclick="window.print()">Print Packing Slip</button>

    <div class="header">
        <div class="company-name">H&O Sportswear</div>
        <div class="order-info">
            <div class="order-number">Order #{order['order_number']}</div>
            <div class="order-date">{order_date}</div>
        </div>
    </div>

    <div class="addresses">
        <div class="address-block">
            <div class="address-label">Ship To</div>
            <div class="address-content">
                {'<br>'.join(address_lines) if address_lines else 'No address provided'}
            </div>
        </div>
        <div class="address-block">
            <div class="address-label">Contact</div>
            <div class="address-content">
                {order['customer_name'] or 'Customer'}<br>
                {order['customer_email'] or ''}<br>
                {order.get('customer_phone') or ''}
            </div>
        </div>
    </div>

    {f'<div class="notes"><div class="notes-label">Order Notes:</div>{order["note"]}</div>' if order.get('note') else ''}

    <table class="items-table">
        <thead>
            <tr>
                <th style="width: 60px; text-align: center;">Qty</th>
                <th>Item</th>
            </tr>
        </thead>
        <tbody>
            {items_html}
        </tbody>
    </table>

    <div class="footer">
        Thank you for your order!<br>
        {order.get('tracking_number') or ''}
    </div>
</body>
</html>'''

        return html, 200, {'Content-Type': 'text/html'}

    except Exception as e:
        try:
            conn.close()
        except:
            pass
        return f"<html><body><h1>Error</h1><p>{str(e)}</p></body></html>", 500


@app.route("/api/orders/<order_number>/customs-info", methods=["GET"])
def api_get_customs_info(order_number):
    """
    Get customs information for an order's line items.
    Returns whether the order is international and customs details for each item.
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()

        # Get the order
        cursor.execute("""
            SELECT id, shipping_address, total_weight_grams
            FROM orders
            WHERE order_number = %s
        """, (order_number,))
        order = cursor.fetchone()

        if not order:
            cursor.close()
            conn.close()
            return jsonify({"success": False, "error": "Order not found"}), 404

        # Check if international
        is_international = False
        destination_country = "Canada"
        shipping_address = {}
        if order.get('shipping_address'):
            try:
                shipping_address = json.loads(order['shipping_address'])
                country = shipping_address.get('country', '').upper()
                destination_country = shipping_address.get('country', 'Canada')
                if country and country not in ['CANADA', 'CA']:
                    is_international = True
            except:
                pass

        # Get line items with customs info
        cursor.execute("""
            SELECT id, product_title, variant_title, sku, quantity, price, grams,
                   hs_code, country_of_origin, customs_description
            FROM order_line_items
            WHERE order_id = %s
            ORDER BY id
        """, (order['id'],))
        line_items = cursor.fetchall()

        items_data = []
        for item in line_items:
            items_data.append({
                "id": item['id'],
                "title": item['product_title'] or '',
                "variant": item['variant_title'] or '',
                "sku": item['sku'] or '',
                "quantity": item['quantity'] or 1,
                "price": float(item['price']) if item.get('price') else 0,
                "weight_grams": item.get('grams') or 0,
                "hs_code": item.get('hs_code') or '',
                "country_of_origin": item.get('country_of_origin') or 'CA',
                "customs_description": item.get('customs_description') or item['product_title'] or ''
            })

        cursor.close()
        conn.close()

        return jsonify({
            "success": True,
            "is_international": is_international,
            "destination_country": destination_country,
            "total_weight_grams": order.get('total_weight_grams') or 0,
            "items": items_data
        })

    except Exception as e:
        try:
            conn.close()
        except:
            pass
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/orders/<order_number>/customs-info", methods=["POST"])
def api_update_customs_info(order_number):
    """
    Update customs information for order line items.

    Request body:
    {
        "items": [
            {
                "id": 123,
                "hs_code": "4901.99",
                "country_of_origin": "CA",
                "customs_description": "Printed paper planner"
            }
        ]
    }
    """
    data = request.get_json()
    if not data or 'items' not in data:
        return jsonify({"success": False, "error": "Missing items data"}), 400

    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()

        # Verify order exists
        cursor.execute("SELECT id FROM orders WHERE order_number = %s", (order_number,))
        order = cursor.fetchone()
        if not order:
            cursor.close()
            conn.close()
            return jsonify({"success": False, "error": "Order not found"}), 404

        # Update each line item
        updated_count = 0
        for item in data['items']:
            if 'id' not in item:
                continue

            cursor.execute("""
                UPDATE order_line_items
                SET hs_code = %s,
                    country_of_origin = %s,
                    customs_description = %s
                WHERE id = %s AND order_id = %s
            """, (
                item.get('hs_code') or None,
                item.get('country_of_origin') or 'CA',
                item.get('customs_description') or None,
                item['id'],
                order['id']
            ))
            updated_count += cursor.rowcount

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({"success": True, "updated_count": updated_count})

    except Exception as e:
        try:
            conn.rollback()
            conn.close()
        except:
            pass
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/orders/<order_number>/customs-form", methods=["GET"])
def api_get_customs_form(order_number):
    """
    Generate a printable customs declaration form for an international order.
    Returns HTML that can be printed.
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()

        # Get the order
        cursor.execute("""
            SELECT id, order_number, customer_name, customer_email,
                   shipping_address, total_price, total_weight_grams, currency
            FROM orders
            WHERE order_number = %s
        """, (order_number,))
        order = cursor.fetchone()

        if not order:
            cursor.close()
            conn.close()
            return "<html><body><h1>Order not found</h1></body></html>", 404

        # Parse shipping address
        shipping_address = {}
        if order.get('shipping_address'):
            try:
                shipping_address = json.loads(order['shipping_address'])
            except:
                pass

        # Get line items with customs info
        cursor.execute("""
            SELECT id, product_title, variant_title, sku, quantity, price, grams,
                   hs_code, country_of_origin, customs_description
            FROM order_line_items
            WHERE order_id = %s
            ORDER BY id
        """, (order['id'],))
        line_items = cursor.fetchall()

        cursor.close()
        conn.close()

        # Calculate totals
        total_items = sum(item.get('quantity', 1) for item in line_items)
        total_value = sum(float(item.get('price', 0)) * (item.get('quantity', 1)) for item in line_items)
        total_weight = order.get('total_weight_grams') or 0

        # Build items HTML
        items_html = ""
        for i, item in enumerate(line_items, 1):
            qty = item.get('quantity', 1)
            price = float(item.get('price', 0))
            weight_g = item.get('grams') or 0
            description = item.get('customs_description') or item.get('product_title') or 'Goods'
            hs_code = item.get('hs_code') or ''
            origin = item.get('country_of_origin') or 'CA'

            items_html += f'''
            <tr>
                <td style="text-align: center;">{i}</td>
                <td>{description}</td>
                <td style="text-align: center;">{qty}</td>
                <td style="text-align: right;">{weight_g}g</td>
                <td style="text-align: right;">${price:.2f} {order.get('currency', 'CAD')}</td>
                <td style="text-align: center;">{origin}</td>
                <td style="text-align: center;">{hs_code}</td>
            </tr>'''

        # Sender info - you can customize this
        sender_name = "Your Company Name"
        sender_address = "123 Your Street, City, Province, Postal Code, Canada"

        # Recipient info
        recipient_name = shipping_address.get('name', order.get('customer_name', ''))
        recipient_addr_parts = []
        if shipping_address.get('address1'):
            recipient_addr_parts.append(shipping_address['address1'])
        if shipping_address.get('address2'):
            recipient_addr_parts.append(shipping_address['address2'])
        city_line = ', '.join(filter(None, [
            shipping_address.get('city'),
            shipping_address.get('province') or shipping_address.get('province_code'),
            shipping_address.get('zip')
        ]))
        if city_line:
            recipient_addr_parts.append(city_line)
        if shipping_address.get('country'):
            recipient_addr_parts.append(shipping_address['country'])
        recipient_address = '<br>'.join(recipient_addr_parts)

        html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Customs Declaration - Order #{order_number}</title>
    <style>
        @media print {{
            body {{ margin: 0; }}
            .no-print {{ display: none; }}
        }}
        body {{
            font-family: Arial, sans-serif;
            font-size: 11pt;
            max-width: 8.5in;
            margin: 0 auto;
            padding: 20px;
            color: #000;
        }}
        .header {{
            text-align: center;
            border-bottom: 2px solid #000;
            padding-bottom: 10px;
            margin-bottom: 20px;
        }}
        .header h1 {{
            margin: 0;
            font-size: 18pt;
            text-transform: uppercase;
        }}
        .header h2 {{
            margin: 5px 0 0 0;
            font-size: 12pt;
            font-weight: normal;
        }}
        .address-section {{
            display: flex;
            gap: 40px;
            margin-bottom: 20px;
        }}
        .address-box {{
            flex: 1;
            border: 1px solid #000;
            padding: 10px;
        }}
        .address-box h3 {{
            margin: 0 0 8px 0;
            font-size: 10pt;
            text-transform: uppercase;
            border-bottom: 1px solid #ccc;
            padding-bottom: 4px;
        }}
        .address-box p {{
            margin: 0;
            line-height: 1.4;
        }}
        .contents-table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 20px;
        }}
        .contents-table th {{
            background: #f0f0f0;
            border: 1px solid #000;
            padding: 6px 8px;
            text-align: left;
            font-size: 9pt;
            text-transform: uppercase;
        }}
        .contents-table td {{
            border: 1px solid #000;
            padding: 6px 8px;
            font-size: 10pt;
        }}
        .totals {{
            display: flex;
            justify-content: flex-end;
            gap: 30px;
            margin-bottom: 20px;
            font-size: 11pt;
        }}
        .totals div {{
            padding: 8px 12px;
            border: 1px solid #000;
        }}
        .totals strong {{
            margin-right: 10px;
        }}
        .declaration {{
            border: 1px solid #000;
            padding: 15px;
            margin-bottom: 20px;
            font-size: 10pt;
        }}
        .declaration h3 {{
            margin: 0 0 10px 0;
            font-size: 11pt;
        }}
        .signature-section {{
            display: flex;
            gap: 40px;
            margin-top: 30px;
        }}
        .signature-box {{
            flex: 1;
        }}
        .signature-line {{
            border-bottom: 1px solid #000;
            height: 40px;
            margin-bottom: 5px;
        }}
        .signature-label {{
            font-size: 9pt;
            color: #666;
        }}
        .print-btn {{
            position: fixed;
            top: 10px;
            right: 10px;
            padding: 10px 20px;
            background: #534bc4;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12pt;
        }}
        .print-btn:hover {{
            background: #3f3a99;
        }}
    </style>
</head>
<body>
    <button class="print-btn no-print" onclick="window.print()">Print Form</button>

    <div class="header">
        <h1>Customs Declaration</h1>
        <h2>CN 22 / Commercial Invoice</h2>
        <p style="margin-top: 8px;">Order #{order_number}</p>
    </div>

    <div class="address-section">
        <div class="address-box">
            <h3>From (Sender)</h3>
            <p>{sender_name}<br>{sender_address}</p>
        </div>
        <div class="address-box">
            <h3>To (Recipient)</h3>
            <p>{recipient_name}<br>{recipient_address}</p>
        </div>
    </div>

    <h3 style="margin-bottom: 10px;">Contents Description</h3>
    <table class="contents-table">
        <thead>
            <tr>
                <th style="width: 30px;">#</th>
                <th>Description of Contents</th>
                <th style="width: 50px; text-align: center;">Qty</th>
                <th style="width: 70px; text-align: right;">Weight</th>
                <th style="width: 90px; text-align: right;">Value</th>
                <th style="width: 60px; text-align: center;">Origin</th>
                <th style="width: 80px; text-align: center;">HS Code</th>
            </tr>
        </thead>
        <tbody>
            {items_html}
        </tbody>
    </table>

    <div class="totals">
        <div><strong>Total Items:</strong> {total_items}</div>
        <div><strong>Total Weight:</strong> {total_weight}g ({total_weight / 1000:.2f}kg)</div>
        <div><strong>Total Value:</strong> ${total_value:.2f} {order.get('currency', 'CAD')}</div>
    </div>

    <div class="declaration">
        <h3>Customs Declaration</h3>
        <p>I, the undersigned, certify that the information given in this customs declaration is true and correct and that this parcel does not contain any dangerous article or articles prohibited by legislation or by postal or customs regulations.</p>
        <p style="margin-top: 10px;"><strong>Category of Item:</strong> ☑ Sale of Goods &nbsp;&nbsp; ☐ Gift &nbsp;&nbsp; ☐ Documents &nbsp;&nbsp; ☐ Commercial Sample &nbsp;&nbsp; ☐ Return</p>
    </div>

    <div class="signature-section">
        <div class="signature-box">
            <div class="signature-line"></div>
            <div class="signature-label">Signature of Sender</div>
        </div>
        <div class="signature-box">
            <div class="signature-line"></div>
            <div class="signature-label">Date</div>
        </div>
    </div>

</body>
</html>'''

        return html, 200, {'Content-Type': 'text/html'}

    except Exception as e:
        try:
            conn.close()
        except:
            pass
        return f"<html><body><h1>Error</h1><p>{str(e)}</p></body></html>", 500


# ═══════════════════════════════════════════════════════════════════════════════
# Rate Shopping & Shipping Rates API
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/orders/<order_number>/rates", methods=["GET"])
def api_get_shipping_rates(order_number):
    """
    Get shipping rates from all carriers for an order.
    Returns rates from UPS and Canada Post sorted by price.
    """
    from rate_shopping import get_rate_shopping_service

    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()

        # Get order details
        cursor.execute("""
            SELECT id, shipping_address, total_weight_grams
            FROM orders
            WHERE order_number = %s
        """, (order_number,))
        order = cursor.fetchone()

        if not order:
            cursor.close()
            conn.close()
            return jsonify({"success": False, "error": "Order not found"}), 404

        # Parse shipping address
        shipping_address = {}
        if order.get('shipping_address'):
            try:
                shipping_address = json.loads(order['shipping_address'])
            except:
                pass

        cursor.close()
        conn.close()

        # Build destination from shipping address
        destination = {
            "name": shipping_address.get("name", "Customer"),
            "address_line1": shipping_address.get("address1", ""),
            "address_line2": shipping_address.get("address2", ""),
            "city": shipping_address.get("city", ""),
            "state": shipping_address.get("province_code") or shipping_address.get("province", ""),
            "postal_code": shipping_address.get("zip", ""),
            "country_code": shipping_address.get("country_code") or "CA"
        }

        # Build package (using order weight or default)
        weight_kg = (order.get("total_weight_grams") or 500) / 1000.0
        packages = [{
            "weight_kg": weight_kg,
            "length_cm": 25,
            "width_cm": 18,
            "height_cm": 5
        }]

        # Get customs data for international orders
        is_international = destination["country_code"].upper() not in ["CA", "CANADA"]
        customs_items = None

        rate_service = get_rate_shopping_service(get_mysql_connection)

        if is_international:
            customs_items = rate_service.get_customs_data_for_order(order["id"])

        # Get rates from all carriers
        result = rate_service.get_all_rates(
            destination=destination,
            packages=packages,
            customs_items=customs_items
        )

        return jsonify(result)

    except Exception as e:
        try:
            conn.close()
        except:
            pass
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/hs-codes", methods=["GET"])
def api_get_hs_codes():
    """
    Get list of HS codes from the reference table.
    Supports filtering by category or search term.
    """
    category = request.args.get('category', '')
    search = request.args.get('search', '')

    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()

        if search:
            cursor.execute("""
                SELECT hs_code, description, category, notes
                FROM hs_code_reference
                WHERE hs_code LIKE %s OR description ILIKE %s OR category ILIKE %s
                ORDER BY category, hs_code
            """, (f"%{search}%", f"%{search}%", f"%{search}%"))
        elif category:
            cursor.execute("""
                SELECT hs_code, description, category, notes
                FROM hs_code_reference
                WHERE category = %s
                ORDER BY hs_code
            """, (category,))
        else:
            cursor.execute("""
                SELECT hs_code, description, category, notes
                FROM hs_code_reference
                ORDER BY category, hs_code
            """)

        codes = cursor.fetchall()

        # Also get list of categories
        cursor.execute("SELECT DISTINCT category FROM hs_code_reference ORDER BY category")
        categories = [row['category'] for row in cursor.fetchall()]

        cursor.close()
        conn.close()

        return jsonify({
            "success": True,
            "codes": codes,
            "categories": categories
        })

    except Exception as e:
        try:
            conn.close()
        except:
            pass
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/hs-codes", methods=["POST"])
def api_add_hs_code():
    """
    Add a new HS code to the reference table.
    """
    data = request.get_json()
    if not data or not data.get('hs_code') or not data.get('description'):
        return jsonify({"success": False, "error": "Missing required fields"}), 400

    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO hs_code_reference (hs_code, description, category, notes)
            VALUES (%s, %s, %s, %s)
            RETURNING id
        """, (
            data.get('hs_code'),
            data.get('description'),
            data.get('category', ''),
            data.get('notes', '')
        ))

        result = cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({"success": True, "id": result['id']})

    except Exception as e:
        try:
            conn.rollback()
            conn.close()
        except:
            pass
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/product-customs", methods=["GET"])
def api_get_product_customs():
    """
    Get product customs info for all or specific SKUs.
    """
    sku = request.args.get('sku', '')

    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()

        if sku:
            cursor.execute("""
                SELECT id, sku, product_title, customs_description, hs_code, hs_code_us,
                       country_of_origin, weight_grams, created_at, updated_at
                FROM product_customs_info
                WHERE sku = %s
            """, (sku,))
        else:
            cursor.execute("""
                SELECT id, sku, product_title, customs_description, hs_code, hs_code_us,
                       country_of_origin, weight_grams, created_at, updated_at
                FROM product_customs_info
                ORDER BY sku
            """)

        products = cursor.fetchall()
        cursor.close()
        conn.close()

        return jsonify({"success": True, "products": products})

    except Exception as e:
        try:
            conn.close()
        except:
            pass
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/product-customs", methods=["POST"])
def api_save_product_customs():
    """
    Save or update product customs info for a SKU.
    Can be used to set defaults that auto-fill when creating shipments.

    Request body:
    {
        "sku": "PLN-2025-GRN",
        "product_title": "2025 Planner",
        "customs_description": "Bound paper planner",
        "hs_code": "4820102010",
        "hs_code_us": "",
        "country_of_origin": "CA",
        "weight_grams": 350
    }
    """
    data = request.get_json()
    if not data or not data.get('sku'):
        return jsonify({"success": False, "error": "Missing SKU"}), 400

    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()

        # Upsert - insert or update on conflict
        cursor.execute("""
            INSERT INTO product_customs_info
                (sku, product_title, customs_description, hs_code, hs_code_us,
                 country_of_origin, weight_grams, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (sku) DO UPDATE SET
                product_title = EXCLUDED.product_title,
                customs_description = EXCLUDED.customs_description,
                hs_code = EXCLUDED.hs_code,
                hs_code_us = EXCLUDED.hs_code_us,
                country_of_origin = EXCLUDED.country_of_origin,
                weight_grams = EXCLUDED.weight_grams,
                updated_at = CURRENT_TIMESTAMP
            RETURNING id
        """, (
            data.get('sku'),
            data.get('product_title') or None,
            data.get('customs_description') or data.get('product_title') or 'Goods',
            data.get('hs_code') or '4820102010',
            data.get('hs_code_us') or None,
            data.get('country_of_origin') or 'CA',
            data.get('weight_grams') or None
        ))

        result = cursor.fetchone()
        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({"success": True, "id": result['id']})

    except Exception as e:
        try:
            conn.rollback()
            conn.close()
        except:
            pass
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/product-customs/<sku>", methods=["DELETE"])
def api_delete_product_customs(sku):
    """Delete product customs info for a SKU."""
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("DELETE FROM product_customs_info WHERE sku = %s", (sku,))
        deleted = cursor.rowcount

        conn.commit()
        cursor.close()
        conn.close()

        if deleted > 0:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "SKU not found"}), 404

    except Exception as e:
        try:
            conn.rollback()
            conn.close()
        except:
            pass
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/orders/<order_number>/save-customs-defaults", methods=["POST"])
def api_save_customs_defaults(order_number):
    """
    Save the customs info from an order's line items as defaults for those SKUs.
    This allows quickly setting up product customs info from existing orders.
    """
    conn = get_mysql_connection()
    try:
        cursor = conn.cursor()

        # Get the order
        cursor.execute("SELECT id FROM orders WHERE order_number = %s", (order_number,))
        order = cursor.fetchone()

        if not order:
            cursor.close()
            conn.close()
            return jsonify({"success": False, "error": "Order not found"}), 404

        # Get line items with customs info
        cursor.execute("""
            SELECT sku, product_title, grams, hs_code, country_of_origin, customs_description
            FROM order_line_items
            WHERE order_id = %s AND sku IS NOT NULL AND sku != ''
        """, (order['id'],))
        line_items = cursor.fetchall()

        saved_count = 0
        for item in line_items:
            if item.get('hs_code'):  # Only save if HS code is set
                cursor.execute("""
                    INSERT INTO product_customs_info
                        (sku, product_title, customs_description, hs_code, country_of_origin, weight_grams, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (sku) DO UPDATE SET
                        product_title = COALESCE(EXCLUDED.product_title, product_customs_info.product_title),
                        customs_description = COALESCE(EXCLUDED.customs_description, product_customs_info.customs_description),
                        hs_code = EXCLUDED.hs_code,
                        country_of_origin = EXCLUDED.country_of_origin,
                        weight_grams = COALESCE(EXCLUDED.weight_grams, product_customs_info.weight_grams),
                        updated_at = CURRENT_TIMESTAMP
                """, (
                    item.get('sku'),
                    item.get('product_title'),
                    item.get('customs_description') or item.get('product_title'),
                    item.get('hs_code'),
                    item.get('country_of_origin') or 'CA',
                    item.get('grams')
                ))
                saved_count += 1

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({
            "success": True,
            "saved_count": saved_count,
            "message": f"Saved customs defaults for {saved_count} product(s)"
        })

    except Exception as e:
        try:
            conn.rollback()
            conn.close()
        except:
            pass
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/orders/<order_number>/cancel", methods=["POST"])
def api_cancel_order(order_number):
    """
    Cancel an order - locally and optionally in Shopify with refund.

    Request body:
    {
        "reason": "customer_cancelled",  # customer_cancelled, duplicate_order, fraud, refund_requested, other
        "reason_notes": "Optional notes",
        "cancelled_by": "Jess",
        "cancel_in_shopify": true,      # Whether to cancel in Shopify
        "issue_refund": true,           # Whether to issue refund
        "email_customer": true,         # Whether to email customer
        "restock_inventory": true       # Whether to restock inventory
    }
    """
    conn = get_mysql_connection()
    try:
        data = request.json or {}
        cursor = conn.cursor()

        # Get the order
        cursor.execute("""
            SELECT id, shopify_order_id, order_number, customer_name, customer_email,
                   tracking_number, total_price
            FROM orders
            WHERE order_number = %s AND cancelled_at IS NULL
        """, (order_number,))
        order = cursor.fetchone()

        if not order:
            return jsonify({"success": False, "error": "Order not found or already cancelled"}), 404

        reason = data.get('reason', 'other')
        reason_notes = data.get('reason_notes', '')
        cancelled_by = data.get('cancelled_by', '')
        cancel_in_shopify = data.get('cancel_in_shopify', False)
        issue_refund = data.get('issue_refund', False)
        email_customer = data.get('email_customer', True)
        restock_inventory = data.get('restock_inventory', True)

        shopify_order_id = order.get('shopify_order_id')
        shopify_cancel_result = None
        shopify_refund_result = None
        refund_amount = None

        # Cancel in Shopify if requested and we have a Shopify order ID
        if cancel_in_shopify and shopify_order_id:
            try:
                shopify_api = get_shopify_api()

                # If refund requested, do refund first (before cancelling)
                if issue_refund:
                    print(f"[Cancel] Creating refund for Shopify order {shopify_order_id}")
                    shopify_refund_result = shopify_api.create_refund(shopify_order_id, notify_customer=email_customer)
                    if shopify_refund_result.get('success'):
                        refund_amount = shopify_refund_result.get('total_refunded', 0)
                        print(f"[Cancel] Refund successful: ${refund_amount:.2f}")
                    else:
                        print(f"[Cancel] Refund failed: {shopify_refund_result.get('error')}")

                # Cancel the order in Shopify
                print(f"[Cancel] Cancelling Shopify order {shopify_order_id}")
                shopify_cancel_result = shopify_api.cancel_order(
                    shopify_order_id,
                    reason=reason,
                    email_customer=email_customer,
                    restock=restock_inventory
                )

                if not shopify_cancel_result.get('success'):
                    # If Shopify cancellation failed, return error but still record locally
                    print(f"[Cancel] Shopify cancellation failed: {shopify_cancel_result.get('error')}")

            except Exception as e:
                print(f"[Cancel] Shopify API error: {e}")
                shopify_cancel_result = {"success": False, "error": str(e)}

        # Insert into cancelled_orders table
        cursor.execute("""
            INSERT INTO cancelled_orders (
                order_id, shopify_order_id, order_number, tracking_number,
                customer_name, customer_email, reason, reason_notes, cancelled_by,
                refund_amount, refund_issued, shopify_refund_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            order['id'],
            order['shopify_order_id'],
            order['order_number'],
            order.get('tracking_number'),
            order['customer_name'],
            order['customer_email'],
            reason,
            reason_notes,
            cancelled_by,
            refund_amount,
            1 if (shopify_refund_result and shopify_refund_result.get('success')) else 0,
            shopify_refund_result.get('refund_id') if shopify_refund_result else None
        ))

        # Update the orders table to mark as cancelled
        cursor.execute("""
            UPDATE orders
            SET cancelled_at = CURRENT_TIMESTAMP,
                cancel_reason = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (reason, order['id']))

        conn.commit()

        # Build response
        response_data = {
            "success": True,
            "message": f"Order #{order_number} cancelled successfully",
            "order_number": order_number,
            "cancelled_locally": True
        }

        if cancel_in_shopify:
            response_data["shopify_cancelled"] = shopify_cancel_result.get('success', False) if shopify_cancel_result else False
            if shopify_cancel_result and not shopify_cancel_result.get('success'):
                response_data["shopify_error"] = shopify_cancel_result.get('error')

        if issue_refund:
            response_data["refund_issued"] = shopify_refund_result.get('success', False) if shopify_refund_result else False
            if shopify_refund_result and shopify_refund_result.get('success'):
                response_data["refund_amount"] = refund_amount
            elif shopify_refund_result:
                response_data["refund_error"] = shopify_refund_result.get('error')

        return jsonify(response_data)

    except Exception as e:
        print(f"Error cancelling order {order_number}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        try:
            cursor.close()
        except Exception:
            pass
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# ── Initialize Background Scheduler ──
# ══════════════════════════════════════════════════════════════════════════════
# Start the background scheduler for automatic tracking updates.
# This runs whether the app is started via gunicorn or directly with python.
# The scheduler handles its own duplicate prevention.
init_background_scheduler()


if __name__ == "__main__":
    # Debug mode disabled by default to prevent auto-reloader from killing long-running syncs
    # Set FLASK_DEBUG=true in environment to enable debug mode for local development
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=debug_mode)
