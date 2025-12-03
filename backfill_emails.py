#!/usr/bin/env python3
"""
Backfill customer emails for existing scans.
Fetches emails from ShipStation and Shopify for scans that are missing customer_email.
"""

import os
import sys
import time
import requests
from datetime import datetime

# Add parent directory to path to import modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shopify_api import ShopifyAPI
import mysql.connector
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# MySQL connection
def get_mysql_connection():
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "parcel_scanner"),
        port=int(os.environ.get("DB_PORT", 3306))
    )

# ShipStation credentials
SHIPSTATION_API_KEY = os.environ.get("SHIPSTATION_API_KEY", "")
SHIPSTATION_API_SECRET = os.environ.get("SHIPSTATION_API_SECRET", "")

def fetch_email_from_shipstation(tracking_number):
    """Fetch customer email from ShipStation."""
    if not SHIPSTATION_API_KEY or not SHIPSTATION_API_SECRET:
        return None

    try:
        url = f"https://ssapi.shipstation.com/shipments?trackingNumber={tracking_number}"
        resp = requests.get(
            url,
            auth=(SHIPSTATION_API_KEY, SHIPSTATION_API_SECRET),
            headers={"Accept": "application/json"},
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        shipments = data.get("shipments", [])

        if shipments:
            first = shipments[0]

            # Try multiple locations
            if "customerEmail" in first:
                return first.get("customerEmail", "")
            elif "buyerEmail" in first:
                return first.get("buyerEmail", "")

            ship_to = first.get("shipTo", {})
            if ship_to and "email" in ship_to:
                return ship_to.get("email", "")

            bill_to = first.get("billTo", {})
            if bill_to and "email" in bill_to:
                return bill_to.get("email", "")

        return None
    except Exception as e:
        print(f"  ShipStation error: {e}")
        return None

def fetch_email_from_shopify(shopify_api, tracking_number):
    """Fetch customer email from Shopify."""
    try:
        shopify_info = shopify_api.get_order_by_tracking(tracking_number)
        if shopify_info and shopify_info.get("order_id"):
            return shopify_info.get("customer_email", "")
        return None
    except Exception as e:
        print(f"  Shopify error: {e}")
        return None

def backfill_emails(limit=None, delay=0.5):
    """
    Backfill customer emails for scans missing email addresses.

    Args:
        limit: Maximum number of scans to process (None = all)
        delay: Delay in seconds between API calls (to avoid rate limiting)
    """
    print("=" * 60)
    print("BACKFILL CUSTOMER EMAILS")
    print("=" * 60)

    # Initialize APIs
    try:
        shopify_api = ShopifyAPI()
        print("✓ Shopify API initialized")
    except Exception as e:
        print(f"⚠️  Shopify API not available: {e}")
        shopify_api = None

    if SHIPSTATION_API_KEY and SHIPSTATION_API_SECRET:
        print("✓ ShipStation API initialized")
    else:
        print("⚠️  ShipStation API not configured")

    # Connect to database
    conn = get_mysql_connection()
    cursor = conn.cursor(dictionary=True)

    # Find scans without emails
    query = """
        SELECT id, tracking_number, customer_name, customer_email, order_number
        FROM scans
        WHERE (customer_email IS NULL OR customer_email = '')
          AND tracking_number IS NOT NULL
          AND tracking_number != ''
          AND order_number != 'Processing...'
          AND order_number != 'N/A'
        ORDER BY scan_date DESC
    """

    if limit:
        query += f" LIMIT {limit}"

    cursor.execute(query)
    scans = cursor.fetchall()

    total = len(scans)
    print(f"\nFound {total} scans without email addresses")

    if total == 0:
        print("✅ All scans already have emails!")
        cursor.close()
        conn.close()
        return

    print(f"Processing with {delay}s delay between API calls...\n")

    # Process each scan
    updated = 0
    skipped = 0

    for i, scan in enumerate(scans, 1):
        scan_id = scan['id']
        tracking_number = scan['tracking_number']

        print(f"[{i}/{total}] Processing {tracking_number}...")

        # Try ShipStation first
        email = fetch_email_from_shipstation(tracking_number)
        source = "ShipStation"

        # Try Shopify if ShipStation didn't return email
        if not email and shopify_api:
            email = fetch_email_from_shopify(shopify_api, tracking_number)
            source = "Shopify"

        if email:
            # Update database
            cursor.execute(
                "UPDATE scans SET customer_email = %s WHERE id = %s",
                (email, scan_id)
            )
            conn.commit()
            print(f"  ✅ Updated with email from {source}: {email}")
            updated += 1
        else:
            print(f"  ⚠️  No email found")
            skipped += 1

        # Rate limiting delay
        if i < total:  # Don't delay after the last one
            time.sleep(delay)

    cursor.close()
    conn.close()

    print("\n" + "=" * 60)
    print(f"BACKFILL COMPLETE")
    print(f"  Updated: {updated}")
    print(f"  Skipped: {skipped}")
    print(f"  Total processed: {total}")
    print("=" * 60)

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='Backfill customer emails for scans')
    parser.add_argument('--limit', type=int, help='Limit number of scans to process')
    parser.add_argument('--delay', type=float, default=0.5, help='Delay between API calls (default: 0.5s)')

    args = parser.parse_args()

    try:
        backfill_emails(limit=args.limit, delay=args.delay)
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
