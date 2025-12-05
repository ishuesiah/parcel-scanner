# orders_sync.py
"""
Shopify Orders Sync Module for Hemlock & Oak Parcel Scanner

Syncs orders from Shopify API to local PostgreSQL database.
- Initial sync: Last 90 days of orders
- Incremental sync: Every 5 minutes, fetches orders updated since last sync
"""

import os
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

# Timezone support for Vancouver/PST
try:
    from zoneinfo import ZoneInfo
    PST = ZoneInfo("America/Vancouver")
except ImportError:
    PST = timezone(timedelta(hours=-8))


def now_pst():
    """Get current time in Vancouver/PST timezone."""
    return datetime.now(PST)


def init_orders_tables(get_db_connection):
    """
    Initialize the orders tables if they don't exist.
    Called on app startup to ensure tables are ready.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Create orders table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                shopify_order_id TEXT UNIQUE NOT NULL,
                order_number TEXT NOT NULL,
                customer_name TEXT,
                customer_email TEXT,
                customer_phone TEXT,
                shipping_address TEXT,
                billing_address TEXT,
                note TEXT,
                note_attributes TEXT,
                total_price REAL,
                subtotal_price REAL,
                total_tax REAL,
                total_shipping REAL,
                currency TEXT DEFAULT 'CAD',
                financial_status TEXT,
                fulfillment_status TEXT,
                tracking_number TEXT,
                scanned_status INTEGER DEFAULT 0,
                scanned_at TIMESTAMP,
                shopify_created_at TIMESTAMP,
                shopify_updated_at TIMESTAMP,
                synced_at TIMESTAMP,
                cancelled_at TIMESTAMP,
                cancel_reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create indexes for orders
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_order_number ON orders(order_number)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_shopify_order_id ON orders(shopify_order_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_customer_email ON orders(customer_email)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_tracking ON orders(tracking_number)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_fulfillment ON orders(fulfillment_status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_scanned ON orders(scanned_status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_shopify_updated ON orders(shopify_updated_at)")

        # Create order_line_items table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS order_line_items (
                id SERIAL PRIMARY KEY,
                order_id INTEGER NOT NULL,
                shopify_line_item_id TEXT,
                sku TEXT,
                product_id TEXT,
                variant_id TEXT,
                product_title TEXT,
                variant_title TEXT,
                quantity INTEGER DEFAULT 1,
                price REAL,
                total_discount REAL DEFAULT 0,
                fulfillable_quantity INTEGER,
                fulfillment_status TEXT,
                requires_shipping INTEGER DEFAULT 1,
                picked INTEGER DEFAULT 0,
                picked_at TIMESTAMP,
                FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_line_items_order ON order_line_items(order_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_line_items_sku ON order_line_items(sku)")

        # Create order_line_item_options table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS order_line_item_options (
                id SERIAL PRIMARY KEY,
                line_item_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                value TEXT,
                FOREIGN KEY (line_item_id) REFERENCES order_line_items(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_options_line_item ON order_line_item_options(line_item_id)")

        # Create order_sync_status table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS order_sync_status (
                id SERIAL PRIMARY KEY,
                sync_type TEXT NOT NULL UNIQUE,
                last_sync_at TIMESTAMP,
                last_sync_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'idle',
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Insert initial sync status record if it doesn't exist
        cursor.execute("""
            INSERT INTO order_sync_status (sync_type, status)
            VALUES ('shopify_orders', 'idle')
            ON CONFLICT (sync_type) DO NOTHING
        """)

        # Reset any stuck "running" status (from interrupted syncs)
        cursor.execute("""
            UPDATE order_sync_status
            SET status = 'idle',
                error_message = 'Reset from interrupted sync on server restart',
                updated_at = CURRENT_TIMESTAMP
            WHERE sync_type = 'shopify_orders' AND status = 'running'
        """)

        # Update cancelled_orders table if it exists but lacks columns
        # First check if table exists
        cursor.execute("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'cancelled_orders'
            )
        """)
        table_exists = cursor.fetchone()

        if not table_exists or not table_exists.get('exists', False):
            # Create cancelled_orders table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS cancelled_orders (
                    id SERIAL PRIMARY KEY,
                    order_id INTEGER,
                    shopify_order_id TEXT,
                    order_number TEXT NOT NULL,
                    tracking_number TEXT,
                    customer_name TEXT,
                    customer_email TEXT,
                    reason TEXT NOT NULL,
                    reason_notes TEXT,
                    cancelled_by TEXT,
                    refund_amount REAL,
                    refund_issued INTEGER DEFAULT 0,
                    shopify_refund_id TEXT,
                    refunded_at TIMESTAMP,
                    shipstation_voided INTEGER DEFAULT 0,
                    shipstation_shipment_id TEXT,
                    shipstation_void_response TEXT,
                    cancelled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_cancelled_order_number ON cancelled_orders(order_number)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_cancelled_shopify_id ON cancelled_orders(shopify_order_id)")

        conn.commit()
        cursor.close()
        conn.close()
        print("✓ Orders tables initialized")

    except Exception as e:
        print(f"❌ Error initializing orders tables: {e}")


