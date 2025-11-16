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
