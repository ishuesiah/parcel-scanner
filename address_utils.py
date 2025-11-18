#!/usr/bin/env python3
"""
Address utility functions for parcel scanner.
Includes PO Box detection and validation.
"""

import re
from typing import Tuple


def is_po_box(address_line: str) -> bool:
    """
    Detect if an address line contains a PO Box.

    Common patterns:
    - PO Box 123
    - P.O. Box 123
    - P O Box 123
    - POB 123
    - Post Office Box 123
    - P.O.B 123
    - Box 123 (sometimes)

    Args:
        address_line: Address line to check

    Returns:
        True if PO Box detected, False otherwise
    """
    if not address_line:
        return False

    # Normalize: uppercase and remove extra spaces
    normalized = re.sub(r'\s+', ' ', address_line.upper().strip())

    # PO Box patterns
    po_box_patterns = [
        r'\bP\.?\s*O\.?\s+BOX\b',  # P.O. Box, PO Box, P O Box
        r'\bPO\s+BOX\b',            # PO BOX
        r'\bP\.O\.B\.?\b',          # P.O.B, P.O.B.
        r'\bPOB\b',                 # POB
        r'\bPOST\s+OFFICE\s+BOX\b', # POST OFFICE BOX
        r'\bBOX\s+\d+\b',           # Box 123 (only if followed by number)
    ]

    for pattern in po_box_patterns:
        if re.search(pattern, normalized):
            return True

    return False


def check_po_box_compatibility(address_str: str, carrier: str) -> Tuple[bool, str]:
    """
    Check if a PO Box address is compatible with the selected carrier.

    Canada Post can deliver to PO Boxes.
    UPS, DHL, FedEx, Purolator cannot deliver to PO Boxes.

    Args:
        address_str: Full address string to check
        carrier: Carrier name (UPS, Canada Post, DHL, etc.)

    Returns:
        Tuple of (is_valid, error_message)
        - is_valid: True if compatible, False if incompatible
        - error_message: Error message if incompatible, empty string if valid
    """
    if not address_str:
        return True, ""

    # Check if address contains PO Box
    has_po_box = is_po_box(address_str)

    if not has_po_box:
        # No PO Box, all carriers are fine
        return True, ""

    # PO Box detected - check carrier compatibility
    carrier_upper = carrier.upper()

    # Canada Post can deliver to PO Boxes
    if "CANADA" in carrier_upper or "POST" in carrier_upper:
        return True, ""

    # USPS can also deliver to PO Boxes
    if "USPS" in carrier_upper:
        return True, ""

    # All other carriers (UPS, DHL, FedEx, Purolator) cannot
    return False, f"🚫 PO BOX DETECTED - {carrier} cannot deliver to PO Box addresses! Use Canada Post instead."


def extract_address_lines(shipment_data: dict) -> list:
    """
    Extract all address lines from shipment data to check for PO Box.

    Args:
        shipment_data: Shipment data from ShipStation or Shopify

    Returns:
        List of address lines to check
    """
    address_lines = []

    # ShipStation format
    if "shipTo" in shipment_data:
        ship_to = shipment_data["shipTo"]
        if isinstance(ship_to, dict):
            address_lines.append(ship_to.get("street1", ""))
            address_lines.append(ship_to.get("street2", ""))
            address_lines.append(ship_to.get("street3", ""))

    # Shopify format
    if "shipping_address" in shipment_data:
        shipping_addr = shipment_data["shipping_address"]
        if isinstance(shipping_addr, dict):
            address_lines.append(shipping_addr.get("address1", ""))
            address_lines.append(shipping_addr.get("address2", ""))

    # Filter out empty lines
    return [line for line in address_lines if line]


# ═══════════════════════════════════════════════════════════════════
# Testing
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("PO BOX DETECTION TESTS")
    print("=" * 60)

    test_addresses = [
        ("123 Main Street", False),
        ("PO Box 456", True),
        ("P.O. Box 789", True),
        ("P O Box 123", True),
        ("POB 456", True),
        ("Post Office Box 789", True),
        ("Box 123 Main St", True),  # Ambiguous, but detected
        ("123 Boxwood Ave", False),  # Contains "Box" but not a PO Box
        ("555 P.O.B. 123", True),
        ("", False),
    ]

    print("\n1. PO Box Detection:")
    for address, expected in test_addresses:
        result = is_po_box(address)
        status = "✓" if result == expected else "✗"
        print(f"  {status} '{address}' -> {result} (expected {expected})")

    print("\n2. Carrier Compatibility:")
    po_box_addr = "PO Box 456"

    carriers_test = [
        ("Canada Post", True),
        ("UPS", False),
        ("DHL", False),
        ("FedEx", False),
        ("Purolator", False),
        ("USPS", True),
    ]

    for carrier, should_be_valid in carriers_test:
        is_valid, error = check_po_box_compatibility(po_box_addr, carrier)
        status = "✓" if is_valid == should_be_valid else "✗"
        print(f"  {status} {carrier}: valid={is_valid}")
        if error:
            print(f"      Error: {error}")

    print("\n" + "=" * 60)
