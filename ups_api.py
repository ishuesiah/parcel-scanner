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
