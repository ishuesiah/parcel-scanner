#!/usr/bin/env python3
"""
Backfill script to detect and split concatenated tracking numbers.

Finds existing scans that have multiple tracking numbers stuck together
(e.g., 1ZAC508867380623021ZAC50882034286504) and splits them into separate
scan records.
"""

import os
import sys
import mysql.connector
from datetime import datetime
from dotenv import load_dotenv

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tracking_utils import split_concatenated_tracking_numbers, detect_carrier

# Load environment variables
load_dotenv()


def get_mysql_connection():
    """Create MySQL database connection."""
    return mysql.connector.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "parcel_scanner"),
        port=int(os.environ.get("DB_PORT", 3306))
    )


def find_concatenated_scans(conn):
    """
    Find scans that might have concatenated tracking numbers.

    Looks for tracking numbers with suspicious lengths:
    - 36 chars (two UPS numbers)
    - 32 chars (two Canada Post numbers)
    - 24 chars (two FedEx or Purolator numbers)

    Returns:
        List of scan records that might need splitting
    """
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT id, tracking_number, carrier, order_number, customer_name,
               customer_email, batch_id, scan_date, status, order_id,
               shipstation_batch_number
        FROM scans
        WHERE (
            LENGTH(tracking_number) = 36 OR   -- Two UPS
            LENGTH(tracking_number) = 32 OR   -- Two Canada Post
            LENGTH(tracking_number) = 24      -- Two FedEx/Purolator
        )
        AND status NOT LIKE '%Split%'  -- Don't re-process already split scans
        ORDER BY scan_date DESC
    """

    cursor.execute(query)
    scans = cursor.fetchall()
    cursor.close()

    return scans


def split_and_create_scans(conn, original_scan):
    """
    Split a concatenated scan and create new records for each tracking number.

    Args:
        conn: MySQL connection
        original_scan: Original scan record dict

    Returns:
        Number of new scans created (0 if no split needed)
    """
    tracking_number = original_scan['tracking_number']

    # Check if this should be split
    split_numbers = split_concatenated_tracking_numbers(tracking_number)

    if len(split_numbers) <= 1:
        # No split needed
        return 0

    print(f"")
    print(f"🔍 SPLIT DETECTED:")
    print(f"   Original scan ID: {original_scan['id']}")
    print(f"   Original tracking: {tracking_number}")
    print(f"   Batch ID: {original_scan['batch_id']}")
    print(f"   Split into {len(split_numbers)} tracking numbers:")

    cursor = conn.cursor()

    # Create new scan records for each split tracking number
    created_count = 0
    for i, individual_tracking in enumerate(split_numbers, 1):
        detected_carrier = detect_carrier(individual_tracking)

        print(f"   {i}. {individual_tracking} ({detected_carrier})")

        # Insert new scan with same metadata as original
        cursor.execute(
            """
            INSERT INTO scans
              (tracking_number, carrier, order_number, customer_name,
               scan_date, status, order_id, customer_email, batch_id, shipstation_batch_number)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                individual_tracking,
                detected_carrier,  # Use detected carrier
                "Processing...",  # Will be filled by background process
                "Looking up...",  # Will be filled by background process
                original_scan['scan_date'],  # Keep original scan date
                "Split from concatenated scan",  # Mark as split
                "",  # Empty order_id, will be filled
                "",  # Empty customer_email, will be filled
                original_scan['batch_id'],
                ""   # Empty shipstation_batch_number
            )
        )
        created_count += 1

    # Update the original scan to mark it as "Split"
    cursor.execute(
        """
        UPDATE scans
        SET status = %s,
            order_number = %s
        WHERE id = %s
        """,
        (
            f"Split into {len(split_numbers)} scans",
            f"SPLIT ({len(split_numbers)})",
            original_scan['id']
        )
    )

    conn.commit()
    cursor.close()

    print(f"   ✅ Created {created_count} new scan records")
    print(f"   ✅ Marked original scan #{original_scan['id']} as split")

    return created_count


def backfill_split_tracking_numbers(dry_run=False):
    """
    Main backfill function.

    Args:
        dry_run: If True, only report what would be done without making changes
    """
    print("=" * 70)
    print("BACKFILL: SPLIT CONCATENATED TRACKING NUMBERS")
    print("=" * 70)

    if dry_run:
        print("🔍 DRY RUN MODE - No changes will be made to the database")
        print("")

    conn = get_mysql_connection()

    try:
        # Find scans that might need splitting
        print("Searching for concatenated tracking numbers...")
        scans = find_concatenated_scans(conn)

        print(f"Found {len(scans)} scans with suspicious lengths")
        print("")

        if len(scans) == 0:
            print("✅ No concatenated tracking numbers found!")
            return

        # Process each scan
        total_created = 0
        total_split = 0

        for scan in scans:
            if dry_run:
                # Just check if it would be split
                split_numbers = split_concatenated_tracking_numbers(scan['tracking_number'])
                if len(split_numbers) > 1:
                    print(f"[DRY RUN] Would split scan #{scan['id']}: {scan['tracking_number']}")
                    print(f"          Into {len(split_numbers)} scans: {', '.join(split_numbers)}")
                    total_split += 1
                    total_created += len(split_numbers)
            else:
                # Actually split and create new scans
                created = split_and_create_scans(conn, scan)
                if created > 0:
                    total_split += 1
                    total_created += created

        print("")
        print("=" * 70)
        print("BACKFILL COMPLETE")
        print("=" * 70)

        if dry_run:
            print(f"Would split {total_split} scans into {total_created} new scans")
            print("")
            print("Run without --dry-run to apply changes")
        else:
            print(f"Split {total_split} scans into {total_created} new scans")
            print("")
            print("✅ New scans have been created with 'Processing...' status")
            print("   Run backfill_emails.py to fetch customer emails for these scans")

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description='Find and split concatenated tracking numbers in existing scans'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without making changes'
    )

    args = parser.parse_args()

    try:
        backfill_split_tracking_numbers(dry_run=args.dry_run)
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
