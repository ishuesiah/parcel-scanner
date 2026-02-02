# Parcel Scanner - February 2025 Changes

## Overview

This document covers the changes made to the "Parcels Not Moving" feature to fix live tracking issues, add real-time updates, and improve usability.

---

## Bug Fixes

### 1. Delivered Parcels Showing in "Parcels Not Moving" List

**Problem:** The "Parcels Not Moving" tab was incorrectly displaying delivered parcels because:
- The background tracking refresh only covered shipments from the last 30 days
- The page displayed shipments up to 120 days old
- Packages delivered after day 30 never got their tracking cache updated

**Solution:**
- Added database-level filtering to exclude delivered/in_transit packages in the SQL query
- Extended background refresh to cover packages 30-120 days old if they still show `label_created` status
- Added `AND (tc.is_delivered IS NULL OR tc.is_delivered = false)` filter
- Added `AND (tc.status IS NULL OR tc.status NOT IN ('delivered', 'in_transit'))` filter

**Files Changed:**
- `web_scanner.py` - Lines 5287-5291 (SQL query), Lines 775-800 (background refresh)

---

## New Features

### 2. Live Tracking Updates via WebSocket

**What it does:** Parcels now update in real-time without manual refresh.

**How it works:**
1. When page loads, WebSocket connects and subscribes to tracking updates
2. Automatically requests tracking refresh for all visible packages (in batches of 20 every 10 seconds)
3. When UPS webhook arrives or background job updates tracking, WebSocket pushes update to browser
4. Delivered/in-transit packages animate out and disappear from the list
5. Page auto-checks for DB changes every 60 seconds as fallback

**UI Indicators:**
- Green "Live updates" indicator in bottom-left when connected
- Toast notifications when packages are delivered or start moving
- Row highlights green and slides out when status changes

**Files Changed:**
- `templates/stationary_parcels.html` - Added Socket.IO integration, auto-refresh logic

---

### 3. Sortable Columns

**What it does:** Click column headers to sort the parcel list.

**Sortable columns:**
- **Days Stuck** - Sort by how long the parcel has been stuck (default: descending)
- **Customer** - Sort alphabetically by customer name
- **Ship Date** - Sort by ship date (newest/oldest first)

**How to use:** Click a column header to sort. Click again to reverse the direction. Arrow indicator shows current sort.

**Files Changed:**
- `web_scanner.py` - Lines 5222-5223 (sort parameters), Lines 5370-5377 (sort logic)
- `templates/stationary_parcels.html` - Sortable table headers with links

---

### 4. Resolve Parcel Feature

**What it does:** Manually resolve parcels that won't be delivered (lost, refunded, re-shipped, etc.)

**Resolution Types:**
| Type | Description |
|------|-------------|
| `delivered_manually` | Confirmed delivered outside of tracking system |
| `refunded` | Customer was refunded |
| `reshipped` | Re-shipped with new tracking number |
| `lost` | Lost in transit |
| `picked_up` | Customer picked up |
| `other` | Other resolution |

**New Tracking Option:** When "Re-shipped" is selected, you can enter the new tracking number. It will be added to the tracking cache automatically.

**How to use:**
1. Click "Resolve" button next to any parcel
2. Select resolution type from dropdown
3. If re-shipped, enter new tracking number
4. Add optional notes
5. Click "Resolve" - parcel is removed from list

**Database:** Creates `resolved_parcels` table to store resolution history.

**API Endpoints:**
- `POST /api/parcels/resolve` - Resolve a parcel
- `GET /api/parcels/resolved` - Get list of resolved parcels

**Files Changed:**
- `web_scanner.py` - Lines 1055-1068 (table creation), Lines 5469-5540 (API endpoints), Lines 5286 (query filter)
- `templates/stationary_parcels.html` - Resolve button and modal

---

### 5. Faster Background Tracking Refresh

**Changes:**
| Setting | Before | After |
|---------|--------|-------|
| Refresh interval | 30 minutes | 10 minutes |
| UPS batch size | 30 | 50 (background), 100 (manual) |
| Canada Post batch size | 20 | 30 (background), 50 (manual) |
| Lookback period | 30 days | 120 days for `label_created` packages |

**Files Changed:**
- `web_scanner.py` - Lines 743, 822-834

---

### 6. Manual Refresh Improvements

**Changes:**
- "Refresh Tracking" button now refreshes ALL parcels (not just current page)
- Redirects after starting refresh to prevent stale data display
- Shows flash message: "Refreshing tracking for X parcels in background..."

**Files Changed:**
- `web_scanner.py` - Lines 5383-5405
- `templates/stationary_parcels.html` - Flash message display

---

## Technical Details

### Database Schema Changes

**New Table: `resolved_parcels`**
```sql
CREATE TABLE IF NOT EXISTS resolved_parcels (
    id SERIAL PRIMARY KEY,
    tracking_number VARCHAR(255) NOT NULL,
    order_number VARCHAR(255),
    resolution_type VARCHAR(50) NOT NULL,
    resolution_notes TEXT,
    new_tracking_number VARCHAR(255),
    resolved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_by VARCHAR(255)
);
```

**Indexes:**
- `idx_resolved_parcels_tracking` on `tracking_number`
- `idx_resolved_parcels_order` on `order_number`

---

### WebSocket Events Used

| Event | Direction | Purpose |
|-------|-----------|---------|
| `subscribe_shipments_page` | Client → Server | Subscribe to all tracking updates |
| `subscribe_tracking` | Client → Server | Subscribe to specific tracking numbers |
| `request_tracking_refresh` | Client → Server | Request backend to poll APIs for updates |
| `tracking_update` | Server → Client | Push tracking status change |
| `subscription_confirmed` | Server → Client | Confirm subscription success |

---

### API Endpoints Added

**POST /api/parcels/resolve**
```json
// Request
{
    "tracking_number": "1Z...",
    "order_number": "12345",
    "resolution_type": "refunded",
    "resolution_notes": "Customer requested refund",
    "new_tracking_number": "" // Optional, for reshipped
}

// Response
{
    "success": true,
    "message": "Parcel 1Z... marked as refunded",
    "new_tracking": null
}
```

**GET /api/parcels/resolved**
```json
// Response
{
    "success": true,
    "resolved": [
        {
            "tracking_number": "1Z...",
            "order_number": "12345",
            "resolution_type": "refunded",
            "resolution_notes": "...",
            "new_tracking_number": null,
            "resolved_at": "2025-02-01T..."
        }
    ]
}
```

---

## Branch Information

**Branch:** `fix/stationary-parcels-delivered-filter`

**Commits:**
1. `42a2bb4` - Fix stationary parcels showing delivered packages
2. `8340658` - Add live tracking updates and sorting to stationary parcels
3. `b1a841f` - Fix refresh and sorting behavior
4. `dfe3471` - Auto-refresh tracking on page load via WebSocket
5. `bec468b` - Add resolve parcel feature with new tracking option

---

## Testing Checklist

- [ ] Load "Parcels Not Moving" page - should not show delivered packages
- [ ] Check browser console for WebSocket connection messages
- [ ] Wait 10-30 seconds - packages should start updating/disappearing as APIs respond
- [ ] Click column headers to verify sorting works
- [ ] Click "Resolve" on a parcel - verify modal opens
- [ ] Select "Re-shipped" - verify new tracking field appears
- [ ] Submit resolution - verify parcel disappears from list
- [ ] Reload page - verify resolved parcel doesn't reappear
