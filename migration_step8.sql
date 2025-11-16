-- Migration Step 8: Add ShipStation batch number tracking
-- This allows us to track which ShipStation batch a scan belongs to

ALTER TABLE scans
ADD COLUMN shipstation_batch_number VARCHAR(50) DEFAULT '' AFTER carrier;
