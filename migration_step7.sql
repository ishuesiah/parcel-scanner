-- STEP 7: Create item_location_rules table
-- Stores rules for matching items to warehouse locations
CREATE TABLE IF NOT EXISTS item_location_rules (
    id INT AUTO_INCREMENT PRIMARY KEY,
    aisle VARCHAR(50) NOT NULL,
    shelf VARCHAR(50) NOT NULL,
    rule_type ENUM('sku', 'keyword') NOT NULL,
    rule_value VARCHAR(255) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_rule_type_value (rule_type, rule_value),
    INDEX idx_location (aisle, shelf)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
