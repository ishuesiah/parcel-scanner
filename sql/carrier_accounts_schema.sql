-- ============================================================================
-- Carrier Accounts Schema - For Direct Carrier Integration
-- Stores credentials for UPS, Canada Post, etc. to create labels directly
-- ============================================================================

-- ============================================================================
-- Table: carrier_accounts
-- Purpose: Store carrier API credentials (encrypted) for label generation
-- ============================================================================
CREATE TABLE IF NOT EXISTS carrier_accounts (
    id SERIAL PRIMARY KEY,
    carrier_code TEXT NOT NULL UNIQUE,           -- 'ups', 'canada_post', 'fedex', etc.
    carrier_name TEXT NOT NULL,                   -- Display name

    -- API Credentials (stored encrypted or as env var references)
    client_id TEXT,                               -- OAuth client ID
    client_secret TEXT,                           -- OAuth client secret (encrypted)
    account_number TEXT,                          -- Carrier account number

    -- Additional carrier-specific fields stored as JSON
    extra_config TEXT,                            -- JSON blob for carrier-specific settings

    -- Shipper/Return address (can override defaults)
    shipper_name TEXT,
    shipper_phone TEXT,
    shipper_address1 TEXT,
    shipper_address2 TEXT,
    shipper_city TEXT,
    shipper_province TEXT,
    shipper_postal_code TEXT,
    shipper_country TEXT DEFAULT 'CA',

    -- Status
    enabled INTEGER DEFAULT 1,                    -- 1 = active, 0 = disabled
    test_mode INTEGER DEFAULT 0,                  -- 1 = use sandbox/test endpoints

    -- Tracking
    last_tested_at TIMESTAMP,                     -- Last successful API test
    last_error TEXT,                              -- Last error message if any

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_carrier_accounts_code ON carrier_accounts(carrier_code);
CREATE INDEX IF NOT EXISTS idx_carrier_accounts_enabled ON carrier_accounts(enabled);

-- ============================================================================
-- Table: shipping_labels
-- Purpose: Store generated shipping labels (PDF data and metadata)
-- ============================================================================
CREATE TABLE IF NOT EXISTS shipping_labels (
    id SERIAL PRIMARY KEY,

    -- Order reference
    order_id INTEGER,                             -- FK to orders.id (nullable)
    order_number TEXT NOT NULL,                   -- Order number for lookup

    -- Carrier info
    carrier_code TEXT NOT NULL,                   -- 'ups', 'canada_post', etc.
    service_code TEXT,                            -- Service type code
    service_name TEXT,                            -- Service display name

    -- Tracking
    tracking_number TEXT,                         -- Tracking number from carrier

    -- Label data
    label_format TEXT DEFAULT 'PDF',              -- PDF, ZPL, PNG, etc.
    label_data BYTEA,                             -- Binary label data (PDF bytes)
    label_url TEXT,                               -- Optional: URL if stored externally

    -- Shipment details
    ship_to_name TEXT,
    ship_to_address TEXT,                         -- JSON blob of full address
    ship_to_country TEXT,

    -- Customs (for international)
    customs_data TEXT,                            -- JSON blob of customs declaration

    -- Costs
    shipping_cost REAL,
    currency TEXT DEFAULT 'CAD',

    -- Package dimensions
    weight_kg REAL,
    length_cm REAL,
    width_cm REAL,
    height_cm REAL,

    -- Status
    status TEXT DEFAULT 'created',                -- created, voided, used
    voided_at TIMESTAMP,
    void_reason TEXT,

    -- Carrier response
    carrier_response TEXT,                        -- Full API response JSON for debugging

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by TEXT                               -- Who created this label
);

CREATE INDEX IF NOT EXISTS idx_shipping_labels_order ON shipping_labels(order_number);
CREATE INDEX IF NOT EXISTS idx_shipping_labels_tracking ON shipping_labels(tracking_number);
CREATE INDEX IF NOT EXISTS idx_shipping_labels_carrier ON shipping_labels(carrier_code);
CREATE INDEX IF NOT EXISTS idx_shipping_labels_created ON shipping_labels(created_at);
CREATE INDEX IF NOT EXISTS idx_shipping_labels_status ON shipping_labels(status);

-- ============================================================================
-- Verification: Check tables were created
-- ============================================================================
-- Run: SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name LIKE '%carrier%' OR table_name LIKE '%label%';
