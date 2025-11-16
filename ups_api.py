# ups_api.py
"""
UPS Tracking API integration for shipment tracking.
Uses OAuth 2.0 client credentials flow for authentication.
"""

import os
import requests
import time
import base64
from typing import Optional, Dict, Any


class UPSAPI:
    def __init__(self):
        """
        Initialize UPS API client with OAuth 2.0 credentials.
        Reads UPS_CLIENT_ID and UPS_CLIENT_SECRET from environment.
        """
        self.client_id = os.environ.get("UPS_CLIENT_ID", "")
        self.client_secret = os.environ.get("UPS_CLIENT_SECRET", "")

        if not self.client_id or not self.client_secret:
            print("⚠️ WARNING: UPS_CLIENT_ID or UPS_CLIENT_SECRET not set in environment")
            self.enabled = False
        else:
            self.enabled = True

        self.access_token = None
        self.token_expires_at = 0

        # UPS API endpoints
        self.oauth_url = "https://onlinetools.ups.com/security/v1/oauth/token"
        self.tracking_url_base = "https://onlinetools.ups.com/api/track/v1/details"

    def get_access_token(self) -> Optional[str]:
        """
        Get OAuth 2.0 access token using client credentials flow.
        Caches token until expiration.

        Returns:
            Access token string, or None if authentication fails
        """
        if not self.enabled:
            return None

        # Check if we have a valid cached token
        if self.access_token and time.time() < self.token_expires_at:
            return self.access_token

        try:
            # Create Basic Auth header: base64(client_id:client_secret)
            credentials = f"{self.client_id}:{self.client_secret}"
            encoded_credentials = base64.b64encode(credentials.encode()).decode()

            headers = {
                "Authorization": f"Basic {encoded_credentials}",
                "Content-Type": "application/x-www-form-urlencoded"
            }

            data = {
                "grant_type": "client_credentials"
            }

            print(f"🔑 Requesting UPS OAuth token...")
            response = requests.post(
                self.oauth_url,
                headers=headers,
                data=data,
                timeout=10
            )

            if response.status_code == 200:
                token_data = response.json()
                self.access_token = token_data.get("access_token")
                expires_in = token_data.get("expires_in", 3600)  # Default 1 hour
                self.token_expires_at = time.time() + expires_in - 60  # Refresh 1 min early
                print(f"✅ UPS OAuth token obtained (expires in {expires_in}s)")
                return self.access_token
            else:
                print(f"❌ UPS OAuth failed: {response.status_code} - {response.text[:200]}")
                return None

        except Exception as e:
            print(f"❌ UPS OAuth error: {e}")
            return None

    def get_tracking_status(self, tracking_number: str) -> Dict[str, Any]:
        """
        Get tracking status for a UPS tracking number.

        Args:
            tracking_number: UPS tracking number

        Returns:
            Dict with status information:
            {
                "status": "label_created" | "in_transit" | "delivered" | "exception" | "unknown" | "error",
                "status_description": str,
                "last_activity": str (datetime or description),
                "location": str,
                "delivered_date": str or None,
                "raw_status_code": str,
                "raw_status_desc": str,
                "error": str (if error occurred)
            }
        """
        if not self.enabled:
            return {
                "status": "error",
                "status_description": "UPS API not configured",
                "error": "UPS_CLIENT_ID or UPS_CLIENT_SECRET not set"
            }

        token = self.get_access_token()
        if not token:
            return {
                "status": "error",
                "status_description": "Authentication failed",
                "error": "Could not obtain UPS access token"
            }

        try:
            url = f"{self.tracking_url_base}/{tracking_number}"

            headers = {
                "Authorization": f"Bearer {token}",
                "transId": str(int(time.time())),
                "transactionSrc": "parcel_scanner"
            }

            print(f"📦 Querying UPS tracking for {tracking_number}...")
            response = requests.get(url, headers=headers, timeout=10)

            if response.status_code == 200:
                data = response.json()
                return self._parse_tracking_response(data)
            elif response.status_code == 404:
                return {
                    "status": "unknown",
                    "status_description": "Tracking number not found",
                    "error": "No tracking information available"
                }
            else:
                print(f"❌ UPS Tracking error: {response.status_code} - {response.text[:200]}")
                return {
                    "status": "error",
                    "status_description": f"API error {response.status_code}",
                    "error": response.text[:200]
                }

        except Exception as e:
            print(f"❌ UPS Tracking exception: {e}")
            return {
                "status": "error",
                "status_description": "Request failed",
                "error": str(e)
            }

    def _parse_tracking_response(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse UPS tracking API response into simplified status.

        Args:
            data: Raw UPS API response JSON

        Returns:
            Parsed status dict
        """
        try:
            track_response = data.get("trackResponse", {})
            shipment = track_response.get("shipment", [{}])[0] if track_response.get("shipment") else {}
            package = shipment.get("package", [{}])[0] if shipment.get("package") else {}

            # Get current status
            current_status = package.get("currentStatus", {})
            status_code = current_status.get("code", "")
            status_desc = current_status.get("description", "Unknown")

            # Get activity history
            activity = package.get("activity", [])
            last_activity = activity[0] if activity else {}
            last_activity_status = last_activity.get("status", {})
            last_activity_desc = last_activity_status.get("description", "")
            last_activity_type = last_activity_status.get("type", "")

            location = ""
            if last_activity.get("location"):
                loc = last_activity["location"].get("address", {})
                city = loc.get("city", "")
                state = loc.get("stateProvince", "")
                country = loc.get("countryCode", "")
                location = f"{city}, {state}, {country}".strip(", ")

            # Determine simplified status
            status = "unknown"
            if status_code in ["011", "012"]:  # Delivered
                status = "delivered"
            elif status_code in ["M", "MP", "P"]:  # In transit / Moving
                status = "in_transit"
            elif status_code in ["I"]:  # Label created / Pre-shipment
                status = "label_created"
            elif status_code in ["X"]:  # Exception / Delay
                status = "exception"
            elif "transit" in status_desc.lower() or "transit" in last_activity_desc.lower():
                status = "in_transit"
            elif "delivered" in status_desc.lower():
                status = "delivered"
            elif "exception" in status_desc.lower() or "delay" in status_desc.lower():
                status = "exception"

            # Get delivery date if delivered
            delivered_date = None
            if status == "delivered" and last_activity.get("date"):
                delivered_date = last_activity.get("date")

            return {
                "status": status,
                "status_description": status_desc or last_activity_desc,
                "last_activity": last_activity.get("date", "") or last_activity.get("time", ""),
                "location": location,
                "delivered_date": delivered_date,
                "raw_status_code": status_code,
                "raw_status_desc": status_desc
            }

        except Exception as e:
            print(f"❌ Error parsing UPS response: {e}")
            return {
                "status": "error",
                "status_description": "Failed to parse tracking data",
                "error": str(e)
            }