class OrdersSync:
    """
    Handles syncing orders from Shopify to local database.
    """

    def __init__(self, shopify_api, get_db_connection):
        """
        Initialize the orders sync service.

        Args:
            shopify_api: ShopifyAPI instance
            get_db_connection: Function to get database connection
        """
        self.shopify = shopify_api
        self.get_db_connection = get_db_connection

    def get_last_sync_time(self) -> Optional[datetime]:
        """Get the last successful sync time from database."""
        try:
            conn = self.get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT last_sync_at FROM order_sync_status
                WHERE sync_type = 'shopify_orders'
            """)
            row = cursor.fetchone()
            cursor.close()
            conn.close()

            if row and row.get('last_sync_at'):
                return row['last_sync_at']
            return None
        except Exception as e:
            print(f"Error getting last sync time: {e}")
            return None

    def update_sync_status(self, status: str, count: int = 0, error: str = None):
        """Update the sync status in database."""
        try:
            conn = self.get_db_connection()
            cursor = conn.cursor()

            if status == 'completed':
                cursor.execute("""
                    UPDATE order_sync_status
                    SET status = 'idle',
                        last_sync_at = CURRENT_TIMESTAMP,
                        last_sync_count = %s,
                        error_message = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE sync_type = 'shopify_orders'
                """, (count,))
            elif status == 'running':
                cursor.execute("""
                    UPDATE order_sync_status
                    SET status = 'running',
                        error_message = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE sync_type = 'shopify_orders'
                """)
            elif status == 'error':
                cursor.execute("""
                    UPDATE order_sync_status
                    SET status = 'error',
                        error_message = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE sync_type = 'shopify_orders'
                """, (error,))

            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"Error updating sync status: {e}")

    def sync_orders(self, full_sync: bool = False, days_back: int = 90) -> Tuple[int, str]:
        """
        Sync orders from Shopify API to local database.

        Uses streaming approach: processes each page of orders immediately
        to avoid memory exhaustion from accumulating all orders.

        Args:
            full_sync: If True, sync last `days_back` days. If False, incremental sync.
            days_back: Number of days to look back for full sync (default 90)

        Returns:
            Tuple of (orders_synced, status_message)
        """
        print(f"Starting orders sync (full_sync={full_sync}, days_back={days_back})...")
        self.update_sync_status('running')

        conn = None
        try:
            # Determine time range
            if full_sync:
                updated_at_min = datetime.now(timezone.utc) - timedelta(days=days_back)
                print(f"Full sync: fetching orders from last {days_back} days")
            else:
                last_sync = self.get_last_sync_time()
                if last_sync:
                    if last_sync.tzinfo is None:
                        last_sync = last_sync.replace(tzinfo=timezone.utc)
                    updated_at_min = last_sync
                    print(f"Incremental sync: fetching orders updated since {last_sync}")
                else:
                    updated_at_min = datetime.now(timezone.utc) - timedelta(days=30)
                    print(f"First sync: fetching orders from last 30 days")

            # Get a single connection for all upserts
            conn = self.get_db_connection()
            conn.autocommit = False

            # Stream orders page-by-page to avoid memory exhaustion
            synced_count = 0
            error_count = 0
            page = 1
            page_info = None

            # Build initial params
            params = {
                "status": "any",
                "updated_at_min": updated_at_min.isoformat(),
                "limit": 250,
                "fields": "id,name,email,phone,total_price,subtotal_price,total_tax,"
                          "shipping_lines,financial_status,fulfillment_status,fulfillments,"
                          "line_items,shipping_address,billing_address,note,note_attributes,"
                          "created_at,updated_at,cancelled_at,cancel_reason,customer"
            }

            while True:
                print(f"Fetching orders page {page}...")
                time.sleep(0.5)  # Rate limiting

                if page_info:
                    request_params = {"page_info": page_info, "limit": 250}
                else:
                    request_params = params

                # Fetch page with retries
                response = None
                next_token = None
                for retry in range(3):
                    try:
                        response, next_token = self.shopify._make_request("orders.json", params=request_params)
                        if response:
                            break
                    except Exception as e:
                        print(f"Page {page} fetch error (attempt {retry + 1}/3): {e}")
                        if retry < 2:
                            time.sleep(min(2 ** retry, 8))

                if not response or "orders" not in response:
                    print(f"No more orders on page {page}")
                    break

                batch = response.get("orders", [])
                if not batch:
                    print(f"Page {page}: no orders found")
                    break

                print(f"Page {page}: fetched {len(batch)} orders, processing...")

                # Process this page immediately (don't accumulate)
                for order in batch:
                    try:
                        self._upsert_order_with_conn(conn, order)
                        synced_count += 1
                    except Exception as e:
                        error_count += 1
                        print(f"Error upserting order {order.get('id')}: {e}")
                        try:
                            conn.rollback()
                        except:
                            pass

                # Commit after each page
                conn.commit()
                print(f"Page {page}: committed {len(batch)} orders (total synced: {synced_count})")

                # Clear batch from memory
                del batch

                if next_token:
                    page_info = next_token
                    page += 1
                else:
                    break

            if error_count > 0:
                print(f"Warning: {error_count} orders failed to sync")

            self.update_sync_status('completed', synced_count)
            message = f"Synced {synced_count} orders successfully"
            print(message)
            return synced_count, message

        except Exception as e:
            error_msg = str(e)
            print(f"Orders sync error: {error_msg}")
            import traceback
            traceback.print_exc()
            self.update_sync_status('error', error=error_msg)
            return 0, f"Sync failed: {error_msg}"
        finally:
            # Always close the connection
            if conn:
                try:
                    conn.close()
                except:
                    pass

    def _fetch_orders_from_shopify(self, updated_at_min: datetime) -> List[Dict]:
        """
        Fetch orders from Shopify API with pagination.

        Args:
            updated_at_min: Minimum updated_at timestamp

        Returns:
            List of order dictionaries
        """
        orders = []
        page_info = None
        page = 1

        # Build initial params
        params = {
            "status": "any",
            "updated_at_min": updated_at_min.isoformat(),
            "limit": 250,
            "fields": "id,name,email,phone,total_price,subtotal_price,total_tax,"
                      "shipping_lines,financial_status,fulfillment_status,fulfillments,"
                      "line_items,shipping_address,billing_address,note,note_attributes,"
                      "created_at,updated_at,cancelled_at,cancel_reason,customer"
        }

        while True:
            print(f"Fetching orders page {page}...")

            # Respect rate limits
            time.sleep(0.5)

            if page_info:
                # Use cursor-based pagination
                request_params = {"page_info": page_info, "limit": 250}
            else:
                request_params = params

            # Retry logic for individual page fetches
            max_retries = 3
            response = None
            next_token = None

            for retry in range(max_retries):
                try:
                    response, next_token = self.shopify._make_request("orders.json", params=request_params)
                    if response:
                        break
                except Exception as e:
                    print(f"Page {page} fetch error (attempt {retry + 1}/{max_retries}): {e}")
                    if retry < max_retries - 1:
                        wait = min(2 ** retry, 8)
                        print(f"Retrying in {wait}s...")
                        time.sleep(wait)
                    else:
                        print(f"Failed to fetch page {page} after {max_retries} attempts, continuing with partial results")

            if not response or "orders" not in response:
                print(f"No more orders or error on page {page}")
                break

            batch = response.get("orders", [])
            if not batch:
                break

            orders.extend(batch)
            print(f"Page {page}: fetched {len(batch)} orders (total: {len(orders)})")

            if next_token:
                page_info = next_token
                page += 1
            else:
                break

        return orders

    def _upsert_order_with_conn(self, conn, shopify_order: Dict):
        """
        Insert or update an order from Shopify data using an existing connection.

        Args:
            conn: Database connection to use
            shopify_order: Order data from Shopify API
        """
        cursor = conn.cursor()

        try:
            # Debug: log first order to verify data structure
            order_name = shopify_order.get('name', 'Unknown')
            if not hasattr(self, '_logged_first_order'):
                print(f"First order being processed: {order_name} (ID: {shopify_order.get('id')})")
                self._logged_first_order = True
            shopify_order_id = str(shopify_order['id'])

            # Check if order exists
            cursor.execute(
                "SELECT id FROM orders WHERE shopify_order_id = %s",
                (shopify_order_id,)
            )
            existing = cursor.fetchone()

            # Extract tracking number from fulfillments if available
            tracking_number = None
            if shopify_order.get('fulfillments'):
                for fulfillment in shopify_order['fulfillments']:
                    if fulfillment.get('tracking_number'):
                        tracking_number = fulfillment['tracking_number']
                        break

            # Extract customer name from multiple sources
            customer_name = self._get_customer_name(shopify_order)
            customer_email = shopify_order.get('email') or ''
            customer_phone = shopify_order.get('phone') or ''

            # Get customer info from customer object if email/phone missing
            customer = shopify_order.get('customer') or {}
            if not customer_email and customer.get('email'):
                customer_email = customer['email']
            if not customer_phone and customer.get('phone'):
                customer_phone = customer['phone']

            # Calculate total shipping
            shipping_lines = shopify_order.get('shipping_lines', [])
            total_shipping = sum(float(s.get('price', 0)) for s in shipping_lines)

            # Build order data
            order_number = shopify_order.get('name', '').replace('#', '').strip()
            if not order_number:
                order_number = str(shopify_order.get('order_number', ''))

            now = datetime.now(timezone.utc).isoformat()

            if existing:
                # UPDATE existing order
                order_id = existing['id']
                cursor.execute("""
                    UPDATE orders SET
                        order_number = %s,
                        customer_name = %s,
                        customer_email = %s,
                        customer_phone = %s,
                        shipping_address = %s,
                        billing_address = %s,
                        note = %s,
                        note_attributes = %s,
                        total_price = %s,
                        subtotal_price = %s,
                        total_tax = %s,
                        total_shipping = %s,
                        currency = %s,
                        financial_status = %s,
                        fulfillment_status = %s,
                        tracking_number = %s,
                        shopify_created_at = %s,
                        shopify_updated_at = %s,
                        cancelled_at = %s,
                        cancel_reason = %s,
                        synced_at = %s,
                        updated_at = %s
                    WHERE id = %s
                """, (
                    order_number,
                    customer_name,
                    customer_email,
                    customer_phone,
                    json.dumps(shopify_order.get('shipping_address')),
                    json.dumps(shopify_order.get('billing_address')),
                    shopify_order.get('note'),
                    json.dumps(shopify_order.get('note_attributes', [])),
                    float(shopify_order.get('total_price', 0)),
                    float(shopify_order.get('subtotal_price', 0)),
                    float(shopify_order.get('total_tax', 0)),
                    total_shipping,
                    shopify_order.get('currency', 'CAD'),
                    shopify_order.get('financial_status'),
                    shopify_order.get('fulfillment_status'),
                    tracking_number,
                    shopify_order.get('created_at'),
                    shopify_order.get('updated_at'),
                    shopify_order.get('cancelled_at'),
                    shopify_order.get('cancel_reason'),
                    now,
                    now,
                    order_id
                ))
            else:
                # INSERT new order
                cursor.execute("""
                    INSERT INTO orders (
                        shopify_order_id, order_number, customer_name, customer_email,
                        customer_phone, shipping_address, billing_address, note,
                        note_attributes, total_price, subtotal_price, total_tax,
                        total_shipping, currency, financial_status, fulfillment_status,
                        tracking_number, shopify_created_at, shopify_updated_at,
                        cancelled_at, cancel_reason, synced_at, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    RETURNING id
                """, (
                    shopify_order_id,
                    order_number,
                    customer_name,
                    customer_email,
                    customer_phone,
                    json.dumps(shopify_order.get('shipping_address')),
                    json.dumps(shopify_order.get('billing_address')),
                    shopify_order.get('note'),
                    json.dumps(shopify_order.get('note_attributes', [])),
                    float(shopify_order.get('total_price', 0)),
                    float(shopify_order.get('subtotal_price', 0)),
                    float(shopify_order.get('total_tax', 0)),
                    total_shipping,
                    shopify_order.get('currency', 'CAD'),
                    shopify_order.get('financial_status'),
                    shopify_order.get('fulfillment_status'),
                    tracking_number,
                    shopify_order.get('created_at'),
                    shopify_order.get('updated_at'),
                    shopify_order.get('cancelled_at'),
                    shopify_order.get('cancel_reason'),
                    now,
                    now,
                    now
                ))
                result = cursor.fetchone()
                order_id = result['id']

            # Sync line items using the same connection
            self._sync_line_items_with_conn(conn, cursor, order_id, shopify_order.get('line_items', []))

        finally:
            cursor.close()

    def _sync_line_items_with_conn(self, conn, cursor, order_id: int, line_items: List[Dict]):
        """
        Sync line items for an order using an existing connection/cursor.

        Args:
            conn: Database connection
            cursor: Database cursor
            order_id: Local order ID
            line_items: Line items from Shopify API
        """
        # Delete existing line items (CASCADE will delete options too)
        cursor.execute("DELETE FROM order_line_items WHERE order_id = %s", (order_id,))

        for item in line_items:
            # Calculate total discount
            discount_allocations = item.get('discount_allocations', [])
            total_discount = sum(float(d.get('amount', 0)) for d in discount_allocations)

            cursor.execute("""
                INSERT INTO order_line_items (
                    order_id, shopify_line_item_id, sku, product_id, variant_id,
                    product_title, variant_title, quantity, price, total_discount,
                    fulfillable_quantity, fulfillment_status, requires_shipping
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                order_id,
                str(item.get('id', '')),
                item.get('sku'),
                str(item.get('product_id', '')),
                str(item.get('variant_id', '')),
                item.get('title'),
                item.get('variant_title'),
                item.get('quantity', 1),
                float(item.get('price', 0)),
                total_discount,
                item.get('fulfillable_quantity', 0),
                item.get('fulfillment_status'),
                1 if item.get('requires_shipping', True) else 0
            ))
            result = cursor.fetchone()
            line_item_id = result['id']

            # Sync line item options/properties (TEPO customizations)
            properties = item.get('properties', [])
            for prop in properties:
                prop_name = prop.get('name', '')
                prop_value = prop.get('value', '')

                # Skip internal properties that start with underscore
                if prop_name and prop_value and not prop_name.startswith('_'):
                    cursor.execute("""
                        INSERT INTO order_line_item_options (line_item_id, name, value)
                        VALUES (%s, %s, %s)
                    """, (line_item_id, prop_name, str(prop_value)))

    def _get_customer_name(self, order: Dict) -> str:
        """Extract customer name from order, trying multiple sources."""

        # Try shipping address first
        if order.get('shipping_address'):
            addr = order['shipping_address']
            name = addr.get('name') or f"{addr.get('first_name', '')} {addr.get('last_name', '')}".strip()
            if name:
                return name

        # Try billing address
        if order.get('billing_address'):
            addr = order['billing_address']
            name = addr.get('name') or f"{addr.get('first_name', '')} {addr.get('last_name', '')}".strip()
            if name:
                return name

        # Try customer object
        if order.get('customer'):
            cust = order['customer']
            name = f"{cust.get('first_name', '')} {cust.get('last_name', '')}".strip()
            if name:
                return name

        return "Unknown Customer"

    def get_sync_status(self) -> Dict:
        """Get current sync status."""
        try:
            conn = self.get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT sync_type, last_sync_at, last_sync_count, status, error_message, updated_at
                FROM order_sync_status
                WHERE sync_type = 'shopify_orders'
            """)
            row = cursor.fetchone()
            cursor.close()
            conn.close()

            if row:
                return dict(row)
            return {"sync_type": "shopify_orders", "status": "unknown"}
        except Exception as e:
            return {"sync_type": "shopify_orders", "status": "error", "error_message": str(e)}


def update_order_scanned_status(get_db_connection, tracking_number: str):
    """
    Mark an order as scanned based on tracking number.
    Call this when a successful scan happens.

    Args:
        get_db_connection: Function to get database connection
        tracking_number: The tracking number that was scanned
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        now = datetime.now(timezone.utc).isoformat()
        cursor.execute("""
            UPDATE orders
            SET scanned_status = 1,
                scanned_at = %s,
                updated_at = %s
            WHERE tracking_number = %s
        """, (now, now, tracking_number))

        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Error updating order scanned status: {e}")
