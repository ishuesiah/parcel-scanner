# shopify_api.py

import requests
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Generator
import time
import re

class ShopifyAPI:
    def __init__(self, api_key: str, api_secret: str, access_token: str, shop_url: str):
        """Initialize Shopify API connection"""
        self.shop_url = shop_url
        self.access_token = access_token
        self.api_version = '2024-01'  # Using latest stable version
        self.session = requests.Session()
        self.session.headers.update({
            'X-Shopify-Access-Token': access_token,
            'Content-Type': 'application/json'
        })
        
        # Initialize cache for order lookups
        self._order_cache: Dict[str, Dict[str, Any]] = {}
        
    def _extract_next_page_token(self, headers) -> Optional[str]:
        """Extract next page token from Link header"""
        link_header = headers.get('Link', '')
        if not link_header:
            return None
            
        # Look for the 'next' link
        matches = re.findall(r'<([^>]+)>;\s*rel="next"', link_header)
        if not matches:
            return None
            
        # Extract the page_info parameter
        next_url = matches[0]
        page_info_match = re.search(r'page_info=([^&]+)', next_url)
        return page_info_match.group(1) if page_info_match else None

    def _make_request(self, endpoint: str, method: str = 'GET', params: dict = None) -> tuple[Optional[Dict], Optional[str]]:
        """Make authenticated request to Shopify API with rate limiting handling"""
        url = f"https://{self.shop_url}/admin/api/{self.api_version}/{endpoint}"
        max_retries = 3
        current_retry = 0
        
        while current_retry < max_retries:
            try:
                response = self.session.request(method, url, params=params)
                
                # Handle rate limits
                if response.status_code == 429:  # Too Many Requests
                    retry_after = int(response.headers.get('Retry-After', 2))
                    print(f"Rate limited. Waiting {retry_after} seconds...")
                    time.sleep(retry_after)
                    current_retry += 1
                    continue
                    
                response.raise_for_status()
                next_page_token = self._extract_next_page_token(response.headers)
                return response.json(), next_page_token
                
            except requests.exceptions.RequestException as e:
                print(f"Shopify API error (attempt {current_retry + 1}/{max_retries}): {e}")
                if current_retry < max_retries - 1:
                    time.sleep(1)  # Wait before retrying
                    current_retry += 1
                else:
                    return None, None
                    
        return None, None

    def _get_paginated_orders(self, initial_params: dict) -> Generator[dict, None, None]:
        """
        Generator function to handle Shopify API pagination for orders
        Uses cursor-based pagination with page_info
        """
        page = 1
        total_orders = 0
        params = initial_params.copy()
        
        while True:
            print(f"\nFetching page {page} of orders...")
            response, next_page_token = self._make_request('orders.json', params=params)
            
            if not response or not isinstance(response, dict):
                print(f"Invalid response received on page {page}")
                break
                
            orders = response.get('orders', [])
            if not orders:
                print(f"No orders found on page {page}")
                break
                
            # Process this page of orders
            current_page_count = len(orders)
            total_orders += current_page_count
            print(f"Retrieved {current_page_count} orders on page {page} (Total: {total_orders})")
            
            for order in orders:
                if isinstance(order, dict):
                    yield order
            
            # Check if there's another page
            if next_page_token:
                params = {'page_info': next_page_token}
                page += 1
            else:
                print(f"No more pages available. Total orders processed: {total_orders}")
                break

    def get_order_by_tracking(self, tracking_number: str) -> Dict[str, Any]:
        """Get order details by tracking number with proper pagination"""
        # Check cache first
        if tracking_number in self._order_cache:
            print(f"Found {tracking_number} in cache")
            return self._order_cache[tracking_number]
            
        try:
            print(f"Searching for order with tracking number: {tracking_number}")
            # Base query parameters
            params = {
                'fulfillment_status': 'shipped',
                'status': 'any',
                'limit': 250,  # Maximum allowed by API
                'fields': 'id,order_number,customer,fulfillments'
            }
            
            # Search orders from the last 60 days
            created_at_min = (datetime.now() - timedelta(days=60)).isoformat()
            print(f"Searching orders from {created_at_min} to now")
            params['created_at_min'] = created_at_min
            
            # Iterate through all pages using the generator
            orders_checked = 0
            for order in self._get_paginated_orders(params):
                orders_checked += 1
                
                if not isinstance(order, dict):
                    continue
                    
                fulfillments = order.get('fulfillments', [])
                if not isinstance(fulfillments, list):
                    continue
                    
                for fulfillment in fulfillments:
                    if not isinstance(fulfillment, dict):
                        continue
                        
                    if tracking_number == fulfillment.get('tracking_number', ''):
                        # Format response
                        customer = order.get('customer', {})
                        if not isinstance(customer, dict):
                            customer = {}
                            
                        order_data = {
                            'order_number': str(order.get('order_number', 'N/A')),
                            'customer_name': (
                                f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
                                or 'N/A'
                            ),
                            'order_id': str(order.get('id', ''))
                        }
                        
                        print(f"Found matching order: {order_data['order_number']}")
                        # Cache the result
                        self._order_cache[tracking_number] = order_data
                        return order_data
            
            print(f"No matching order found for {tracking_number} after checking {orders_checked} orders")
            # Return fallback if not found
            return {
                'order_number': 'N/A',
                'customer_name': 'No Order Found',
                'order_id': None
            }
            
        except Exception as e:
            print(f"Error fetching order by tracking number: {e}")
            import traceback
            traceback.print_exc()
            return {
                'order_number': 'N/A',
                'customer_name': f'Error: {str(e)}',
                'order_id': None
            }

    def clear_cache(self):
        """Clear the order cache"""
        self._order_cache = {}
