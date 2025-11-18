#!/usr/bin/env python3
"""
Utility functions for tracking number detection and splitting.
Handles cases where multiple tracking numbers are accidentally concatenated.
"""

import re
from typing import List, Tuple


def detect_carrier(tracking_number: str) -> str:
    """
    Detect carrier based on tracking number format.

    Args:
        tracking_number: The tracking number to analyze

    Returns:
        Carrier name or "Unknown"
    """
    tracking = tracking_number.strip().upper()

    # UPS: 1Z + 16 alphanumeric = 18 chars
    if tracking.startswith("1Z") and len(tracking) == 18:
        return "UPS"

    # Canada Post: Typically starts with various prefixes, 16 digits
    if tracking.startswith("2016") or (len(tracking) == 16 and tracking.isdigit()):
        return "Canada Post"

    # Purolator: 12 digits
    if len(tracking) == 12 and tracking.isdigit():
        return "Purolator"

    # DHL: 10-11 digits
    if len(tracking) in [10, 11] and tracking.isdigit():
        return "DHL"

    # FedEx: 12 or 15 digits
    if len(tracking) in [12, 15] and tracking.isdigit():
        return "FedEx"

    # USPS: Various formats, typically 20-22 chars
    if tracking.startswith("LA") or (len(tracking) in range(20, 31) and tracking.isalnum()):
        return "USPS"

    return "Unknown"


def split_concatenated_tracking_numbers(tracking_number: str) -> List[str]:
    """
    Detect and split concatenated tracking numbers.

    Handles common cases like:
    - Two UPS numbers stuck together (1Z...1Z... = 36 chars)
    - Two Canada Post numbers (32 digits)
    - Other patterns

    Args:
        tracking_number: Potentially concatenated tracking number

    Returns:
        List of individual tracking numbers (single item if no split detected)
    """
    tracking = tracking_number.strip()

    # Skip if too short to be concatenated
    if len(tracking) < 18:
        return [tracking]

    # ═══════════════════════════════════════════════════════════════
    # UPS: Two 18-character numbers starting with 1Z
    # ═══════════════════════════════════════════════════════════════
    if len(tracking) == 36 and tracking.startswith("1Z"):
        # Check if there's another "1Z" at position 18
        if tracking[18:20] == "1Z":
            first = tracking[:18]
            second = tracking[18:]

            # Validate both look like UPS tracking numbers
            if _is_valid_ups(first) and _is_valid_ups(second):
                print(f"🔍 SPLIT DETECTED: UPS concatenation")
                print(f"   Original: {tracking}")
                print(f"   Split 1:  {first}")
                print(f"   Split 2:  {second}")
                return [first, second]

    # ═══════════════════════════════════════════════════════════════
    # Canada Post: Two 16-digit numbers
    # ═══════════════════════════════════════════════════════════════
    if len(tracking) == 32 and tracking.isdigit():
        first = tracking[:16]
        second = tracking[16:]

        # Both should be valid Canada Post format
        if detect_carrier(first) == "Canada Post" and detect_carrier(second) == "Canada Post":
            print(f"🔍 SPLIT DETECTED: Canada Post concatenation")
            print(f"   Original: {tracking}")
            print(f"   Split 1:  {first}")
            print(f"   Split 2:  {second}")
            return [first, second]

    # ═══════════════════════════════════════════════════════════════
    # FedEx: Two 12-digit numbers (24 total)
    # ═══════════════════════════════════════════════════════════════
    if len(tracking) == 24 and tracking.isdigit():
        first = tracking[:12]
        second = tracking[12:]

        if detect_carrier(first) == "FedEx" and detect_carrier(second) == "FedEx":
            print(f"🔍 SPLIT DETECTED: FedEx concatenation")
            print(f"   Original: {tracking}")
            print(f"   Split 1:  {first}")
            print(f"   Split 2:  {second}")
            return [first, second]

    # ═══════════════════════════════════════════════════════════════
    # Purolator: Two 12-digit numbers (24 total)
    # But only if it doesn't match FedEx pattern
    # ═══════════════════════════════════════════════════════════════
    if len(tracking) == 24 and tracking.isdigit():
        # This overlaps with FedEx, so we check for specific Purolator patterns
        # For now, default to FedEx detection above
        pass

    # ═══════════════════════════════════════════════════════════════
    # Generic: Try to find multiple 1Z patterns (UPS)
    # ═══════════════════════════════════════════════════════════════
    if "1Z" in tracking:
        # Find all positions where "1Z" occurs
        positions = [m.start() for m in re.finditer(r'1Z', tracking)]

        if len(positions) >= 2:
            # Try to extract tracking numbers at each position
            potential_numbers = []
            for pos in positions:
                # UPS tracking is 18 chars starting from this position
                if pos + 18 <= len(tracking):
                    candidate = tracking[pos:pos + 18]
                    if _is_valid_ups(candidate):
                        potential_numbers.append((pos, candidate))

            # If we found 2+ valid UPS numbers with no overlap
            if len(potential_numbers) >= 2:
                # Check they don't overlap
                non_overlapping = []
                last_end = 0
                for pos, num in sorted(potential_numbers):
                    if pos >= last_end:
                        non_overlapping.append(num)
                        last_end = pos + len(num)

                if len(non_overlapping) >= 2:
                    print(f"🔍 SPLIT DETECTED: Multiple UPS numbers found")
                    print(f"   Original: {tracking}")
                    for i, num in enumerate(non_overlapping, 1):
                        print(f"   Split {i}:  {num}")
                    return non_overlapping

    # No split detected
    return [tracking]


def _is_valid_ups(tracking: str) -> bool:
    """
    Validate UPS tracking number format.

    UPS tracking format:
    - Starts with "1Z"
    - Followed by 6-char shipper number (alphanumeric)
    - Followed by 2-char service code
    - Followed by 8-char package ID
    - Total: 18 characters

    Args:
        tracking: Tracking number to validate

    Returns:
        True if valid UPS format
    """
    if not tracking or len(tracking) != 18:
        return False

    if not tracking.startswith("1Z"):
        return False

    # Rest should be alphanumeric
    if not tracking[2:].isalnum():
        return False

    return True


def should_split_scan(tracking_number: str) -> Tuple[bool, List[str]]:
    """
    Check if a tracking number should be split and return the splits.

    Args:
        tracking_number: The tracking number to check

    Returns:
        Tuple of (should_split: bool, split_numbers: List[str])
    """
    splits = split_concatenated_tracking_numbers(tracking_number)
    return len(splits) > 1, splits


# ═══════════════════════════════════════════════════════════════════
# Testing
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("TRACKING NUMBER SPLIT DETECTION TESTS")
    print("=" * 60)

    test_cases = [
        # UPS concatenated
        "1ZAC508867380623021ZAC50882034286504",
        # Single UPS
        "1ZAC50886738062302",
        # Canada Post concatenated
        "20169876543210982016123456789012",
        # Single Canada Post
        "2016987654321098",
        # Not concatenated
        "1234567890",
    ]

    for test in test_cases:
        print(f"\nTest: {test}")
        result = split_concatenated_tracking_numbers(test)
        if len(result) > 1:
            print(f"  ✅ SPLIT: {len(result)} numbers detected")
            for i, num in enumerate(result, 1):
                print(f"     {i}. {num} ({detect_carrier(num)})")
        else:
            print(f"  ℹ️  No split: {result[0]} ({detect_carrier(result[0])})")
