#!/usr/bin/env python3
"""
Migration script: Kinsta MySQL -> Neon PostgreSQL
Migrates batches, scans, and notifications tables.

Usage:
    python migrate_to_neon.py

Or set environment variables:
    MYSQL_URL=mysql://user:pass@host:port/db
    NEON_URL=postgresql://user:pass@host/db
"""

import pymysql
import psycopg2
from psycopg2.extras import execute_values
import os
from datetime import datetime

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE CREDENTIALS
# ══════════════════════════════════════════════════════════════════════════════

MYSQL_CONFIG = {
    "host": "northamerica-northeast1-001.proxy.kinsta.app",
    "port": 30603,
    "user": "hemlockandoak",
    "password": "oH2=bU8=pW6-zB9+dL7_",
    "database": "parcel-scanner",
}

NEON_URL = "postgresql://neondb_owner:npg_GxL0bWvQsJT7@ep-crimson-wind-af1wonxm-pooler.c-2.us-west-2.aws.neon.tech/neondb?sslmode=require"

# ══════════════════════════════════════════════════════════════════════════════
# POSTGRESQL TABLE SCHEMAS (converted from MySQL)
# ══════════════════════════════════════════════════════════════════════════════

POSTGRES_SCHEMAS = {
    "batches": """
        CREATE TABLE IF NOT EXISTS batches (
            id SERIAL PRIMARY KEY,
            carrier VARCHAR(50),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tracking_numbers TEXT,
            status VARCHAR(20) DEFAULT 'in_progress',
            notified_at TIMESTAMP NULL,
            notes TEXT
        )
    """,

    "scans": """
        CREATE TABLE IF NOT EXISTS scans (
            id SERIAL PRIMARY KEY,
            tracking_number VARCHAR(255),
            carrier VARCHAR(50),
            order_number VARCHAR(255),
            customer_name VARCHAR(255),
            scan_date TIMESTAMP,
            status VARCHAR(50),
            order_id VARCHAR(255),
            customer_email VARCHAR(255) DEFAULT '',
            batch_id INTEGER REFERENCES batches(id),
            shipstation_batch_number VARCHAR(255) DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_scans_tracking ON scans(tracking_number);
        CREATE INDEX IF NOT EXISTS idx_scans_batch_id ON scans(batch_id);
        CREATE INDEX IF NOT EXISTS idx_scans_order_number ON scans(order_number);
    """,

    "notifications": """
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            batch_id INTEGER NOT NULL,
            order_number VARCHAR(100) NOT NULL,
            customer_email VARCHAR(255) NOT NULL,
            tracking_number VARCHAR(100) NOT NULL,
            notified_at TIMESTAMP NOT NULL,
            success BOOLEAN DEFAULT TRUE,
            error_message TEXT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_notifications_order ON notifications(order_number);
        CREATE INDEX IF NOT EXISTS idx_notifications_batch ON notifications(batch_id);
        CREATE UNIQUE INDEX IF NOT EXISTS unique_order_notification ON notifications(order_number, batch_id);
    """,

    "shipments_cache": """
        CREATE TABLE IF NOT EXISTS shipments_cache (
            id SERIAL PRIMARY KEY,
            tracking_number VARCHAR(255) NOT NULL,
            order_number VARCHAR(255),
            customer_name VARCHAR(255),
            carrier_code VARCHAR(50),
            ship_date DATE,
            shipstation_batch_number VARCHAR(255),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_shipments_tracking ON shipments_cache(tracking_number);
        CREATE INDEX IF NOT EXISTS idx_shipments_ship_date ON shipments_cache(ship_date);
        CREATE INDEX IF NOT EXISTS idx_shipments_order ON shipments_cache(order_number);
        CREATE UNIQUE INDEX IF NOT EXISTS unique_shipments_tracking ON shipments_cache(tracking_number);
    """,

    "tracking_status_cache": """
        CREATE TABLE IF NOT EXISTS tracking_status_cache (
            id SERIAL PRIMARY KEY,
            tracking_number VARCHAR(255) NOT NULL,
            carrier VARCHAR(50) DEFAULT 'UPS',
            status VARCHAR(50),
            status_description VARCHAR(500),
            estimated_delivery VARCHAR(255),
            last_location VARCHAR(255),
            last_activity_date VARCHAR(50),
            is_delivered BOOLEAN DEFAULT FALSE,
            raw_status_code VARCHAR(50),
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE UNIQUE INDEX IF NOT EXISTS unique_tracking_status ON tracking_status_cache(tracking_number);
        CREATE INDEX IF NOT EXISTS idx_tracking_status ON tracking_status_cache(status);
        CREATE INDEX IF NOT EXISTS idx_tracking_delivered ON tracking_status_cache(is_delivered);
    """,

    "cancelled_orders": """
        CREATE TABLE IF NOT EXISTS cancelled_orders (
            id SERIAL PRIMARY KEY,
            order_number VARCHAR(100) NOT NULL,
            tracking_number VARCHAR(100),
            reason VARCHAR(255) DEFAULT 'Order cancelled',
            cancelled_by VARCHAR(100) DEFAULT 'Customer Service',
            cancelled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS unique_cancelled_order ON cancelled_orders(order_number);
        CREATE INDEX IF NOT EXISTS idx_cancelled_tracking ON cancelled_orders(tracking_number);
    """,

    "order_verifications": """
        CREATE TABLE IF NOT EXISTS order_verifications (
            id SERIAL PRIMARY KEY,
            order_number VARCHAR(100) NOT NULL,
            tracking_number VARCHAR(100) NULL,
            shopify_order_id VARCHAR(100) NULL,
            verified_at TIMESTAMP NOT NULL,
            items_checked INTEGER DEFAULT 0,
            total_items INTEGER DEFAULT 0,
            notes TEXT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_verifications_order ON order_verifications(order_number);
        CREATE INDEX IF NOT EXISTS idx_verifications_tracking ON order_verifications(tracking_number);
    """,

    "item_location_rules": """
        CREATE TABLE IF NOT EXISTS item_location_rules (
            id SERIAL PRIMARY KEY,
            aisle VARCHAR(50) NOT NULL,
            shelf VARCHAR(50) NOT NULL,
            rule_type VARCHAR(10) NOT NULL CHECK (rule_type IN ('sku', 'keyword')),
            rule_value VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_location_rule ON item_location_rules(rule_type, rule_value);
        CREATE INDEX IF NOT EXISTS idx_location_aisle ON item_location_rules(aisle, shelf);
    """
}

