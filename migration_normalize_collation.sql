-- Migration: Normalize collations for better JOIN performance
-- This fixes "Illegal mix of collations" errors and enables index usage in JOINs
-- Run this once on your database

-- Normalize scans table columns to utf8mb4_unicode_ci
ALTER TABLE scans
MODIFY COLUMN tracking_number VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Add index on tracking_number if not exists (needed for JOIN performance)
-- Note: This may error if index already exists - that's OK
CREATE INDEX idx_tracking ON scans(tracking_number);

-- Normalize shipments_cache table
ALTER TABLE shipments_cache
MODIFY COLUMN tracking_number VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
MODIFY COLUMN order_number VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Normalize tracking_status_cache table
ALTER TABLE tracking_status_cache
MODIFY COLUMN tracking_number VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL;

-- Normalize cancelled_orders table (should already be correct, but just in case)
ALTER TABLE cancelled_orders
MODIFY COLUMN order_number VARCHAR(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL;

-- Add composite index for common query pattern (ship_date filtering)
CREATE INDEX idx_ship_date_tracking ON shipments_cache(ship_date, tracking_number);

-- Verify collations match (run this query to check)
SELECT
    TABLE_NAME,
    COLUMN_NAME,
    COLLATION_NAME
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND COLUMN_NAME IN ('tracking_number', 'order_number')
  AND TABLE_NAME IN ('scans', 'shipments_cache', 'tracking_status_cache', 'cancelled_orders')
ORDER BY TABLE_NAME, COLUMN_NAME;
