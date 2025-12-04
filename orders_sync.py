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

        Args:
            full_sync: If True, sync last `days_back` days. If False, incremental sync.
            days_back: Number of days to look back for full sync (default 90)

        Returns:
            Tuple of (orders_synced, status_message)
        """
        print(f"Starting orders sync (full_sync={full_sync}, days_back={days_back})...")
        self.update_sync_status('running')

        try:
            # Determine time range
            if full_sync:
                updated_at_min = datetime.now(timezone.utc) - timedelta(days=days_back)
                print(f"Full sync: fetching orders from last {days_back} days")
            else:
                last_sync = self.get_last_sync_time()
                if last_sync:
                    # Add timezone if missing
                    if last_sync.tzinfo is None:
                        last_sync = last_sync.replace(tzinfo=timezone.utc)
                    updated_at_min = last_sync
                    print(f"Incremental sync: fetching orders updated since {last_sync}")
                else:
                    # First ever sync - get last 30 days
                    updated_at_min = datetime.now(timezone.utc) - timedelta(days=30)
                    print(f"First sync: fetching orders from last 30 days")

            # Fetch orders from Shopify
            orders = self._fetch_orders_from_shopify(updated_at_min)
            print(f"Fetched {len(orders)} orders from Shopify")

            # Upsert each order
            synced_count = 0
            for order in orders:
                try:
                    self._upsert_order(order)
                    synced_count += 1
                except Exception as e:
                    print(f"Error upserting order {order.get('id')}: {e}")

            self.update_sync_status('completed', synced_count)
            message = f"Synced {synced_count} orders successfully"
            print(message)
            return synced_count, message

        except Exception as e:
            error_msg = str(e)
            print(f"Orders sync error: {error_msg}")
            self.update_sync_status('error', error=error_msg)
            return 0, f"Sync failed: {error_msg}"

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

            response, next_token = self.shopify._make_request("orders.json", params=request_params)

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

    def _upsert_order(self, shopify_order: Dict):
        """
        Insert or update an order from Shopify data.

        Args:
            shopify_order: Order data from Shopify API
        """
        conn = self.get_db_connection()
        cursor = conn.cursor()

        try:
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

            conn.commit()

            # Sync line items
            self._sync_line_items(order_id, shopify_order.get('line_items', []))

        finally:
            cursor.close()
            conn.close()

    def _sync_line_items(self, order_id: int, line_items: List[Dict]):
        """
        Sync line items for an order. Deletes existing and re-inserts.

        Args:
            order_id: Local order ID
            line_items: Line items from Shopify API
        """
        conn = self.get_db_connection()
        cursor = conn.cursor()

        try:
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

            conn.commit()

        finally:
            cursor.close()
            conn.close()

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