def connect_mysql():
    """Connect to Kinsta MySQL."""
    print("📦 Connecting to Kinsta MySQL...")
    conn = pymysql.connect(
        **MYSQL_CONFIG,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=30,
        read_timeout=60
    )
    print("✓ Connected to MySQL")
    return conn

def connect_postgres():
    """Connect to Neon PostgreSQL."""
    print("🐘 Connecting to Neon PostgreSQL...")
    conn = psycopg2.connect(NEON_URL)
    conn.autocommit = False
    print("✓ Connected to PostgreSQL")
    return conn

def get_mysql_tables(mysql_conn):
    """Get list of tables in MySQL."""
    cursor = mysql_conn.cursor()
    cursor.execute("SHOW TABLES")
    tables = [list(row.values())[0] for row in cursor.fetchall()]
    cursor.close()
    return tables

def get_table_count(mysql_conn, table):
    """Get row count for a table."""
    cursor = mysql_conn.cursor()
    cursor.execute(f"SELECT COUNT(*) as cnt FROM `{table}`")
    count = cursor.fetchone()['cnt']
    cursor.close()
    return count

def create_postgres_tables(pg_conn):
    """Create all tables in PostgreSQL."""
    print("\n📋 Creating PostgreSQL tables...")
    cursor = pg_conn.cursor()

    for table, schema in POSTGRES_SCHEMAS.items():
        try:
            cursor.execute(schema)
            print(f"  ✓ Created/verified table: {table}")
        except Exception as e:
            print(f"  ⚠️ Error creating {table}: {e}")

    pg_conn.commit()
    cursor.close()
    print("✓ All tables created")

