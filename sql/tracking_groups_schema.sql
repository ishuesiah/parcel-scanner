-- ============================================================================
-- Tracking Groups Schema - For CS Ticket Tracking
-- Run this SQL to add tracking groups functionality
-- ============================================================================

-- ============================================================================
-- Table: tracking_groups
-- Purpose: Named groups for tracking multiple orders together (CS tickets)
-- ============================================================================
CREATE TABLE IF NOT EXISTS tracking_groups (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,                          -- Group name, e.g., "CS Ticket #12345"
    description TEXT,                            -- Optional notes
    created_by TEXT,                             -- Who created this group
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tracking_groups_name ON tracking_groups(name);
CREATE INDEX IF NOT EXISTS idx_tracking_groups_created ON tracking_groups(created_at);

-- ============================================================================
-- Table: tracking_group_orders
-- Purpose: Orders belonging to each tracking group
-- ============================================================================
CREATE TABLE IF NOT EXISTS tracking_group_orders (
    id SERIAL PRIMARY KEY,
    group_id INTEGER NOT NULL,                   -- FK to tracking_groups.id
    order_number TEXT NOT NULL,                  -- Order number to track
    tracking_number TEXT,                        -- Optional: specific tracking number
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,                                  -- Optional notes for this order in context

    FOREIGN KEY (group_id) REFERENCES tracking_groups(id) ON DELETE CASCADE,
    UNIQUE(group_id, order_number)               -- Prevent duplicate orders in same group
);

CREATE INDEX IF NOT EXISTS idx_group_orders_group ON tracking_group_orders(group_id);
CREATE INDEX IF NOT EXISTS idx_group_orders_order ON tracking_group_orders(order_number);
