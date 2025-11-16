# shopify_api.py
import os
import requests
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Generator
import time
import re

class ShopifyAPI:
    def __init__(self):
        """
        Initialize Shopify API connection by reading these env vars:
          • SHOPIFY_API_KEY
          • SHOPIFY_API_SECRET
          • SHOPIFY_ACCESS_TOKEN
          • SHOP_URL
        """
        # Read straight from Kinsta's environment
        api_key      = os.environ.get("SHOPIFY_API_KEY", "")
        api_secret   = os.environ.get("SHOPIFY_API_SECRET", "")
        access_token = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
        shop_url     = os.environ.get("SHOP_URL", "")
        
        if not access_token or not shop_url:
            raise RuntimeError(
                "Missing SHOPIFY_ACCESS_TOKEN or SHOP_URL in environment."
            )

        self.shop_url    = shop_url
        self.access_token = access_token
        self.api_version = "2024-01"  # or whatever version you’re on
        self.session = requests.Session()
        self.session.headers.update({
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        })
        
        # Initialize cache for order lookups
        self._order_cache: Dict[str, Dict[str, Any]] = {}

    # … rest of your methods follow exactly as before …
    def _extract_next_page_token(self, headers) -> Optional[str]:
        link_header = headers.get("Link", "")
        if not link_header:
            return None
        matches = re.findall(r'<([^>]+)>;\s*rel="next"', link_header)
        if not matches:
            return None
        next_url = matches[0]
        m = re.search(r"page_info=([^&]+)", next_url)
        return m.group(1) if m else None

    def _make_request(self, endpoint: str, method: str = "GET", params: dict = None) -> tuple[Optional[Dict], Optional[str]]:
        url = f"https://{self.shop_url}/admin/api/{self.api_version}/{endpoint}"
        max_retries = 5  # Increased from 3 to 5
        retry = 0

        while retry < max_retries:
            try:
                resp = self.session.request(method, url, params=params, timeout=15)

                # Handle rate limiting (429)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 2))
                    print(f"Shopify rate limit hit, waiting {wait}s before retry {retry + 1}/{max_retries}")
                    time.sleep(wait)
                    retry += 1
                    continue

                # Handle 503 Service Unavailable with exponential backoff
                if resp.status_code == 503:
                    wait = min(2 ** retry, 16)  # Exponential backoff: 1s, 2s, 4s, 8s, 16s max
                    print(f"Shopify 503 error, waiting {wait}s before retry {retry + 1}/{max_retries}")
                    time.sleep(wait)
                    retry += 1
                    continue

                # Handle other 5xx errors with exponential backoff
                if 500 <= resp.status_code < 600:
                    wait = min(2 ** retry, 16)
                    print(f"Shopify {resp.status_code} error, waiting {wait}s before retry {retry + 1}/{max_retries}")
                    time.sleep(wait)
                    retry += 1
                    continue

                resp.raise_for_status()

                # Validate response is JSON before parsing
                content_type = resp.headers.get('Content-Type', '')
                if 'application/json' not in content_type:
                    print(f"Shopify API returned non-JSON response. Content-Type: {content_type}")
                    print(f"Response preview: {resp.text[:200]}")
                    return None, None

                try:
                    json_data = resp.json()
                    next_token = self._extract_next_page_token(resp.headers)
                    return json_data, next_token
                except ValueError as e:
                    print(f"Shopify API JSON parse error: {e}")
                    print(f"Response preview: {resp.text[:200]}")
                    return None, None

            except requests.exceptions.Timeout as e:
                wait = min(2 ** retry, 8)
                print(f"Shopify timeout error: {e}, waiting {wait}s before retry {retry + 1}/{max_retries}")
                if retry < max_retries - 1:
                    time.sleep(wait)
                    retry += 1
                    continue
                return None, None

            except requests.exceptions.RequestException as e:
                wait = min(2 ** retry, 8)
                print(f"Shopify request error: {e}, waiting {wait}s before retry {retry + 1}/{max_retries}")
                if retry < max_retries - 1:
                    time.sleep(wait)
                    retry += 1
                    continue
                return None, None

        print(f"Shopify API request failed after {max_retries} retries")
        return None, None

    def _get_paginated_orders(self, initial_params: dict) -> Generator[dict, None, None]:
        page = 1
        params = initial_params.copy()
        
        while True:
            response, next_token = self._make_request("orders.json", params=params)
            if not response or "orders" not in response:
                break
            orders = response["orders"]
            if not orders:
                break
            for order in orders:
                yield order
            if next_token:
                params = {"page_info": next_token, "limit": initial_params.get("limit", 250)}
                page += 1
            else:
                break

    def get_order_by_tracking(self, tracking_number: str) -> Dict[str, Any]:
        if tracking_number in self._order_cache:
            return self._order_cache[tracking_number]

        try:
            params = {
                "fulfillment_status": "shipped",
                "status": "any",
                "limit": 250,
                "fields": "id,order_number,customer,fulfillments"
            }
            created_at_min = (datetime.now() - timedelta(days=60)).isoformat()
            params["created_at_min"] = created_at_min

            for order in self._get_paginated_orders(params):
                fulfillments = order.get("fulfillments", [])
                for f in fulfillments:
                    if f.get("tracking_number") == tracking_number:
                        cust = order.get("customer", {}) or {}
                        order_data = {
                            "order_number": str(order.get("order_number", "N/A")),
                            "customer_name": (
                                f"{cust.get('first_name','')} {cust.get('last_name','')}".strip()
                                or "N/A"
                            ),
                            "customer_email": cust.get("email", ""),
                            "order_id": str(order.get("id", ""))
                        }
                        self._order_cache[tracking_number] = order_data
                        return order_data
            return {
                "order_number": "N/A",
                "customer_name": "No Order Found",
                "customer_email": "",
                "order_id": None
            }
        except Exception as e:
            return {
                "order_number": "N/A",
                "customer_name": f"Error: {e}",
                "customer_email": "",
                "order_id": None
            }

    def clear_cache(self):
        self._order_cache.clear()

    def get_order_details_for_verification(self, identifier: str) -> Optional[Dict[str, Any]]:
        """
        Get full order details including line items for verification.
        First tries to find by tracking number, then by order number.

        Args:
            identifier: Either a tracking number or order number

        Returns:
            Dict with order details and line items, or None if not found
        """
        try:
            # First, try to find by tracking number
            params = {
                "fulfillment_status": "any",
                "status": "any",
                "limit": 250,
                "fields": "id,order_number,name,customer,line_items,fulfillments"
            }
            created_at_min = (datetime.now() - timedelta(days=90)).isoformat()
            params["created_at_min"] = created_at_min

            # Search through orders with fulfillments for tracking number
            for order in self._get_paginated_orders(params):
                fulfillments = order.get("fulfillments", [])
                for f in fulfillments:
                    if f.get("tracking_number") == identifier:
                        return self._format_order_for_verification(order, f.get("tracking_number"))

            # If not found by tracking, try by order number
            # Order number could be numeric (#1234) or string (1234)
            clean_identifier = identifier.lstrip('#')

            params = {
                "name": clean_identifier,  # Shopify uses 'name' field for order number
                "status": "any",
                "limit": 1,
                "fields": "id,order_number,name,customer,line_items,fulfillments"
            }

            response, _ = self._make_request("orders.json", params=params)
            if response and "orders" in response and response["orders"]:
                order = response["orders"][0]
                # Get tracking number from first fulfillment if available
                tracking = None
                fulfillments = order.get("fulfillments", [])
                if fulfillments:
                    tracking = fulfillments[0].get("tracking_number")
                return self._format_order_for_verification(order, tracking)

            return None

        except Exception as e:
            print(f"Error fetching order details: {e}")
            return None

    def _format_order_for_verification(self, order: Dict, tracking_number: Optional[str] = None) -> Dict[str, Any]:
        """Format order data for verification page display."""
        cust = order.get("customer", {}) or {}
        line_items = order.get("line_items", [])

        # Format line items with cleaned up properties
        formatted_items = []
        for item in line_items:
            # Clean up variant title (remove "Default Title" if it's the only variant)
            variant_title = item.get("variant_title", "")
            if variant_title == "Default Title":
                variant_title = ""

            # Parse properties and format nicely
            properties = item.get("properties", [])
            formatted_properties = []
            if properties:
                for prop in properties:
                    name = prop.get("name", "")
                    value = prop.get("value", "")
                    if name and value and not name.startswith("_"):  # Skip hidden properties
                        formatted_properties.append(f"{name}: {value}")

            formatted_items.append({
                "id": item.get("id"),
                "name": item.get("name", ""),
                "sku": item.get("sku", "N/A"),
                "quantity": item.get("quantity", 1),
                "variant_title": variant_title,
                "properties": formatted_properties,
                "product_id": item.get("product_id"),
                "variant_id": item.get("variant_id")
            })

        return {
            "order_number": str(order.get("order_number", "N/A")),
            "order_name": order.get("name", ""),  # e.g., "#1234"
            "shopify_order_id": str(order.get("id", "")),
            "customer_name": (
                f"{cust.get('first_name','')} {cust.get('last_name','')}".strip()
                or "N/A"
            ),
            "customer_email": cust.get("email", ""),
            "tracking_number": tracking_number,
            "line_items": formatted_items,
            "total_items": sum(item.get("quantity", 1) for item in line_items)
        }