def migrate_table(mysql_conn, pg_conn, table, batch_size=1000):
    """Migrate a single table from MySQL to PostgreSQL."""
    mysql_cursor = mysql_conn.cursor()
    pg_cursor = pg_conn.cursor()

    # Get total count
    mysql_cursor.execute(f"SELECT COUNT(*) as cnt FROM `{table}`")
    total = mysql_cursor.fetchone()['cnt']

    if total == 0:
        print(f"  ⏭️  {table}: empty, skipping")
        return 0

    print(f"  📤 {table}: migrating {total} rows...")

    # Get column names and types
    mysql_cursor.execute(f"DESCRIBE `{table}`")
    col_info = mysql_cursor.fetchall()
    columns = [col['Field'] for col in col_info]

    # Identify timestamp/datetime columns (need NULL instead of empty string)
    timestamp_cols = set()
    for col in col_info:
        col_type = col['Type'].lower()
        if 'datetime' in col_type or 'timestamp' in col_type or 'date' in col_type:
            timestamp_cols.add(col['Field'])

    # Clear existing data in postgres (fresh import)
    pg_cursor.execute(f"TRUNCATE TABLE {table} CASCADE")
    pg_conn.commit()

    # Migrate in batches
    offset = 0
    migrated = 0

    while offset < total:
        mysql_cursor.execute(f"SELECT * FROM `{table}` LIMIT {batch_size} OFFSET {offset}")
        rows = mysql_cursor.fetchall()

        if not rows:
            break

        # Convert rows to tuples, handling empty strings for timestamps
        values = []
        for row in rows:
            row_values = []
            for col in columns:
                val = row[col]
                # Convert empty strings to None for timestamp columns
                if col in timestamp_cols and val == '':
                    row_values.append(None)
                elif isinstance(val, str) and val == '' and col in timestamp_cols:
                    row_values.append(None)
                elif val is None:
                    row_values.append(None)
                else:
                    row_values.append(val)
            values.append(tuple(row_values))

        # Insert into PostgreSQL
        cols_str = ', '.join(columns)
        placeholders = ', '.join(['%s'] * len(columns))

        try:
            execute_values(
                pg_cursor,
                f"INSERT INTO {table} ({cols_str}) VALUES %s",
                values,
                template=f"({placeholders})"
            )
            pg_conn.commit()
            migrated += len(rows)
        except Exception as e:
            pg_conn.rollback()
            print(f"    ⚠️ Batch error at offset {offset}: {e}")
            # Try one by one
            for val_tuple in values:
                try:
                    pg_cursor.execute(
                        f"INSERT INTO {table} ({cols_str}) VALUES ({placeholders})",
                        val_tuple
                    )
                    pg_conn.commit()
                    migrated += 1
                except Exception as e2:
                    pg_conn.rollback()
                    # Skip with minimal logging
                    pass

        offset += batch_size
        print(f"    ... {min(offset, total)}/{total} rows")

    # Reset sequence to max id
    try:
        pg_cursor.execute(f"SELECT MAX(id) FROM {table}")
        max_id = pg_cursor.fetchone()[0] or 0
        pg_cursor.execute(f"SELECT setval('{table}_id_seq', {max_id + 1}, false)")
        pg_conn.commit()
    except Exception as e:
        pg_conn.rollback()
        print(f"    ⚠️ Could not reset sequence: {e}")

    mysql_cursor.close()
    pg_cursor.close()

    print(f"  ✓ {table}: migrated {migrated} rows")
    return migrated

def main():
    print("=" * 60)
    print("  KINSTA MYSQL → NEON POSTGRESQL MIGRATION")
    print("=" * 60)
    print()

    # Connect to both databases
    mysql_conn = connect_mysql()
    pg_conn = connect_postgres()

    # Show MySQL tables
    print("\n📋 MySQL tables found:")
    tables = get_mysql_tables(mysql_conn)
    for t in tables:
        count = get_table_count(mysql_conn, t)
        print(f"  - {t}: {count} rows")

    # Create PostgreSQL tables
    create_postgres_tables(pg_conn)

    # Migrate each table
    print("\n🚀 Starting migration...")
    tables_to_migrate = ['batches', 'scans', 'notifications']

    # Check for additional tables that exist
    optional_tables = ['shipments_cache', 'tracking_status_cache', 'cancelled_orders',
                       'order_verifications', 'item_location_rules']
    for t in optional_tables:
        if t in tables:
            tables_to_migrate.append(t)

    total_migrated = 0
    for table in tables_to_migrate:
        if table in tables:
            count = migrate_table(mysql_conn, pg_conn, table)
            total_migrated += count
        else:
            print(f"  ⏭️  {table}: not found in MySQL, skipping")

    # Close connections
    mysql_conn.close()
    pg_conn.close()

    print("\n" + "=" * 60)
    print(f"  ✅ MIGRATION COMPLETE!")
    print(f"  Total rows migrated: {total_migrated}")
    print("=" * 60)
    print("\nNext steps:")
    print("1. Update your environment variables to use Neon PostgreSQL")
    print("2. Update web_scanner.py to use psycopg2 instead of pymysql")
    print("3. Test the application")

if __name__ == "__main__":
    main()
