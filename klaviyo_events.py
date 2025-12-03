# klaviyo_events.py
import os
import requests
from datetime import datetime
from typing import Optional, Dict, Any
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class KlaviyoEvents:
    def __init__(self):
        """
        Initialize Klaviyo Events API connection.
        Reads KLAVIYO_API_KEY from environment variables.
        """
        self.api_key = os.environ.get("KLAVIYO_API_KEY", "")
        self.enabled = os.environ.get("KLAVIYO_ENABLE", "true").lower() == "true"

        if not self.api_key:
            logger.warning("KLAVIYO_API_KEY not found in environment. Klaviyo events will be disabled.")
            self.enabled = False

        # Klaviyo API v2024-10-15 (latest revision)
        self.api_url = "https://a.klaviyo.com/api/events/"
        self.headers = {
            "Authorization": f"Klaviyo-API-Key {self.api_key}",
            "Content-Type": "application/json",
            "revision": "2024-10-15"
        }

    def track_parcel_scanned(
        self,
        tracking_number: str,
        order_number: str,
        customer_name: str,
        customer_email: str,
        carrier: str = "",
        order_id: Optional[str] = None,
        batch_id: Optional[int] = None,
        line_items: Optional[list] = None
    ) -> bool:
        """
        Send a 'Parcel Scanned' event to Klaviyo.

        Args:
            tracking_number: The tracking number of the parcel
            order_number: The order number from Shopify
            customer_name: Customer's full name
            customer_email: Customer's email (required for Klaviyo)
            carrier: Detected carrier (e.g., "UPS", "FedEx")
            order_id: Shopify order ID
            batch_id: Internal batch ID
            line_items: List of items in the order (from Shopify)

        Returns:
            bool: True if event was sent successfully, False otherwise
        """
        if not self.enabled:
            logger.debug("Klaviyo is disabled. Skipping event.")
            return False

        # Skip if no customer email (Klaviyo requires an identifier)
        if not customer_email or customer_email == "":
            logger.warning(f"No customer email for tracking {tracking_number}. Cannot send to Klaviyo.")
            return False

        try:
            # Build the event payload according to Klaviyo API spec
            event_payload = {
                "data": {
                    "type": "event",
                    "attributes": {
                        "profile": {
                            "data": {
                                "type": "profile",
                                "attributes": {
                                    "email": customer_email,
                                    "first_name": customer_name.split()[0] if customer_name and customer_name != "N/A" else "",
                                    "last_name": " ".join(customer_name.split()[1:]) if customer_name and len(customer_name.split()) > 1 else ""
                                }
                            }
                        },
                        "metric": {
                            "data": {
                                "type": "metric",
                                "attributes": {
                                    "name": "Parcel Scanned"
                                }
                            }
                        },
                        "properties": {
                            "tracking_number": tracking_number,
                            "order_number": order_number,
                            "carrier": carrier,
                            "order_id": order_id,
                            "batch_id": batch_id,
                            "scan_time": datetime.now().isoformat(),
                            "customer_name": customer_name,
                            "items": line_items if line_items else []
                        },
                        "time": datetime.now().isoformat(),
                        "value": 1
                    }
                }
            }

            # Send the request to Klaviyo
            response = requests.post(
                self.api_url,
                json=event_payload,
                headers=self.headers,
                timeout=5  # 5 second timeout to avoid blocking
            )

            if response.status_code in [200, 201, 202]:
                logger.info(f"✓ Klaviyo event sent for tracking: {tracking_number}")
                return True
            else:
                logger.error(f"Klaviyo API error {response.status_code}: {response.text}")
                return False

        except requests.exceptions.Timeout:
            logger.error(f"Klaviyo API timeout for tracking: {tracking_number}")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"Klaviyo API request failed: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error sending Klaviyo event: {str(e)}")
            return False

    def track_duplicate_scan(
        self,
        tracking_number: str,
        customer_email: str,
        original_batch_id: Optional[int] = None
    ) -> bool:
        """
        Send a 'Duplicate Scan Detected' event to Klaviyo.

        Args:
            tracking_number: The duplicate tracking number
            customer_email: Customer's email
            original_batch_id: The batch ID where it was originally scanned

        Returns:
            bool: True if event was sent successfully, False otherwise
        """
        if not self.enabled or not customer_email:
            return False

        try:
            event_payload = {
                "data": {
                    "type": "event",
                    "attributes": {
                        "profile": {
                            "data": {
                                "type": "profile",
                                "attributes": {
                                    "email": customer_email
                                }
                            }
                        },
                        "metric": {
                            "data": {
                                "type": "metric",
                                "attributes": {
                                    "name": "Duplicate Scan Detected"
                                }
                            }
                        },
                        "properties": {
                            "tracking_number": tracking_number,
                            "original_batch_id": original_batch_id,
                            "scan_time": datetime.now().isoformat()
                        },
                        "time": datetime.now().isoformat()
                    }
                }
            }

            response = requests.post(
                self.api_url,
                json=event_payload,
                headers=self.headers,
                timeout=5
            )

            if response.status_code in [200, 201, 202]:
                logger.info(f"✓ Klaviyo duplicate event sent for: {tracking_number}")
                return True
            else:
                logger.error(f"Klaviyo duplicate event failed {response.status_code}: {response.text}")
                return False

        except Exception as e:
            logger.error(f"Error sending duplicate scan event: {str(e)}")
            return False
