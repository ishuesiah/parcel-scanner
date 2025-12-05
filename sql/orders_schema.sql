-- ============================================================================
-- Hemlock & Oak Parcel Scanner - Orders Database Schema
-- Run this SQL in the Neon console to create the orders tables
-- ============================================================================

-- ============================================================================
-- Table: orders
-- Purpose: Local cache of Shopify orders for faster scanning and order lookup
-- Syncs from Shopify API every 5 minutes (incremental) with 90-day initial sync
-- ============================================================================
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,

    -- Shopify identifiers
    shopify_order_id TEXT UNIQUE NOT NULL,      -- e.g., "6522182205684"
    order_number TEXT NOT NULL,                  -- e.g., "60430" (display number without #)

    -- Customer info
    customer_name TEXT,
    customer_email TEXT,
    customer_phone TEXT,

    -- Addresses (stored as JSON strings)
    shipping_address TEXT,                       -- JSON blob
    billing_address TEXT,                        -- JSON blob

    -- Order notes
    note TEXT,                                   -- Shopify note field (customer notes)
    note_attributes TEXT,                        -- JSON - custom notes from apps like TEPO

    -- Financial
    total_price REAL,
    subtotal_price REAL,
    total_tax REAL,
    total_shipping REAL,
    currency TEXT DEFAULT 'CAD',
    financial_status TEXT,                       -- paid, refunded, partially_refunded, pending

    -- Fulfillment
    fulfillment_status TEXT,                     -- fulfilled, unfulfilled, partial, null
    tracking_number TEXT,                        -- Primary tracking number for quick lookup

    -- Parcel Scanner specific
    scanned_status INTEGER DEFAULT 0,            -- 0 = not scanned, 1 = scanned
    scanned_at TIMESTAMP,                        -- When it was scanned

    -- Shopify timestamps
    shopify_created_at TIMESTAMP,                -- When order was placed
    shopify_updated_at TIMESTAMP,                -- Last update in Shopify

    -- Sync tracking
    synced_at TIMESTAMP,                         -- Last sync from Shopify API

    -- Cancellation (quick reference, details in cancelled_orders table)
    cancelled_at TIMESTAMP,
    cancel_reason TEXT,

    -- Local timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Essential indexes for performance
CREATE INDEX IF NOT EXISTS idx_orders_order_number ON orders(order_number);
CREATE INDEX IF NOT EXISTS idx_orders_shopify_order_id ON orders(shopify_order_id);
CREATE INDEX IF NOT EXISTS idx_orders_customer_email ON orders(customer_email);
CREATE INDEX IF NOT EXISTS idx_orders_tracking ON orders(tracking_number);
CREATE INDEX IF NOT EXISTS idx_orders_fulfillment ON orders(fulfillment_status);
CREATE INDEX IF NOT EXISTS idx_orders_scanned ON orders(scanned_status);
CREATE INDEX IF NOT EXISTS idx_orders_shopify_updated ON orders(shopify_updated_at);
CREATE INDEX IF NOT EXISTS idx_orders_shopify_created ON orders(shopify_created_at);

-- ============================================================================
-- Table: order_line_items
-- Purpose: Individual items within each order (products ordered)
-- ============================================================================
CREATE TABLE IF NOT EXISTS order_line_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL,                   -- FK to orders.id
    shopify_line_item_id TEXT,                   -- Shopify's line item ID

    -- Product info
    sku TEXT,
    product_id TEXT,                             -- Shopify product ID
    variant_id TEXT,                             -- Shopify variant ID
    product_title TEXT,                          -- e.g., "Weekly & Daily Planner"
    variant_title TEXT,                          -- e.g., "Forest Green / Dated"

    -- Quantities & pricing
    quantity INTEGER DEFAULT 1,
    price REAL,                                  -- Per unit price
    total_discount REAL DEFAULT 0,

    -- Fulfillment tracking
    fulfillable_quantity INTEGER,
    fulfillment_status TEXT,                     -- fulfilled, unfulfilled
    requires_shipping INTEGER DEFAULT 1,         -- 1 = yes, 0 = no (digital products)

    -- For future pick/pack verification
    picked INTEGER DEFAULT 0,                    -- Has warehouse picked it?
    picked_at TIMESTAMP,

    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_line_items_order ON order_line_items(order_id);
CREATE INDEX IF NOT EXISTS idx_line_items_sku ON order_line_items(sku);
CREATE INDEX IF NOT EXISTS idx_line_items_fulfillment ON order_line_items(fulfillment_status);

-- ============================================================================
-- Table: order_line_item_options
-- Purpose: Stores TEPO customization properties for line items
-- In Shopify API these come from line_item.properties[]
-- In ShipStation they're called "item options"
-- ============================================================================
CREATE TABLE IF NOT EXISTS order_line_item_options (
    id SERIAL PRIMARY KEY,
    line_item_id INTEGER NOT NULL,               -- FK to order_line_items.id

    name TEXT NOT NULL,                          -- Option name, e.g., "Start Month", "Personalization"
    value TEXT,                                  -- Option value, e.g., "January 2025", "Sarah"

    FOREIGN KEY (line_item_id) REFERENCES order_line_items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_options_line_item ON order_line_item_options(line_item_id);

-- ============================================================================
-- Table: cancelled_orders (Recreation with enhanced schema)
-- Purpose: Track cancelled orders with full details for audit trail
-- Note: Drop existing empty table and recreate with new schema
-- ============================================================================

-- First drop the existing (empty) table if it exists
DROP TABLE IF EXISTS cancelled_orders;

CREATE TABLE cancelled_orders (
    id SERIAL PRIMARY KEY,

    -- Order reference
    order_id INTEGER,                            -- FK to orders.id (if order exists in local DB)
    shopify_order_id TEXT,                       -- Shopify order ID
    order_number TEXT NOT NULL,                  -- Display number like "60430"
    tracking_number TEXT,                        -- If already shipped

    -- Customer info snapshot (in case order is deleted)
    customer_name TEXT,
    customer_email TEXT,

    -- Cancellation details
    reason TEXT NOT NULL,
    -- Valid values: 'customer_cancelled', 'duplicate_order', 'fraud', 'refund_requested', 'other'
    reason_notes TEXT,                           -- Additional notes, especially if reason is "other"
    cancelled_by TEXT,                           -- Free text: who cancelled (e.g., "Jess", "Tia")

    -- Refund tracking (for future full implementation)
    refund_amount REAL,
    refund_issued INTEGER DEFAULT 0,             -- 0 = no, 1 = yes
    shopify_refund_id TEXT,                      -- Refund ID returned from Shopify API
    refunded_at TIMESTAMP,

    -- ShipStation void tracking (for future full implementation)
    shipstation_voided INTEGER DEFAULT 0,        -- 0 = no, 1 = yes
    shipstation_shipment_id TEXT,                -- ShipStation shipment ID that was voided
    shipstation_void_response TEXT,              -- Full API response for debugging

    cancelled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_cancelled_order_number ON cancelled_orders(order_number);
CREATE INDEX IF NOT EXISTS idx_cancelled_shopify_id ON cancelled_orders(shopify_order_id);
CREATE INDEX IF NOT EXISTS idx_cancelled_at ON cancelled_orders(cancelled_at);

-- ============================================================================
-- Table: order_sync_status
-- Purpose: Track the last successful sync time for incremental updates
-- ============================================================================
CREATE TABLE IF NOT EXISTS order_sync_status (
    id SERIAL PRIMARY KEY,
    sync_type TEXT NOT NULL UNIQUE,              -- 'shopify_orders' or other sync types
    last_sync_at TIMESTAMP,                      -- When was the last successful sync
    last_sync_count INTEGER DEFAULT 0,           -- How many records were synced
    status TEXT DEFAULT 'idle',                  -- 'running', 'idle', 'error'
    error_message TEXT,                          -- Last error if any
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Insert initial sync status record
INSERT INTO order_sync_status (sync_type, status)
VALUES ('shopify_orders', 'idle')
ON CONFLICT (sync_type) DO NOTHING;

-- ============================================================================
-- Verification: Check that all tables were created
-- ============================================================================
-- Run this after the above to verify:
-- SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY table_name;
