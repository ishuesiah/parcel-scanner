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
        Initialize Shopify API connection by reading these four env vars:
          • SHOPIFY_API_KEY
          • SHOPIFY_API_SECRET
          • SHOPIFY_ACCESS_TOKEN
          • SHOPIFY_SHOP_URL
        """
        # Read straight from Kinsta’s environment
        api_key      = os.environ.get("SHOPIFY_API_KEY", "")
        api_secret   = os.environ.get("SHOPIFY_API_SECRET", "")
        access_token = os.environ.get("ACCESS_TOKEN", "")
        shop_url     = os.environ.get("SHOP_URL", "")
        
        if not access_token or not shop_url:
            raise RuntimeError(
                "Missing SHOPIFY_ACCESS_TOKEN or SHOPIFY_SHOP_URL in environment."
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
        max_retries = 3
        retry = 0
        
        while retry < max_retries:
            try:
                resp = self.session.request(method, url, params=params)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 2))
                    time.sleep(wait)
                    retry += 1
                    continue
                resp.raise_for_status()
                next_token = self._extract_next_page_token(resp.headers)
                return resp.json(), next_token
            except requests.exceptions.RequestException as e:
                if retry < max_retries - 1:
                    time.sleep(1)
                    retry += 1
                    continue
                return None, None
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
                            "order_id": str(order.get("id", ""))
                        }
                        self._order_cache[tracking_number] = order_data
                        return order_data
            return {
                "order_number": "N/A",
                "customer_name": "No Order Found",
                "order_id": None
            }
        except Exception as e:
            return {
                "order_number": "N/A",
                "customer_name": f"Error: {e}",
                "order_id": None
            }

    def clear_cache(self):
        self._order_cache.clear()
