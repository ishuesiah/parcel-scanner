# Parcel Scanner - Project Documentation

## Overview
Flask-based parcel scanning and order management system for Hemlock & Oak Stationery. Integrates with Shopify, ShipStation, UPS, Canada Post, and Klaviyo for order fulfillment, tracking, shipping, and customer notifications.

## Tech Stack
- **Backend**: Python/Flask with eventlet for WebSocket support
- **Database**: PostgreSQL (Neon)
- **Frontend**: Jinja2 templates, vanilla JavaScript, Tailwind-inspired CSS
- **APIs**: Shopify, ShipStation, UPS, Canada Post, Klaviyo
- **Real-time**: Socket.IO for WebSocket-based live tracking updates
- **Background Jobs**: APScheduler for tracking updates, threading for notifications

---

## Recent Changes (January 2025)

### Unified Order Detail Panel
- **New file**: `static/js/order-panel.js` - Reusable slide-out panel module
- Full-screen slide-out panel (ShipStation-style) for order details
- Real-time WebSocket tracking updates via Socket.IO
- Shows shipment info: carrier, tracking number (clickable), ship date, batch
- Shows live tracking: status, estimated delivery, last location, progress bar
- Used across all pages (batch_view, check_shipments, all_orders)
- Global `showOrderDetails(orderNumber)` function for backwards compatibility
- Panel HTML in `base.html`, CSS in `styles.css` (Section 24)

### Collapsible Sidebar
- Sidebar collapses completely (`width: 0`) when hidden
- Floating expand button (right-arrow) appears when collapsed
- State persisted in localStorage
- CSS in `styles.css` (Section 25)

### Move Orders Between Batches
- Select multiple scans in batch_view.html using checkboxes
- Dropdown to select target batch
- Move button transfers scans to different batch
- Inline success message (no page reload required)
- API: `POST /api/scans/move` with `scan_ids`, `target_batch_id`, `source_batch_id`

### Multi-Tab Batch Support
- Scan forms now include hidden `batch_id` field
- `/scan` endpoint accepts `batch_id` from form data (falls back to session)
- Enables scanning to different batches in multiple browser tabs simultaneously

### Background Notification System
- Notifications now run in background thread
- Progress indicator (bottom-right corner) shows:
  - Spinner while sending
  - Progress count and bar (e.g., "5 of 12 (42%)")
  - Checkmark on completion with summary
- Persists across page navigation
- Auto-dismisses after 8 seconds
- **API Endpoints**:
  - `POST /api/notify/start` - Start background notification task
  - `GET /api/notify/status/<batch_id>` - Check task progress
  - `GET /api/notify/status` - Check all active tasks
- Global `notification_tasks` dict tracks progress
- JavaScript `NotificationProgress` module in base.html

### UPS Track Alert Webhooks
- Real-time tracking updates via UPS push notifications
- Endpoint: `POST /webhooks/ups/track-alert`
- Auto-subscribes to tracking updates when scanning UPS packages
- Updates `tracking_status_cache` immediately on webhook receipt
- Env vars: `UPS_WEBHOOK_SECRET`, `APP_URL`

### Stale Tracking Detection
- Detects when estimated delivery date has passed but status still shows "in_transit"
- Forces refresh of tracking data when this occurs
- Prevents showing outdated "delivery by [past date]" messages

### Bug Fixes
- Fixed "cursor already closed" error in order details endpoint
- Fixed dropdown placeholder text visibility (white on white)
- Fixed sidebar collapse not reversing

---

## Key Files

### Core Application
- `web_scanner.py` - Main Flask app with all routes and API endpoints (~7500+ lines)
- `orders_sync.py` - Database initialization and Shopify order sync
- `shopify_api.py` - Shopify API integration
- `websocket_manager.py` - Socket.IO event handlers for real-time updates

### JavaScript Modules
- `static/js/order-panel.js` - Unified order detail slide-out panel

### Shipping & Tracking APIs
- `ups_api.py` - UPS tracking (`UPSAPI`) and shipping/rating/labels (`UPSShippingAPI`)
- `canadapost_api.py` - Canada Post tracking (`CanadaPostAPI`) and rating (`CanadaPostShippingAPI`)
- `rate_shopping.py` - Unified rate comparison across carriers
- `klaviyo_api.py` - Klaviyo email notifications

