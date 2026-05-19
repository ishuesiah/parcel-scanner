-- ============================================================================
-- Hemlock & Oak Parcel Scanner - Sticker Pre-Orders Schema
-- A lightweight per-order log of items that need pre-order stickers applied.
-- Replaces the Slack-tab spreadsheet workflow.
-- ============================================================================

CREATE TABLE IF NOT EXISTS sticker_preorders (
    id SERIAL PRIMARY KEY,

    -- Order reference (free text so entries can be created before order syncs)
    order_number TEXT,                           -- e.g., "60430"
    customer_name TEXT,                          -- denormalized snapshot

    -- Item info
    sku TEXT,
    product_title TEXT NOT NULL,                 -- "Forest Green Planner", "Washi Tape Set A", etc.
    quantity INTEGER DEFAULT 1,

    -- Status workflow: needed → applied → shipped
    -- 'cancelled' for entries that no longer need a sticker
    status TEXT NOT NULL DEFAULT 'needed',

    -- Free text notes
    notes TEXT,

    -- Accountability
    created_by TEXT,                             -- session user_email when added
    updated_by TEXT,                             -- last person to change status

    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP                       -- set when status moves to 'shipped' or 'cancelled'
);

CREATE INDEX IF NOT EXISTS idx_sticker_preorders_status ON sticker_preorders(status);
CREATE INDEX IF NOT EXISTS idx_sticker_preorders_order_number ON sticker_preorders(order_number);
CREATE INDEX IF NOT EXISTS idx_sticker_preorders_created_at ON sticker_preorders(created_at);
