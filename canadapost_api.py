# canadapost_api.py
"""
Canada Post Tracking API integration for shipment tracking.
Uses Basic Authentication with username/password.
"""

import os
import requests
import base64
import xml.etree.ElementTree as ET
from typing import Optional, Dict, Any


class CanadaPostAPI:
    def __init__(self):
        """
        Initialize Canada Post API client.
        Reads CANADAPOST_USERNAME and CANADAPOST_PASSWORD from environment.
        Set CANADAPOST_ENV to 'production' for live API (default is development).
        """
        self.username = os.environ.get("CANADAPOST_USERNAME", "")
        self.password = os.environ.get("CANADAPOST_PASSWORD", "")
        self.env = os.environ.get("CANADAPOST_ENV", "development").lower()

        if not self.username or not self.password:
            print("WARNING: CANADAPOST_USERNAME or CANADAPOST_PASSWORD not set in environment")
            self.enabled = False
        else:
            self.enabled = True

        # API endpoints based on environment
        if self.env == "production":
            self.base_url = "https://soa-gw.canadapost.ca"
        else:
            self.base_url = "https://ct.soa-gw.canadapost.ca"
            print(f"Canada Post API running in DEVELOPMENT mode")

    def _get_auth_header(self) -> str:
        """Generate Basic Auth header value."""
        credentials = f"{self.username}:{self.password}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

    def _get_headers(self) -> Dict[str, str]:
        """Get standard headers for Canada Post API requests."""
        return {
            "Accept": "application/vnd.cpc.track-v2+xml",
            "Authorization": self._get_auth_header(),
            "Accept-language": "en-CA"
        }

    def get_tracking_summary(self, pin: str) -> Dict[str, Any]:
        """
        Get tracking summary for a Canada Post PIN (tracking number).

        Args:
            pin: Canada Post tracking number (12, 13, or 16 characters)

        Returns:
            Dict with status information similar to UPS API format
        """
        if not self.enabled:
            return {
                "status": "error",
                "status_description": "Canada Post API not configured",
                "error": "CANADAPOST_USERNAME or CANADAPOST_PASSWORD not set"
            }

        try:
            url = f"{self.base_url}/vis/track/pin/{pin}/summary"

            print(f"Querying Canada Post tracking for {pin}...")
            response = requests.get(url, headers=self._get_headers(), timeout=15)

            if response.status_code == 200:
                return self._parse_summary_response(response.text, pin)
            elif response.status_code == 404:
                return {
                    "status": "unknown",
                    "status_description": "Tracking number not found",
                    "error": "No tracking information available"
                }
            else:
                print(f"Canada Post API error: {response.status_code} - {response.text[:500]}")
                # Try to parse error from XML
                error_msg = self._parse_error(response.text)
                return {
                    "status": "error",
                    "status_description": error_msg or f"API error {response.status_code}",
                    "error": response.text[:200]
                }

        except requests.Timeout:
            print(f"Canada Post API timeout for {pin}")
            return {
                "status": "error",
                "status_description": "Request timed out",
                "error": "Canada Post API request timed out"
            }
        except Exception as e:
            print(f"Canada Post API exception: {e}")
            return {
                "status": "error",
                "status_description": "Request failed",
                "error": str(e)
            }

    def get_tracking_details(self, pin: str) -> Dict[str, Any]:
        """
        Get detailed tracking events for a Canada Post PIN.

        Args:
            pin: Canada Post tracking number

        Returns:
            Dict with detailed tracking information
        """
        if not self.enabled:
            return {
                "status": "error",
                "status_description": "Canada Post API not configured",
                "error": "CANADAPOST_USERNAME or CANADAPOST_PASSWORD not set"
            }

        try:
            url = f"{self.base_url}/vis/track/pin/{pin}/detail"

            print(f"Querying Canada Post tracking details for {pin}...")
            response = requests.get(url, headers=self._get_headers(), timeout=15)

            if response.status_code == 200:
                return self._parse_detail_response(response.text, pin)
            elif response.status_code == 404:
                return {
                    "status": "unknown",
                    "status_description": "Tracking number not found",
                    "error": "No tracking information available"
                }
            else:
                print(f"Canada Post API error: {response.status_code} - {response.text[:500]}")
                error_msg = self._parse_error(response.text)
                return {
                    "status": "error",
                    "status_description": error_msg or f"API error {response.status_code}",
                    "error": response.text[:200]
                }

        except Exception as e:
            print(f"Canada Post API exception: {e}")
            return {
                "status": "error",
                "status_description": "Request failed",
                "error": str(e)
            }

    def _parse_error(self, xml_text: str) -> Optional[str]:
        """Parse error message from XML response."""
        try:
            root = ET.fromstring(xml_text)
            # Look for messages/message/description
            for desc in root.iter():
                if 'description' in desc.tag.lower():
                    return desc.text
            return None
        except:
            return None

    def _parse_summary_response(self, xml_text: str, pin: str) -> Dict[str, Any]:
        """
        Parse Canada Post tracking summary XML response.

        Returns dict compatible with our tracking cache format.
        """
        try:
            root = ET.fromstring(xml_text)

            # Helper to get element text safely
            def get_text(parent, tag):
                # Handle namespaced XML
                for elem in parent.iter():
                    if elem.tag.endswith(tag) or elem.tag == tag:
                        return elem.text or ""
                return ""

            # Find pin-summary element
            pin_summary = None
            for elem in root.iter():
                if 'pin-summary' in elem.tag:
                    pin_summary = elem
                    break

            if pin_summary is None:
                pin_summary = root

            event_type = get_text(pin_summary, 'event-type')
            event_description = get_text(pin_summary, 'event-description')
            actual_delivery = get_text(pin_summary, 'actual-delivery-date')
            expected_delivery = get_text(pin_summary, 'expected-delivery-date')
            event_location = get_text(pin_summary, 'event-location')
            destination_province = get_text(pin_summary, 'destination-province')
            mailed_on = get_text(pin_summary, 'mailed-on-date')
            service_name = get_text(pin_summary, 'service-name')

            # Determine simplified status based on event-type
            status = self._map_event_type_to_status(event_type, event_description)

            # Format location
            location = event_location
            if destination_province and not location:
                location = destination_province

            # Format estimated delivery
            est_delivery = ""
            if actual_delivery:
                est_delivery = f"Delivered: {actual_delivery}"
            elif expected_delivery:
                est_delivery = expected_delivery

            print(f"Canada Post parsed: status={status}, event_type={event_type}, desc={event_description[:50] if event_description else 'N/A'}")

            return {
                "status": status,
                "status_description": event_description or service_name,
                "last_activity": mailed_on,
                "location": location,
                "delivered_date": actual_delivery if status == "delivered" else None,
                "estimated_delivery": est_delivery,
                "raw_status_code": event_type,
                "raw_status_desc": event_description
            }

        except ET.ParseError as e:
            print(f"Failed to parse Canada Post XML: {e}")
            print(f"Raw response: {xml_text[:500]}")
            return {
                "status": "error",
                "status_description": "Failed to parse tracking data",
                "error": str(e)
            }
        except Exception as e:
            print(f"Error parsing Canada Post response: {e}")
            import traceback
            traceback.print_exc()
            return {
                "status": "error",
                "status_description": "Failed to parse tracking data",
                "error": str(e)
            }

    def _parse_detail_response(self, xml_text: str, pin: str) -> Dict[str, Any]:
        """
        Parse Canada Post tracking detail XML response.
        Returns more detailed information including all events.
        """
        try:
            root = ET.fromstring(xml_text)

            def get_text(parent, tag):
                for elem in parent.iter():
                    if elem.tag.endswith(tag) or elem.tag == tag:
                        return elem.text or ""
                return ""

            expected_delivery = get_text(root, 'expected-delivery-date')
            service_name = get_text(root, 'service-name')

            # Get all events
            events = []
            for elem in root.iter():
                if 'occurrence' in elem.tag or elem.tag == 'item':
                    event = {
                        "event_id": get_text(elem, 'event-identifier'),
                        "date": get_text(elem, 'event-date'),
                        "time": get_text(elem, 'event-time'),
                        "description": get_text(elem, 'event-description'),
                        "location": get_text(elem, 'event-site'),
                        "province": get_text(elem, 'event-province')
                    }
                    if event["date"]:  # Only add if has date
                        events.append(event)

            # Get most recent event
            latest_event = events[0] if events else {}
            event_type = latest_event.get("event_id", "")
            event_description = latest_event.get("description", "")

            # Determine status from latest event
            status = self._map_event_id_to_status(event_type, event_description)

            location = ""
            if latest_event:
                site = latest_event.get("location", "")
                province = latest_event.get("province", "")
                location = f"{site}, {province}".strip(", ")

            return {
                "status": status,
                "status_description": event_description or service_name,
                "last_activity": latest_event.get("date", ""),
                "location": location,
                "estimated_delivery": expected_delivery,
                "events": events,
                "raw_status_code": event_type,
                "raw_status_desc": event_description
            }

        except Exception as e:
            print(f"Error parsing Canada Post detail response: {e}")
            return {
                "status": "error",
                "status_description": "Failed to parse tracking data",
                "error": str(e)
            }

    def _map_event_type_to_status(self, event_type: str, description: str = "") -> str:
        """
        Map Canada Post event-type to simplified status.

        Event types from docs:
        - DELIVERED: Package delivered
        - ATTEMPTED: Delivery attempted but not completed
        - INFO: Informational update
        - OUT: Out for delivery
        - INDUCTION: Package inducted/accepted
        - SIGNATURE: Signature captured
        - etc.
        """
        event_type_upper = (event_type or "").upper()
        desc_lower = (description or "").lower()

        # Check for delivered
        if event_type_upper == "DELIVERED":
            return "delivered"

        # Check for out for delivery
        if event_type_upper == "OUT":
            return "in_transit"  # Will show as "Almost There" in UI

        # Check for attempted delivery
        if event_type_upper == "ATTEMPTED":
            if "returned" in desc_lower or "return" in desc_lower:
                return "exception"
            return "exception"  # Delivery attempted but failed

        # Check for induction (label created / accepted)
        if event_type_upper == "INDUCTION":
            return "label_created"

        # Check description for more context
        if "delivered" in desc_lower:
            return "delivered"
        if "out for delivery" in desc_lower:
            return "in_transit"
        if any(x in desc_lower for x in ["processed", "transit", "arrived", "departed", "in transit"]):
            return "in_transit"
        if any(x in desc_lower for x in ["accepted", "received", "picked up", "electronic information"]):
            return "label_created"
        if any(x in desc_lower for x in ["return", "refused", "undeliverable", "cannot be delivered"]):
            return "exception"

        # Default to in_transit for INFO and other types
        if event_type_upper in ["INFO", "VEHICLE_INFO", "INCOMING", "TO_RETAIL", "TO_APL"]:
            return "in_transit"

        return "unknown"

    def _map_event_id_to_status(self, event_id: str, description: str = "") -> str:
        """
        Map Canada Post event-identifier (numeric) to simplified status.
        Based on the tracking scan events table in docs.
        """
        try:
            event_num = int(event_id) if event_id else 0
        except ValueError:
            event_num = 0

        # Delivered events (1405-1476 range, and others)
        delivered_events = [
            1405, 1406, 1408, 1409, 1421, 1422, 1423, 1424, 1425, 1426, 1427, 1428,
            1429, 1430, 1431, 1432, 1433, 1434, 1441, 1442, 1461, 1462, 1463, 1465,
            1466, 1467, 1468, 1469, 1471, 1472, 1475, 1476, 1496, 1498
        ]

        # Out for delivery events
        out_for_delivery_events = [174, 500]

        # Attempted/Exception events
        attempted_events = [
            167, 168, 169, 181, 182, 183, 184, 700, 1407, 1410, 1411, 1412, 1414,
            1415, 1416, 1417, 1418, 1419, 1420, 1435, 1436, 1437, 1438, 1443, 1444,
            1450, 1473, 1479, 1480, 1481, 1482, 1483, 1484, 1487, 1488, 1490, 1491,
            1492, 1493, 1494, 1495, 2407, 2410, 2411, 2412, 2414, 2802
        ]

        # Induction/Label created events
        induction_events = [1300, 1301, 1302, 1303, 2300, 3000, 3001, 3002]

        if event_num in delivered_events:
            return "delivered"
        if event_num in out_for_delivery_events:
            return "in_transit"
        if event_num in attempted_events:
            return "exception"
        if event_num in induction_events:
            return "label_created"

        # Fall back to description-based detection
        return self._map_event_type_to_status("", description)