### Templates (in `/templates`)
- `base.html` - Base layout with sidebar, notification progress bar, order panel
- `macros.html` - Reusable Jinja2 macros (tracking_link, status_badge, progress_bar)
- `all_orders.html` - Orders list with filters, sorting, batch selection, label generation
- `check_shipments.html` - Live tracking dashboard with dynamic tracking groups
- `batch_view.html` - Individual batch view with move-to-batch functionality
- `new_batch.html` - Create new scan batch with async notifications
- `settings.html` - Packing slip builder, carrier account settings
- `ss_batch_detail.html` - ShipStation batch details with order modal
- `order_batches.html` / `order_batch_detail.html` - Order batch management
- `all_scans.html` - All scans list
- `pick_and_pack.html` - Order verification with barcode scanning
- `stuck_orders.html` - Fix orders with missing data

---

## Current Features (Working)

### Order Management
- Sync orders from Shopify (incremental and full 90-day sync)
- Server-side sorting and filtering with JSONB operators
- Shift+click multi-select for batch creation
- Advanced filters (by item, customer, address, international status, country)
- Default view: 250 orders per page

### Customs Information System
- Edit HS codes, descriptions, country of origin per line item
- Save customs info as product defaults (auto-fills future orders)
- Rate shopping across UPS and Canada Post
- Printable customs declaration forms

### Packing Slip Builder
- HTML/CSS/JS editors with live preview
- Template variables: `{{order_number}}`, `{{shipping_name}}`, `{{shipping_first_name}}`, etc.
- Circled quantities for items > 1: `{{this.quantity_circled}}`
- Logo upload support

### Live Tracking Dashboard
- Real-time tracking status from UPS and Canada Post
- **Dynamic Tracking Groups**: Create custom groups with specific orders to track
- Each tracking group appears as its own tab
- Background scheduler (every 15-30 minutes) for automatic updates
- Exception flagging for stuck shipments
- 2-hour cache TTL for tracking data

### Clickable Tracking Numbers (Recently Added)
All tracking numbers are now clickable links to carrier tracking pages:
- UPS → ups.com/track
- Canada Post → canadapost-postescanada.ca/track
- FedEx → fedex.com/fedextrack
- USPS → tools.usps.com
- Unknown carriers → Google search fallback

Uses reusable `tracking_link` macro in `macros.html` and JavaScript `trackingLink()` helper.

### Carrier Account Settings
- Configure UPS credentials (client ID, secret, account number)
- Configure Canada Post credentials (username, password, customer number)
- Stored in `carrier_accounts` table

### Label Generation (UPS Only)
- Generate UPS shipping labels via API
- Supports domestic and international shipments
- Customs declarations for international
- Labels stored in `shipping_labels` table
- Endpoint: `POST /api/orders/<order_number>/create-label`

### Scanning & Batches
- Barcode scanner support for parcel tracking
- Batch creation with carrier selection
- Duplicate detection
- Pick and pack verification with SKU matching

---

## What's Missing / Not Implemented

### Label Generation
- **Canada Post label generation** - Only has tracking and rating, no `create_shipment()` method
- **FedEx/DHL/USPS label generation** - Not integrated
- **Return label creation** - Not implemented

### Carrier Integrations
- **FedEx API** - Not integrated (only Google tracking fallback)
- **DHL API** - Not integrated
- **USPS API** - Not integrated
- **Purolator API** - Not integrated (only tracking URL link)

### Multi-Channel Support
- Only integrates with **Shopify** for orders
- Missing: Amazon, eBay, WooCommerce, Etsy, BigCommerce, etc.

### Automation & Rules
- No automation rules engine (e.g., auto-assign carrier based on weight/destination)
- No scheduled shipment processing
- No automatic rate shopping selection

### User Management
- **No multi-user support** - Single user system
- **No authentication/authorization** - Open access
- **No role-based permissions**
- **No audit logging**

