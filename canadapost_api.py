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


# Singleton instance
_canadapost_api = None

def get_canadapost_api() -> CanadaPostAPI:
    """Get or create singleton Canada Post API instance."""
    global _canadapost_api
    if _canadapost_api is None:
        _canadapost_api = CanadaPostAPI()
    return _canadapost_api