class CanadaPostShippingAPI:
    """
    Canada Post Shipping & Rating API.
    Supports rate quotes for domestic and international shipments.
    """

    def __init__(self):
        """
        Initialize Canada Post Shipping API.
        Requires: CANADAPOST_USERNAME, CANADAPOST_PASSWORD, CANADAPOST_CUSTOMER_NUMBER
        Optional: CANADAPOST_CONTRACT_ID (for commercial rates)
        """
        self.username = os.environ.get("CANADAPOST_USERNAME", "")
        self.password = os.environ.get("CANADAPOST_PASSWORD", "")
        self.customer_number = os.environ.get("CANADAPOST_CUSTOMER_NUMBER", "")
        self.contract_id = os.environ.get("CANADAPOST_CONTRACT_ID", "")
        self.env = os.environ.get("CANADAPOST_ENV", "development").lower()

        # API endpoints
        if self.env == "production":
            self.base_url = "https://soa-gw.canadapost.ca"
        else:
            self.base_url = "https://ct.soa-gw.canadapost.ca"

        self.enabled = bool(self.username and self.password)
        self.rating_enabled = bool(self.enabled and self.customer_number)

        if not self.enabled:
            print("⚠️ Canada Post Shipping API not configured")
        elif not self.customer_number:
            print("⚠️ CANADAPOST_CUSTOMER_NUMBER not set - rating disabled")

        # Origin postal code from environment
        self.origin_postal = os.environ.get("WAREHOUSE_POSTAL", "").replace(" ", "").upper()

    def _get_auth_header(self) -> str:
        """Generate Basic Auth header value."""
        credentials = f"{self.username}:{self.password}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

    def get_rates(
        self,
        destination_postal: str,
        destination_country: str,
        weight_kg: float,
        dimensions_cm: Dict[str, float] = None,
        customs_items: list = None
    ) -> Dict[str, Any]:
        """
        Get shipping rates from Canada Post.

        Args:
            destination_postal: Postal/ZIP code
            destination_country: ISO country code (CA, US, etc.)
            weight_kg: Total weight in kg
            dimensions_cm: {"length": 25, "width": 18, "height": 5}
            customs_items: (for international) list of items with value

        Returns:
            {
                "success": True,
                "rates": [
                    {
                        "service_code": "DOM.EP",
                        "service_name": "Expedited Parcel",
                        "total_charge": 15.43,
                        "currency": "CAD",
                        "delivery_days": 2
                    },
                    ...
                ]
            }
        """
        if not self.rating_enabled:
            return {"success": False, "error": "Canada Post Rating API not configured"}

        if not self.origin_postal:
            return {"success": False, "error": "WAREHOUSE_POSTAL not configured"}

        is_domestic = destination_country.upper() in ["CA", "CANADA"]
        is_usa = destination_country.upper() in ["US", "USA", "UNITED STATES"]

        # Build mailing scenario XML
        xml_parts = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<mailing-scenario xmlns="http://www.canadapost.ca/ws/ship/rate-v4">',
        ]

        # Add customer info for contract rates
        if self.customer_number:
            xml_parts.append(f'<customer-number>{self.customer_number}</customer-number>')
        if self.contract_id:
            xml_parts.append(f'<contract-id>{self.contract_id}</contract-id>')

        # Origin postal code
        xml_parts.append(f'<origin-postal-code>{self.origin_postal}</origin-postal-code>')

        # Parcel characteristics
        xml_parts.append('<parcel-characteristics>')
        xml_parts.append(f'<weight>{weight_kg:.3f}</weight>')

        if dimensions_cm:
            xml_parts.append('<dimensions>')
            xml_parts.append(f'<length>{dimensions_cm.get("length", 25):.1f}</length>')
            xml_parts.append(f'<width>{dimensions_cm.get("width", 18):.1f}</width>')
            xml_parts.append(f'<height>{dimensions_cm.get("height", 5):.1f}</height>')
            xml_parts.append('</dimensions>')

        xml_parts.append('</parcel-characteristics>')

        # Destination
        xml_parts.append('<destination>')
        if is_domestic:
            clean_postal = destination_postal.replace(" ", "").upper()
            xml_parts.append(f'<domestic><postal-code>{clean_postal}</postal-code></domestic>')
        elif is_usa:
            xml_parts.append(f'<united-states><zip-code>{destination_postal}</zip-code></united-states>')
        else:
            xml_parts.append(f'<international><country-code>{destination_country.upper()}</country-code></international>')
        xml_parts.append('</destination>')

        xml_parts.append('</mailing-scenario>')

        xml_body = '\n'.join(xml_parts)

        try:
            response = requests.post(
                f"{self.base_url}/rs/ship/price",
                data=xml_body.encode('utf-8'),
                headers={
                    "Content-Type": "application/vnd.cpc.ship.rate-v4+xml",
                    "Accept": "application/vnd.cpc.ship.rate-v4+xml",
                    "Authorization": self._get_auth_header()
                },
                timeout=30
            )

            if response.status_code == 200:
                return self._parse_rate_response(response.content)
            else:
                error_msg = response.text[:500]
                print(f"Canada Post Rating error: {response.status_code} - {error_msg}")
                return {"success": False, "error": f"Canada Post API error: {response.status_code}"}

        except Exception as e:
            print(f"Canada Post Rating exception: {e}")
            return {"success": False, "error": str(e)}

    def _parse_rate_response(self, xml_content: bytes) -> Dict[str, Any]:
        """Parse Canada Post rate response XML."""
        try:
            root = ET.fromstring(xml_content)
            ns = {'cp': 'http://www.canadapost.ca/ws/ship/rate-v4'}

            rates = []
            for quote in root.findall('.//cp:price-quote', ns):
                service_code = quote.find('cp:service-code', ns)
                service_name = quote.find('cp:service-name', ns)
                price_details = quote.find('cp:price-details', ns)
                service_standard = quote.find('cp:service-standard', ns)

                if price_details is not None:
                    due = price_details.find('cp:due', ns)

                    delivery_days = None
                    if service_standard is not None:
                        days_elem = service_standard.find('cp:expected-transit-time', ns)
                        if days_elem is not None and days_elem.text:
                            try:
                                delivery_days = int(days_elem.text)
                            except:
                                pass

                    rates.append({
                        "service_code": service_code.text if service_code is not None else "",
                        "service_name": self._get_service_name(
                            service_code.text if service_code is not None else "",
                            service_name.text if service_name is not None else ""
                        ),
                        "total_charge": float(due.text) if due is not None and due.text else 0,
                        "currency": "CAD",
                        "delivery_days": delivery_days
                    })

            # Sort by price
            rates.sort(key=lambda x: x["total_charge"])

            return {"success": True, "rates": rates}

        except ET.ParseError as e:
            print(f"Failed to parse Canada Post rate XML: {e}")
            return {"success": False, "error": f"XML parse error: {e}"}
        except Exception as e:
            print(f"Error parsing Canada Post rate response: {e}")
            return {"success": False, "error": str(e)}

    def _get_service_name(self, code: str, name: str = "") -> str:
        """Map Canada Post service codes to friendly names."""
        services = {
            # Domestic
            "DOM.RP": "Regular Parcel",
            "DOM.EP": "Expedited Parcel",
            "DOM.XP": "Xpresspost",
            "DOM.PC": "Priority",
            # USA
            "USA.EP": "Expedited Parcel USA",
            "USA.PW.ENV": "Priority Worldwide Envelope USA",
            "USA.PW.PAK": "Priority Worldwide pak USA",
            "USA.PW.PARCEL": "Priority Worldwide Parcel USA",
            "USA.SP.AIR": "Small Packet USA Air",
            "USA.TP": "Tracked Packet - USA",
            "USA.XP": "Xpresspost USA",
            # International
            "INT.XP": "Xpresspost International",
            "INT.IP.AIR": "International Parcel Air",
            "INT.IP.SURF": "International Parcel Surface",
            "INT.PW.ENV": "Priority Worldwide Envelope Int'l",
            "INT.PW.PAK": "Priority Worldwide pak Int'l",
            "INT.PW.PARCEL": "Priority Worldwide parcel Int'l",
            "INT.SP.AIR": "Small Packet International Air",
            "INT.SP.SURF": "Small Packet International Surface",
            "INT.TP": "Tracked Packet - International"
        }
        return services.get(code, name or code)


# Singleton instances
_canadapost_api = None
_canadapost_shipping_api = None


def get_canadapost_api() -> CanadaPostAPI:
    """Get or create singleton Canada Post Tracking API instance."""
    global _canadapost_api
    if _canadapost_api is None:
        _canadapost_api = CanadaPostAPI()
    return _canadapost_api


def get_canadapost_shipping_api() -> CanadaPostShippingAPI:
    """Get or create singleton Canada Post Shipping/Rating API instance."""
    global _canadapost_shipping_api
    if _canadapost_shipping_api is None:
        _canadapost_shipping_api = CanadaPostShippingAPI()
    return _canadapost_shipping_api