### Security Issues (From Security Audit)
1. **CSRF protection missing** - Need Flask-WTF
2. **Plaintext API secrets** - Should use Fernet encryption
3. **Weak file upload validation** - Need magic byte verification
4. **Missing authorization checks** - Need tenant isolation
5. **Error message exposure** - Should use generic client errors
6. **No session regeneration** - Need regenerate on login
7. **No rate limiting** - Need Flask-Limiter
8. **OAuth state parameter missing** - Add state verification

### Inventory Management
- No stock levels tracking
- No low stock alerts
- No reorder points

### Reporting & Analytics
- No sales reports
- No shipping cost analysis
- No delivery performance metrics

### Other Missing Features
- **Batch printing** - Can't print multiple labels at once
- **Address validation** - No address verification before shipping
- **Insurance options** - Can't add shipping insurance
- **Signature confirmation** - Can't require signature on delivery
- **Email notifications** - No shipping confirmation emails to customers
- **Webhooks** - No webhook support for real-time updates

---

## ShipStation Feature Comparison

| Feature | This App | ShipStation |
|---------|----------|-------------|
| Order import | Shopify only | 100+ integrations |
| Carrier support | UPS, Canada Post | 70+ carriers |
| Label generation | UPS only | All carriers |
| Rate shopping | UPS, Canada Post | All carriers |
| Batch scanning | Yes | No (manual workflow) |
| Duplicate detection | Yes | No |
| Custom tracking groups | Yes | No |
| Pick & pack verification | Yes | Basic |
| Live tracking dashboard | Yes | Basic |
| Automation rules | No | Yes |
| Multi-user | No | Yes |
| Returns management | No | Yes |
| Inventory sync | No | Yes |
| Branded tracking pages | No | Yes |

---

## Database Tables

### Orders & Items
- `orders` - Synced from Shopify (with JSONB shipping_address, billing_address)
- `order_line_items` - Line items with customs fields (hs_code, country_of_origin, customs_description)
- `order_batches` / `order_batch_items` - Batch grouping for fulfillment

### Customs & Shipping
- `product_customs_info` - Default HS codes per SKU (auto-fills on international orders)
- `hs_code_reference` - Common HS codes lookup (pre-populated with stationery codes)
- `shipping_labels` - Generated label PDFs and metadata

### Tracking & Cache
- `shipments_cache` - Cached ShipStation shipment data
- `tracking_status_cache` - Cached UPS/Canada Post tracking statuses
- `tracking_groups` - User-defined tracking groups
- `tracking_group_orders` - Orders assigned to tracking groups

### Scanning
- `scans` - Individual parcel scans
- `batches` - Scan batch groupings

### Settings
- `app_settings` - Key-value store for packing slip templates, company info, etc.
- `carrier_accounts` - UPS/Canada Post API credentials

---

## Environment Variables

### Database
- `DATABASE_URL` - Neon PostgreSQL connection string

### Shopify
- `SHOPIFY_SHOP_NAME`
- `SHOPIFY_ACCESS_TOKEN`

### UPS (for tracking, rating, and labels)
- `UPS_CLIENT_ID`
- `UPS_CLIENT_SECRET`
- `UPS_ACCOUNT_NUMBER` - Required for rating/shipping

### Canada Post (for tracking and rating only)
- `CANADAPOST_USERNAME`
- `CANADAPOST_PASSWORD`
- `CANADAPOST_CUSTOMER_NUMBER` - Required for rating
- `CANADAPOST_CONTRACT_ID` - Optional, for commercial rates
- `CANADAPOST_ENV` - "production" or "development"

### Warehouse/Shipper Address
- `COMPANY_NAME`
- `COMPANY_PHONE`
- `WAREHOUSE_ADDRESS1`
- `WAREHOUSE_ADDRESS2`
- `WAREHOUSE_CITY`
- `WAREHOUSE_PROVINCE`
- `WAREHOUSE_POSTAL`

---

## API Endpoints

