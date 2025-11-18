-- Migration: Add cancelled_orders table
-- This table tracks orders that have been cancelled and should not be shipped

CREATE TABLE IF NOT EXISTS cancelled_orders (
    id INT AUTO_INCREMENT PRIMARY KEY,
    order_number VARCHAR(100) NOT NULL,
    tracking_number VARCHAR(100),
    reason VARCHAR(255) DEFAULT 'Order cancelled',
    cancelled_by VARCHAR(100) DEFAULT 'Customer Service',
    cancelled_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,
    UNIQUE KEY unique_order (order_number),
    INDEX idx_tracking (tracking_number),
    INDEX idx_cancelled_at (cancelled_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
