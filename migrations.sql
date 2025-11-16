-- ===================================================================
-- PARCEL SCANNER DATABASE MIGRATIONS
-- ===================================================================
-- Run these SQL commands on your MySQL database
--
-- IMPORTANT: Run these commands ONE AT A TIME and check for errors
-- If a column already exists, you'll get an error - that's okay, skip it
-- ===================================================================

-- STEP 1: Add customer_email column to scans table
-- This stores customer email for Klaviyo notifications
ALTER TABLE scans
ADD COLUMN customer_email VARCHAR(255) DEFAULT '' AFTER order_id;

-- STEP 2: Add status column to batches table
-- Tracks batch lifecycle: 'in_progress' → 'recorded' → 'notified'
ALTER TABLE batches
ADD COLUMN status VARCHAR(20) DEFAULT 'in_progress' AFTER carrier;

-- STEP 3: Add notified_at timestamp to batches table
-- Records when Klaviyo notifications were sent
ALTER TABLE batches
ADD COLUMN notified_at DATETIME NULL AFTER status;

-- STEP 4: Add notes column to batches table
-- Allows users to add notes/comments for each batch
ALTER TABLE batches
ADD COLUMN notes TEXT NULL AFTER notified_at;

-- STEP 5: Create notifications table
-- Tracks which orders have been notified to prevent duplicates
CREATE TABLE IF NOT EXISTS notifications (
    id INT AUTO_INCREMENT PRIMARY KEY,
    batch_id INT NOT NULL,
    order_number VARCHAR(100) NOT NULL,
    customer_email VARCHAR(255) NOT NULL,
    tracking_number VARCHAR(100) NOT NULL,
    notified_at DATETIME NOT NULL,
    success BOOLEAN DEFAULT TRUE,
    error_message TEXT NULL,
    INDEX idx_order_number (order_number),
    INDEX idx_batch_id (batch_id),
    UNIQUE KEY unique_order_notification (order_number, batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ===================================================================
-- VERIFICATION: Run this to check if all columns exist
-- ===================================================================
SELECT
    'scans.customer_email' as column_name,
    COUNT(*) as exists_count
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'scans'
  AND COLUMN_NAME = 'customer_email'
UNION ALL
SELECT
    'batches.status',
    COUNT(*)
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'batches'
  AND COLUMN_NAME = 'status'
UNION ALL
SELECT
    'batches.notified_at',
    COUNT(*)
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'batches'
  AND COLUMN_NAME = 'notified_at'
UNION ALL
SELECT
    'batches.notes',
    COUNT(*)
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'batches'
  AND COLUMN_NAME = 'notes'
UNION ALL
SELECT
    'notifications table',
    COUNT(*)
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'notifications';

-- All rows should show '1' in exists_count column if migrations are complete

-- STEP 6: Create order_verifications table
-- Tracks order verification/pick-and-pack completion
CREATE TABLE IF NOT EXISTS order_verifications (
    id INT AUTO_INCREMENT PRIMARY KEY,
    order_number VARCHAR(100) NOT NULL,
    tracking_number VARCHAR(100) NULL,
    shopify_order_id VARCHAR(100) NULL,
    verified_at DATETIME NOT NULL,
    items_checked INT DEFAULT 0,
    total_items INT DEFAULT 0,
    notes TEXT NULL,
    INDEX idx_order_number (order_number),
    INDEX idx_tracking_number (tracking_number),
    INDEX idx_verified_at (verified_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