### Customs & Shipping
- `GET /api/orders/<order>/customs-info` - Get customs info for order
- `POST /api/orders/<order>/customs-info` - Update customs info
- `GET /api/orders/<order>/rates` - Get shipping rates from all carriers
- `POST /api/orders/<order>/save-customs-defaults` - Save as product defaults
- `GET /api/hs-codes` - List HS codes (supports ?category= and ?search=)
- `POST /api/hs-codes` - Add new HS code

### Labels
- `POST /api/orders/<order>/create-label` - Generate UPS shipping label
- `GET /api/labels/<label_id>/download` - Download label PDF/image
- `GET /api/orders/<order>/labels` - Get all labels for an order

### Tracking Groups
- `GET /api/tracking-groups` - List all tracking groups
- `POST /api/tracking-groups` - Create new tracking group
- `GET /api/tracking-groups/<id>` - Get group with orders
- `PUT /api/tracking-groups/<id>` - Update group name/description
- `DELETE /api/tracking-groups/<id>` - Delete group
- `POST /api/tracking-groups/<id>/orders` - Add orders to group
- `DELETE /api/tracking-groups/<id>/orders/<order>` - Remove order from group

### Orders
- `GET /api/orders/<order>/details` - Full order details with line items, shipment & tracking data
- `POST /api/orders/<order>/cancel` - Cancel order
- `GET /api/orders/<order>/packing-slip` - Generate packing slip PDF
- `GET /api/orders/<order>/customs-form` - Generate customs form PDF

### Scans & Batches
- `POST /api/scans/move` - Move scans between batches (`scan_ids`, `target_batch_id`, `source_batch_id`)
- `POST /scan` - Add scan to batch (accepts `batch_id` in form data for multi-tab support)

### Notifications
- `POST /api/notify/start` - Start background notification task for batch
- `GET /api/notify/status/<batch_id>` - Get notification task progress
- `GET /api/notify/status` - Get all active notification tasks

### Webhooks
- `POST /webhooks/ups/track-alert` - UPS Track Alert webhook for real-time tracking updates

### Settings
- `GET /api/settings` - Get all settings
- `POST /api/settings` - Update settings
- `GET /api/packing-slip/preview` - Preview packing slip with sample data

---

## Background Jobs

### APScheduler Jobs
1. **Shipments Sync** (every 2 minutes) - Sync shipments from ShipStation
2. **Orders Sync** (every 2 minutes) - Incremental sync from Shopify
3. **UPS Tracking Refresh** (every 15 minutes) - Update tracking for non-delivered UPS shipments
4. **Canada Post Tracking Refresh** (every 15 minutes) - Update tracking for non-delivered CP shipments
5. **Email Backfill** (daily) - Backfill missing customer emails
6. **Split Tracking Backfill** (daily) - Handle orders with multiple tracking numbers

### Tracking Cache Strategy
- 2-hour cache TTL for individual tracking lookups
- Only refreshes non-delivered shipments from last 30 days
- Batch processing: Up to 30 UPS / 20 Canada Post per cycle

---

## Notes

### Template Syntax
- Templates use Handlebars-like syntax for packing slips but are rendered by custom JS in `settings.html`
- Use `{% raw %}...{% endraw %}` in Jinja2 templates containing `{{}}` to prevent conflicts
- Import macros with: `{% from "macros.html" import tracking_link %}`

### Database Initialization
- Tables are auto-created via `init_*` functions at startup
- Uses `CREATE TABLE IF NOT EXISTS` - safe to run multiple times
- Default HS codes are inserted with `ON CONFLICT DO NOTHING`

### International Orders
- Detected by checking if `country_code` is not CA/Canada
- Customs section appears automatically in order details modal
- HS codes default to 4820102010 (planners) if not set

### Country Filtering
- Uses PostgreSQL JSONB operators: `shipping_address->>'country_code'`
- Supports both country code (US) and full name (United States)

---

## Recommended Next Steps (Priority Order)

1. **Security hardening** - Add CSRF, encrypt secrets, rate limiting
2. **Canada Post label generation** - Complete shipping integration
3. **Multi-user authentication** - Add login system with roles
4. **Address validation** - Verify addresses before shipping
5. **Batch label printing** - Print multiple labels at once
6. **Email notifications** - Send shipping confirmations
7. **Automation rules** - Auto-select carriers based on criteria
