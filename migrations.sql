-- Database migrations for notification feature
-- Run these SQL commands on your MySQL database

-- Add customer_email column to scans table
ALTER TABLE scans
ADD COLUMN customer_email VARCHAR(255) DEFAULT '' AFTER order_id;

-- Add status column to batches table
ALTER TABLE batches
ADD COLUMN status VARCHAR(20) DEFAULT 'in_progress' AFTER carrier;

-- Add notified_at timestamp to batches table
ALTER TABLE batches
ADD COLUMN notified_at DATETIME NULL AFTER status;

-- Create notifications table to track which orders have been notified
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
