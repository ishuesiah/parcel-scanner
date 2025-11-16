# klaviyo_api.py
import os
import requests
import time
import uuid
from datetime import datetime
from typing import Optional, Dict, Any

class KlaviyoAPI:
    def __init__(self):
        """
        Initialize Klaviyo API connection using private API key from environment.
        Set KLAVIYO_API_KEY in your environment variables.
        """
        self.api_key = os.environ.get("KLAVIYO_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("Missing KLAVIYO_API_KEY in environment variables")

        self.api_version = "2024-10-15"  # Latest Klaviyo API version
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Klaviyo-API-Key {self.api_key}",
            "revision": self.api_version,
            "Content-Type": "application/json"
        })

    def track_event(self, email: str, event_name: str, properties: Dict[str, Any]) -> bool:
        """
        Track an event for a customer in Klaviyo.

        Args:
            email: Customer email address
            event_name: Name of the event (e.g., "Order Shipped")
            properties: Dictionary of event properties (order_number, tracking_number, etc.)

        Returns:
            True if successful, False otherwise
        """
        if not email:
            print(f"⚠️ Skipping Klaviyo event - no email provided")
            return False

        url = "https://a.klaviyo.com/api/events/"

        # Generate unique ID to prevent event deduplication
        unique_id = str(uuid.uuid4())

        # Correct Klaviyo Events API format (2024-10-15)
        payload = {
            "data": {
                "type": "event",
                "attributes": {
                    "profile": {
                        "data": {
                            "type": "profile",
                            "attributes": {
                                "email": email
                            }
                        }
                    },
                    "metric": {
                        "data": {
                            "type": "metric",
                            "attributes": {
                                "name": event_name
                            }
                        }
                    },
                    "properties": properties,
                    "time": datetime.utcnow().isoformat() + "Z",
                    "unique_id": unique_id
                }
            }
        }

        print(f"📤 Sending Klaviyo event '{event_name}' for {email}")
        print(f"   Unique ID: {unique_id}")
        print(f"   Properties: {properties}")

        max_retries = 3
        for retry in range(max_retries):
            try:
                resp = self.session.post(url, json=payload, timeout=10)

                # Log response details
                print(f"📨 Klaviyo response: {resp.status_code}")

                # Handle rate limiting
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 2))
                    print(f"⏳ Klaviyo rate limit hit, waiting {wait}s before retry {retry + 1}/{max_retries}")
                    time.sleep(wait)
                    continue

                # Handle 5xx errors with exponential backoff
                if 500 <= resp.status_code < 600:
                    wait = min(2 ** retry, 8)
                    print(f"❌ Klaviyo {resp.status_code} error, retry {retry + 1}/{max_retries} after {wait}s")
                    print(f"   Response: {resp.text[:500]}")
                    if retry < max_retries - 1:
                        time.sleep(wait)
                        continue
                    else:
                        print(f"❌ Klaviyo event failed for {email}: {resp.status_code}")
                        return False

                # Handle 4xx errors (bad request, etc.)
                if 400 <= resp.status_code < 500:
                    print(f"❌ Klaviyo client error {resp.status_code}: {resp.text[:500]}")
                    return False

                resp.raise_for_status()
                print(f"✅ Klaviyo event '{event_name}' tracked successfully for {email}")
                return True

            except requests.exceptions.Timeout as e:
                print(f"⏱️ Klaviyo timeout for {email}, retry {retry + 1}/{max_retries}: {e}")
                if retry < max_retries - 1:
                    time.sleep(1)
                    continue
                return False

            except requests.exceptions.RequestException as e:
                print(f"❌ Klaviyo request error for {email}: {e}")
                if retry < max_retries - 1:
                    time.sleep(1)
                    continue
                return False

        print(f"❌ Klaviyo event failed for {email} after {max_retries} retries")
        return False

    def notify_order_shipped(self, email: str, order_number: str, tracking_number: str, carrier: str) -> bool:
        """
        Convenience method to send "Order Shipped" event.

        Args:
            email: Customer email
            order_number: Order number
            tracking_number: Tracking number
            carrier: Carrier name (UPS, Canada Post, etc.)

        Returns:
            True if successful, False otherwise
        """
        properties = {
            "order_number": order_number,
            "tracking_number": tracking_number,
            "carrier": carrier
        }

        return self.track_event(email, "Order Shipped", properties)
