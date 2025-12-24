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
                # Convert to int in case API returns string
                try:
                    expires_in = int(expires_in)
                except (ValueError, TypeError):
                    expires_in = 3600
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
            status_type = current_status.get("type", "")

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

            # Extract estimated delivery date from multiple possible locations
            estimated_delivery = ""

            # Try deliveryDate first
            delivery_date = package.get("deliveryDate", [])
            if delivery_date:
                if isinstance(delivery_date, list) and len(delivery_date) > 0:
                    dd = delivery_date[0]
                    date_str = dd.get("date", "")
                    if date_str:
                        # Format: YYYYMMDD -> Month DD
                        try:
                            from datetime import datetime
                            dt = datetime.strptime(date_str, "%Y%m%d")
                            estimated_delivery = dt.strftime("%B %d")
                        except:
                            estimated_delivery = date_str
                elif isinstance(delivery_date, dict):
                    date_str = delivery_date.get("date", "")
                    if date_str:
                        try:
                            from datetime import datetime
                            dt = datetime.strptime(date_str, "%Y%m%d")
                            estimated_delivery = dt.strftime("%B %d")
                        except:
                            estimated_delivery = date_str

            # Try deliveryTime for more precision
            delivery_time = package.get("deliveryTime", {})
            if delivery_time:
                start_time = delivery_time.get("startTime", "")
                end_time = delivery_time.get("endTime", "")
                if start_time and end_time and estimated_delivery:
                    # Format times nicely (130000 -> 1:00 PM)
                    try:
                        start_hour = int(start_time[:2])
                        end_hour = int(end_time[:2])
                        start_min = start_time[2:4]
                        end_min = end_time[2:4]
                        start_ampm = "AM" if start_hour < 12 else "PM"
                        end_ampm = "AM" if end_hour < 12 else "PM"
                        if start_hour > 12: start_hour -= 12
                        if end_hour > 12: end_hour -= 12
                        if start_hour == 0: start_hour = 12
                        if end_hour == 0: end_hour = 12
                        estimated_delivery += f" ({start_hour}:{start_min} {start_ampm} - {end_hour}:{end_min} {end_ampm})"
                    except:
                        estimated_delivery += f" ({start_time}-{end_time})"

            # Determine simplified status with more comprehensive mapping
            status = "unknown"

            # Check status type first (more reliable)
            if status_type == "D":  # Delivered
                status = "delivered"
            elif status_type == "I":  # In Transit
                status = "in_transit"
            elif status_type == "P":  # Pickup
                status = "in_transit"
            elif status_type == "M":  # Manifest/Billing info received
                status = "label_created"
            elif status_type == "X":  # Exception
                status = "exception"
            elif status_type == "RS":  # Returned to Shipper
                status = "exception"

            # Fall back to status code if type didn't match
            if status == "unknown":
                if status_code in ["011", "KB", "KM"]:  # Delivered codes (011=Delivered)
                    status = "delivered"
                elif status_code in ["M", "MP", "P", "J", "W", "A", "AR", "AF", "OR", "DP", "OT", "IT", "005", "012", "021", "022"]:
                    # In transit codes: M=Manifest, MP=Manifest Pickup, P=Pickup,
                    # J=Package in transit, W=Wait, A=Arrived, AR=Arrival, AF=At Facility,
                    # OR=Out for delivery, DP=Departure, OT=On the way, IT=In transit
                    # 005=On the Way, 012=Clearance in Progress, 021/022=In transit variants
                    status = "in_transit"
                elif status_code in ["I", "MV", "NA"]:  # Label created / Pre-shipment
                    status = "label_created"
                elif status_code in ["X", "RS", "DJ", "D", "RD"]:  # Exception / Delay / Damage
                    status = "exception"

            # Fall back to text matching if still unknown
            if status == "unknown":
                combined_desc = f"{status_desc} {last_activity_desc}".lower()
                # Be strict about "delivered" - must be a clear delivery confirmation
                # Avoid false positives like "scheduled delivery", "delivery attempt", etc.
                if any(phrase in combined_desc for phrase in ["delivered", "left at", "signed by", "received by"]):
                    # But exclude phrases that indicate pending/attempted delivery
                    if not any(exclude in combined_desc for exclude in ["scheduled", "attempt", "will be", "expected", "estimated"]):
                        status = "delivered"
                elif any(x in combined_desc for x in ["transit", "on the way", "in progress", "departed", "arrived", "processing", "cleared", "customs", "out for delivery", "facility"]):
                    status = "in_transit"
                elif any(x in combined_desc for x in ["label", "created", "billing", "shipper created", "ready for ups"]):
                    status = "label_created"
                elif any(x in combined_desc for x in ["exception", "delay", "damage", "return", "refused", "undeliverable"]):
                    status = "exception"
                else:
                    # If we have any activity at all, assume it's moving
                    if activity and len(activity) > 0:
                        status = "in_transit"

            # Get delivery date if delivered
            delivered_date = None
            if status == "delivered" and last_activity.get("date"):
                delivered_date = last_activity.get("date")

            print(f"📊 UPS parsed: status={status}, code={status_code}, type={status_type}, desc={status_desc[:50]}, est={estimated_delivery}")

            return {
                "status": status,
                "status_description": status_desc or last_activity_desc,
                "last_activity": last_activity.get("date", "") or last_activity.get("time", ""),
                "location": location,
                "delivered_date": delivered_date,
                "estimated_delivery": estimated_delivery,
                "raw_status_code": status_code,
                "raw_status_desc": status_desc
            }

        except Exception as e:
            print(f"❌ Error parsing UPS response: {e}")
            import traceback
            traceback.print_exc()
            return {
                "status": "error",
                "status_description": "Failed to parse tracking data",
                "error": str(e)
            }

    # ══════════════════════════════════════════════════════════════════════════
    # UPS Track Alert API (Webhooks)
    # ══════════════════════════════════════════════════════════════════════════

    def subscribe_track_alerts(
        self,
        tracking_numbers: list,
        webhook_url: str,
        webhook_credential: str,
        locale: str = "en_US"
    ) -> Dict[str, Any]:
        """
        Subscribe tracking numbers to UPS Track Alert for webhook notifications.

        UPS will POST status updates to your webhook URL when tracking events occur.
        Subscriptions are valid for 14 days.

        Args:
            tracking_numbers: List of 1Z or 1R tracking numbers (max 100)
            webhook_url: HTTPS URL to receive webhook POSTs
            webhook_credential: Auth token UPS will include in webhook requests
            locale: Language/region code (default "en_US")

        Returns:
            Dict with subscription results per tracking number
        """
        if not self.enabled:
            return {
                "success": False,
                "error": "UPS API not configured"
            }

        if not tracking_numbers:
            return {
                "success": False,
                "error": "No tracking numbers provided"
            }

        # UPS allows max 100 per request
        if len(tracking_numbers) > 100:
            return {
                "success": False,
                "error": f"Too many tracking numbers ({len(tracking_numbers)}). Max is 100."
            }

        token = self.get_access_token()
        if not token:
            return {
                "success": False,
                "error": "Could not obtain UPS access token"
            }

        try:
            url = "https://onlinetools.ups.com/api/track/v1/subscription/enhanced/package"

            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "transId": str(int(time.time())),
                "transactionSrc": "parcel_scanner"
            }

            payload = {
                "locale": locale,
                "trackingNumberList": tracking_numbers,
                "destination": {
                    "url": webhook_url,
                    "credentialType": "Bearer",
                    "credential": webhook_credential
                },
                # D=Delivery, I=In-Progress, M=Manifest, X=Exception
                "eventPreferences": ["D", "I", "M", "X"]
            }

            print(f"📡 Subscribing {len(tracking_numbers)} tracking numbers to UPS Track Alert...")
            response = requests.post(url, headers=headers, json=payload, timeout=15)

            if response.status_code == 200:
                data = response.json()
                print(f"✅ UPS Track Alert subscription successful")
                return {
                    "success": True,
                    "data": data
                }
            else:
                print(f"❌ UPS Track Alert subscription failed: {response.status_code} - {response.text[:300]}")
                return {
                    "success": False,
                    "status_code": response.status_code,
                    "error": response.text[:300]
                }

        except Exception as e:
            print(f"❌ UPS Track Alert subscription exception: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    @staticmethod
    def parse_webhook_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse a UPS Track Alert webhook payload into a standardized format.

        Args:
            payload: Raw webhook JSON from UPS

        Returns:
            Parsed tracking status in same format as get_tracking_status()
        """
        try:
            tracking_number = payload.get("trackingNumber", "")
            activity_status = payload.get("activityStatus", {})

            status_type = activity_status.get("type", "")
            status_code = activity_status.get("code", "")
            status_desc = activity_status.get("description", "")

            # Parse location
            location_data = payload.get("activityLocation", {})
            city = location_data.get("city", "")
            state = location_data.get("stateProvince", "")
            country = location_data.get("country", "")
            location = f"{city}, {state}, {country}".strip(", ")

            # Parse dates
            local_date = payload.get("localActivityDate", "")  # YYYYMMDD
            local_time = payload.get("localActivityTime", "")  # HHMMSS
            actual_delivery_date = payload.get("actualDeliveryDate", "")

            # Determine status
            status = "unknown"
            if status_type == "D":
                status = "delivered"
            elif status_type == "I":
                status = "in_transit"
            elif status_type == "M":
                status = "label_created"
            elif status_type == "X":
                status = "exception"

            # Format estimated delivery
            estimated_delivery = ""
            scheduled = payload.get("scheduledDeliveryDate", "")
            if scheduled:
                try:
                    from datetime import datetime
                    dt = datetime.strptime(scheduled, "%Y%m%d")
                    estimated_delivery = dt.strftime("%B %d")
                except:
                    estimated_delivery = scheduled

            return {
                "tracking_number": tracking_number,
                "status": status,
                "status_description": status_desc,
                "location": location,
                "local_date": local_date,
                "local_time": local_time,
                "delivered_date": actual_delivery_date if status == "delivered" else None,
                "estimated_delivery": estimated_delivery,
                "received_by": payload.get("receivedBy", ""),
                "raw_status_code": status_code,
                "raw_status_type": status_type
            }

        except Exception as e:
            print(f"❌ Error parsing UPS webhook payload: {e}")
            return {
                "status": "error",
                "error": str(e)
            }


class UPSShippingAPI:
    """
    UPS Shipping & Rating API.
    Extends tracking to support rate quotes and label creation with customs.
    """

    def __init__(self):
        """
        Initialize UPS Shipping API.
        Requires: UPS_CLIENT_ID, UPS_CLIENT_SECRET, UPS_ACCOUNT_NUMBER
        """
        self.client_id = os.environ.get("UPS_CLIENT_ID", "")
        self.client_secret = os.environ.get("UPS_CLIENT_SECRET", "")
        self.account_number = os.environ.get("UPS_ACCOUNT_NUMBER", "")

        self._access_token = None
        self._token_expires_at = 0

        # Production endpoint
        self.base_url = "https://onlinetools.ups.com/api"

        self.enabled = bool(self.client_id and self.client_secret)
        self.rating_enabled = bool(self.enabled and self.account_number)

        if not self.enabled:
            print("⚠️ UPS Shipping API not configured (missing credentials)")
        elif not self.account_number:
            print("⚠️ UPS_ACCOUNT_NUMBER not set - rating/shipping disabled")

        # Shipper address from environment
        self.shipper_address = {
            "name": os.environ.get("COMPANY_NAME", "Hemlock & Oak Stationery Inc."),
            "attention_name": "Shipping Dept",
            "phone": os.environ.get("COMPANY_PHONE", ""),
            "address_line1": os.environ.get("WAREHOUSE_ADDRESS1", ""),
            "address_line2": os.environ.get("WAREHOUSE_ADDRESS2", ""),
            "city": os.environ.get("WAREHOUSE_CITY", ""),
            "state": os.environ.get("WAREHOUSE_PROVINCE", "BC"),
            "postal_code": os.environ.get("WAREHOUSE_POSTAL", ""),
            "country_code": "CA"
        }

    def _get_oauth_token(self) -> Optional[str]:
        """Get OAuth 2.0 access token (cached until expiry)."""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return self._access_token

        try:
            credentials = f"{self.client_id}:{self.client_secret}"
            encoded_credentials = base64.b64encode(credentials.encode()).decode()

            response = requests.post(
                "https://onlinetools.ups.com/security/v1/oauth/token",
                data={"grant_type": "client_credentials"},
                headers={
                    "Authorization": f"Basic {encoded_credentials}",
                    "Content-Type": "application/x-www-form-urlencoded"
                },
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                self._access_token = data.get("access_token")
                expires_in = int(data.get("expires_in", 3600))
                self._token_expires_at = time.time() + expires_in
                return self._access_token
            else:
                print(f"UPS OAuth error: {response.status_code}")
                return None

        except Exception as e:
            print(f"UPS OAuth exception: {e}")
            return None

    def get_rates(
        self,
        destination: Dict[str, str],
        packages: list,
        customs_items: list = None
    ) -> Dict[str, Any]:
        """
        Get shipping rates from UPS.

        Args:
            destination: {
                "name": "Customer Name",
                "address_line1": "123 Main St",
                "city": "New York",
                "state": "NY",
                "postal_code": "10001",
                "country_code": "US"
            }
            packages: [{
                "weight_kg": 0.5,
                "length_cm": 25,
                "width_cm": 18,
                "height_cm": 5
            }]
            customs_items: (for international) [{
                "description": "Planner agenda",
                "hs_code": "4820102010",
                "country_of_origin": "CA",
                "quantity": 1,
                "value": 74.10,
                "weight_kg": 0.3
            }]

        Returns:
            {
                "success": True,
                "rates": [
                    {
                        "service_code": "08",
                        "service_name": "UPS Worldwide Expedited",
                        "total_charge": 25.43,
                        "currency": "CAD",
                        "delivery_days": 3,
                        "delivery_date": "2025-12-10"
                    },
                    ...
                ]
            }
        """
        if not self.rating_enabled:
            return {"success": False, "error": "UPS Rating API not configured (missing account number)"}

        token = self._get_oauth_token()
        if not token:
            return {"success": False, "error": "Failed to get UPS OAuth token"}

        is_international = destination.get("country_code", "CA").upper() not in ["CA", "CANADA"]

        # Build package objects
        ups_packages = []
        for pkg in packages:
            ups_package = {
                "PackagingType": {"Code": "02", "Description": "Package"},
                "Dimensions": {
                    "UnitOfMeasurement": {"Code": "CM"},
                    "Length": str(int(pkg.get("length_cm", 25))),
                    "Width": str(int(pkg.get("width_cm", 18))),
                    "Height": str(int(pkg.get("height_cm", 5)))
                },
                "PackageWeight": {
                    "UnitOfMeasurement": {"Code": "KGS"},
                    "Weight": str(round(pkg.get("weight_kg", 0.5), 2))
                }
            }
            ups_packages.append(ups_package)

        # Build shipper address lines
        shipper_lines = [self.shipper_address.get("address_line1", "")]
        if self.shipper_address.get("address_line2"):
            shipper_lines.append(self.shipper_address["address_line2"])

        # Build destination address lines
        dest_lines = [destination.get("address_line1", "")]
        if destination.get("address_line2"):
            dest_lines.append(destination["address_line2"])

        # Build rate request
        rate_request = {
            "RateRequest": {
                "Request": {
                    "SubVersion": "2205",
                    "TransactionReference": {"CustomerContext": "Rating"}
                },
                "Shipment": {
                    "Shipper": {
                        "Name": self.shipper_address.get("name", "Shipper"),
                        "ShipperNumber": self.account_number,
                        "Address": {
                            "AddressLine": shipper_lines,
                            "City": self.shipper_address.get("city", ""),
                            "StateProvinceCode": self.shipper_address.get("state", "BC"),
                            "PostalCode": self.shipper_address.get("postal_code", ""),
                            "CountryCode": self.shipper_address.get("country_code", "CA")
                        }
                    },
                    "ShipTo": {
                        "Name": destination.get("name", "Customer"),
                        "Address": {
                            "AddressLine": dest_lines,
                            "City": destination.get("city", ""),
                            "StateProvinceCode": destination.get("state", ""),
                            "PostalCode": destination.get("postal_code", ""),
                            "CountryCode": destination.get("country_code", "CA")
                        }
                    },
                    "ShipFrom": {
                        "Name": self.shipper_address.get("name", "Shipper"),
                        "Address": {
                            "AddressLine": shipper_lines,
                            "City": self.shipper_address.get("city", ""),
                            "StateProvinceCode": self.shipper_address.get("state", "BC"),
                            "PostalCode": self.shipper_address.get("postal_code", ""),
                            "CountryCode": self.shipper_address.get("country_code", "CA")
                        }
                    },
                    "PaymentDetails": {
                        "ShipmentCharge": [{
                            "Type": "01",
                            "BillShipper": {"AccountNumber": self.account_number}
                        }]
                    },
                    "Package": ups_packages,
                    "ShipmentRatingOptions": {
                        "NegotiatedRatesIndicator": ""
                    }
                }
            }
        }

        # Add international customs info
        if is_international and customs_items:
            total_value = sum(
                (item.get("value", 0) or 0) * (item.get("quantity", 1) or 1)
                for item in customs_items
            )

            rate_request["RateRequest"]["Shipment"]["InvoiceLineTotal"] = {
                "CurrencyCode": "CAD",
                "MonetaryValue": str(round(total_value, 2))
            }

        try:
            response = requests.post(
                f"{self.base_url}/rating/v2205/Shop",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "transId": f"rate_{int(time.time())}",
                    "transactionSrc": "HO-ParcelScanner"
                },
                json=rate_request,
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                rated_shipments = data.get("RateResponse", {}).get("RatedShipment", [])

                rates = []
                for rs in rated_shipments:
                    service = rs.get("Service", {})
                    total = rs.get("TotalCharges", {})
                    negotiated = rs.get("NegotiatedRateCharges", {}).get("TotalCharge", {})

                    charge = negotiated if negotiated else total

                    delivery_days = None
                    delivery_date = None
                    if rs.get("GuaranteedDelivery"):
                        delivery_days = rs["GuaranteedDelivery"].get("BusinessDaysInTransit")
                    if rs.get("TimeInTransit", {}).get("ServiceSummary"):
                        est_arrival = rs["TimeInTransit"]["ServiceSummary"].get("EstimatedArrival", {})
                        delivery_date = est_arrival.get("Arrival", {}).get("Date")

                    rates.append({
                        "service_code": service.get("Code", ""),
                        "service_name": self._get_service_name(service.get("Code", "")),
                        "total_charge": float(charge.get("MonetaryValue", 0)),
                        "currency": charge.get("CurrencyCode", "CAD"),
                        "delivery_days": int(delivery_days) if delivery_days else None,
                        "delivery_date": delivery_date
                    })

                rates.sort(key=lambda x: x["total_charge"])

                return {"success": True, "rates": rates}
            else:
                error_msg = response.text[:500]
                print(f"UPS Rating error: {response.status_code} - {error_msg}")
                return {"success": False, "error": f"UPS API error: {response.status_code}"}

        except Exception as e:
            print(f"UPS Rating exception: {e}")
            return {"success": False, "error": str(e)}

    def _get_service_name(self, code: str) -> str:
        """Map UPS service codes to friendly names."""
        services = {
            "01": "UPS Next Day Air",
            "02": "UPS 2nd Day Air",
            "03": "UPS Ground",
            "07": "UPS Worldwide Express",
            "08": "UPS Worldwide Expedited",
            "11": "UPS Standard",
            "12": "UPS 3 Day Select",
            "13": "UPS Next Day Air Saver",
            "14": "UPS Next Day Air Early",
            "54": "UPS Worldwide Express Plus",
            "59": "UPS 2nd Day Air A.M.",
            "65": "UPS Worldwide Saver",
            "82": "UPS Today Standard",
            "83": "UPS Today Dedicated Courier",
            "84": "UPS Today Intercity",
            "85": "UPS Today Express",
            "86": "UPS Today Express Saver",
            "96": "UPS Worldwide Express Freight"
        }
        return services.get(code, f"UPS Service {code}")

    def create_label(
        self,
        shipper: Dict[str, str],
        ship_to: Dict[str, str],
        packages: list,
        service_code: str = "11",  # Default: UPS Standard
        customs_items: list = None,
        reference1: str = None,
        reference2: str = None
    ) -> Dict[str, Any]:
        """
        Create a UPS shipping label with optional customs for international shipments.

        Args:
            shipper: {
                "name": "Company Name",
                "attention_name": "Contact Name",
                "phone": "1234567890",
                "address_line1": "123 Main St",
                "address_line2": "",
                "city": "Vancouver",
                "state": "BC",
                "postal_code": "V6B1A1",
                "country_code": "CA"
            }
            ship_to: {
                "name": "Customer Name",
                "attention_name": "Customer Name",
                "phone": "0987654321",
                "address_line1": "456 Customer Ave",
                "city": "New York",
                "state": "NY",
                "postal_code": "10001",
                "country_code": "US"
            }
            packages: [{
                "weight_kg": 0.5,
                "length_cm": 25,
                "width_cm": 18,
                "height_cm": 5,
                "description": "Planner"
            }]
            service_code: UPS service code (e.g., "11" for Standard, "08" for Expedited)
            customs_items: (for international) [{
                "description": "Planner agenda",
                "hs_code": "4820102010",
                "country_of_origin": "CA",
                "quantity": 1,
                "value": 74.10,
                "weight_kg": 0.3
            }]
            reference1: Order number or reference
            reference2: Additional reference

        Returns:
            {
                "success": True,
                "tracking_number": "1Z...",
                "label_image": "base64_encoded_gif_or_pdf",
                "label_format": "GIF",
                "total_charge": 25.43,
                "currency": "CAD",
                "service_code": "11",
                "service_name": "UPS Standard"
            }
        """
        if not self.rating_enabled:
            return {"success": False, "error": "UPS Shipping API not configured"}

        token = self._get_oauth_token()
        if not token:
            return {"success": False, "error": "Failed to get UPS OAuth token"}

        is_international = ship_to.get("country_code", "CA").upper() != shipper.get("country_code", "CA").upper()

        # Build package objects
        ups_packages = []
        for i, pkg in enumerate(packages):
            ups_package = {
                "Description": pkg.get("description", "Merchandise")[:35],
                "Packaging": {"Code": "02", "Description": "Package"},
                "Dimensions": {
                    "UnitOfMeasurement": {"Code": "CM", "Description": "Centimeters"},
                    "Length": str(int(pkg.get("length_cm", 25))),
                    "Width": str(int(pkg.get("width_cm", 18))),
                    "Height": str(int(pkg.get("height_cm", 5)))
                },
                "PackageWeight": {
                    "UnitOfMeasurement": {"Code": "KGS", "Description": "Kilograms"},
                    "Weight": str(round(pkg.get("weight_kg", 0.5), 2))
                }
            }

            # Add reference numbers to first package
            if i == 0 and (reference1 or reference2):
                refs = []
                if reference1:
                    refs.append({"Code": "00", "Value": str(reference1)[:35]})
                if reference2:
                    refs.append({"Code": "00", "Value": str(reference2)[:35]})
                if refs:
                    ups_package["ReferenceNumber"] = refs

            ups_packages.append(ups_package)

        # Build shipper address
        shipper_lines = [shipper.get("address_line1", "")]
        if shipper.get("address_line2"):
            shipper_lines.append(shipper["address_line2"])

        # Build ship-to address
        ship_to_lines = [ship_to.get("address_line1", "")]
        if ship_to.get("address_line2"):
            ship_to_lines.append(ship_to["address_line2"])

        # Build shipment request
        shipment_request = {
            "ShipmentRequest": {
                "Request": {
                    "SubVersion": "2205",
                    "RequestOption": "nonvalidate",
                    "TransactionReference": {"CustomerContext": f"Label_{reference1 or 'order'}"}
                },
                "Shipment": {
                    "Description": "Stationery products",
                    "Shipper": {
                        "Name": shipper.get("name", "Shipper")[:35],
                        "AttentionName": shipper.get("attention_name", shipper.get("name", ""))[:35],
                        "Phone": {"Number": shipper.get("phone", "")[:15]},
                        "ShipperNumber": self.account_number,
                        "Address": {
                            "AddressLine": shipper_lines,
                            "City": shipper.get("city", "")[:30],
                            "StateProvinceCode": shipper.get("state", "")[:5],
                            "PostalCode": shipper.get("postal_code", "").replace(" ", ""),
                            "CountryCode": shipper.get("country_code", "CA")
                        }
                    },
                    "ShipTo": {
                        "Name": ship_to.get("name", "Customer")[:35],
                        "AttentionName": ship_to.get("attention_name", ship_to.get("name", ""))[:35],
                        "Phone": {"Number": ship_to.get("phone", "")[:15] or "0000000000"},
                        "Address": {
                            "AddressLine": ship_to_lines,
                            "City": ship_to.get("city", "")[:30],
                            "StateProvinceCode": ship_to.get("state", "")[:5],
                            "PostalCode": ship_to.get("postal_code", "").replace(" ", ""),
                            "CountryCode": ship_to.get("country_code", "CA")
                        }
                    },
                    "ShipFrom": {
                        "Name": shipper.get("name", "Shipper")[:35],
                        "AttentionName": shipper.get("attention_name", shipper.get("name", ""))[:35],
                        "Phone": {"Number": shipper.get("phone", "")[:15]},
                        "Address": {
                            "AddressLine": shipper_lines,
                            "City": shipper.get("city", "")[:30],
                            "StateProvinceCode": shipper.get("state", "")[:5],
                            "PostalCode": shipper.get("postal_code", "").replace(" ", ""),
                            "CountryCode": shipper.get("country_code", "CA")
                        }
                    },
                    "PaymentInformation": {
                        "ShipmentCharge": [{
                            "Type": "01",
                            "BillShipper": {"AccountNumber": self.account_number}
                        }]
                    },
                    "Service": {
                        "Code": service_code,
                        "Description": self._get_service_name(service_code)
                    },
                    "Package": ups_packages
                },
                "LabelSpecification": {
                    "LabelImageFormat": {"Code": "GIF", "Description": "GIF"},
                    "LabelStockSize": {"Height": "6", "Width": "4"}
                }
            }
        }

        # Add international customs documentation
        if is_international and customs_items:
            total_value = sum(
                (item.get("value", 0) or 0) * (item.get("quantity", 1) or 1)
                for item in customs_items
            )

            products = []
            for item in customs_items:
                product = {
                    "Description": item.get("description", "Merchandise")[:35],
                    "Unit": {
                        "Number": str(item.get("quantity", 1)),
                        "Value": str(round(item.get("value", 0), 2)),
                        "UnitOfMeasurement": {"Code": "PCS", "Description": "Pieces"}
                    },
                    "CommodityCode": item.get("hs_code", "4820102010")[:15],
                    "OriginCountryCode": item.get("country_of_origin", "CA"),
                    "ProductWeight": {
                        "UnitOfMeasurement": {"Code": "KGS"},
                        "Weight": str(round(item.get("weight_kg", 0.1), 2))
                    }
                }
                products.append(product)

            # International forms
            shipment_request["ShipmentRequest"]["Shipment"]["ShipmentServiceOptions"] = {
                "InternationalForms": {
                    "FormType": ["01"],  # Invoice
                    "InvoiceNumber": str(reference1 or "INV001")[:35],
                    "InvoiceDate": time.strftime("%Y%m%d"),
                    "ReasonForExport": "SALE",
                    "CurrencyCode": "CAD",
                    "Product": products,
                    "Contacts": {
                        "SoldTo": {
                            "Name": ship_to.get("name", "Customer")[:35],
                            "AttentionName": ship_to.get("attention_name", ship_to.get("name", ""))[:35],
                            "Phone": {"Number": ship_to.get("phone", "0000000000")[:15]},
                            "Address": {
                                "AddressLine": ship_to_lines,
                                "City": ship_to.get("city", "")[:30],
                                "StateProvinceCode": ship_to.get("state", "")[:5],
                                "PostalCode": ship_to.get("postal_code", "").replace(" ", ""),
                                "CountryCode": ship_to.get("country_code", "US")
                            }
                        }
                    }
                }
            }

        try:
            response = requests.post(
                f"{self.base_url}/shipments/v2205/ship",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "transId": f"ship_{int(time.time())}",
                    "transactionSrc": "HO-ParcelScanner"
                },
                json=shipment_request,
                timeout=60
            )

            if response.status_code == 200:
                data = response.json()
                shipment_results = data.get("ShipmentResponse", {}).get("ShipmentResults", {})

                tracking_number = shipment_results.get("ShipmentIdentificationNumber", "")
                package_results = shipment_results.get("PackageResults", [])
                if isinstance(package_results, dict):
                    package_results = [package_results]

                # Get label image from first package
                label_image = ""
                if package_results:
                    label = package_results[0].get("ShippingLabel", {})
                    graphic_image = label.get("GraphicImage", "")
                    label_image = graphic_image

                # Get charges
                shipment_charges = shipment_results.get("ShipmentCharges", {})
                total_charges = shipment_charges.get("TotalCharges", {})
                total_amount = float(total_charges.get("MonetaryValue", 0))
                currency = total_charges.get("CurrencyCode", "CAD")

                return {
                    "success": True,
                    "tracking_number": tracking_number,
                    "label_image": label_image,
                    "label_format": "GIF",
                    "total_charge": total_amount,
                    "currency": currency,
                    "service_code": service_code,
                    "service_name": self._get_service_name(service_code),
                    "raw_response": data
                }
            else:
                error_msg = response.text[:1000]
                print(f"UPS Shipping error: {response.status_code} - {error_msg}")

                # Try to parse error details
                try:
                    error_data = response.json()
                    errors = error_data.get("response", {}).get("errors", [])
                    if errors:
                        error_msg = "; ".join([e.get("message", "") for e in errors])
                except:
                    pass

                return {"success": False, "error": f"UPS API error: {error_msg}"}

        except Exception as e:
            print(f"UPS Shipping exception: {e}")
            import traceback
            traceback.print_exc()
            return {"success": False, "error": str(e)}


# Singleton instances
_ups_api = None
_ups_shipping_api = None


def get_ups_api() -> UPSAPI:
    """Get or create singleton UPS Tracking API instance."""
    global _ups_api
    if _ups_api is None:
        _ups_api = UPSAPI()
    return _ups_api


def get_ups_shipping_api() -> UPSShippingAPI:
    """Get or create singleton UPS Shipping/Rating API instance."""
    global _ups_shipping_api
    if _ups_shipping_api is None:
        _ups_shipping_api = UPSShippingAPI()
    return _ups_shipping_api
