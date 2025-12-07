# Parcel Scanner - Project Documentation

## Overview
Flask-based parcel scanning and order management system for Hemlock & Oak Stationery. Integrates with Shopify, ShipStation, UPS, and Canada Post for order fulfillment, tracking, and shipping.

## Tech Stack
- **Backend**: Python/Flask
- **Database**: PostgreSQL (Neon)
- **Frontend**: Jinja2 templates, vanilla JavaScript, Tailwind-inspired CSS
- **APIs**: Shopify, ShipStation, UPS, Canada Post

## Key Files

### Core Application
- `web_scanner.py` - Main Flask app with all routes and API endpoints
- `orders_sync.py` - Database initialization and Shopify order sync
- `shopify_api.py` - Shopify API integration

### Shipping & Tracking APIs
- `ups_api.py` - UPS tracking (`UPSAPI`) and shipping/rating (`UPSShippingAPI`)
- `canadapost_api.py` - Canada Post tracking (`CanadaPostAPI`) and rating (`CanadaPostShippingAPI`)
- `rate_shopping.py` - Unified rate comparison across carriers

### Templates (in `/templates`)
- `base.html` - Base layout with sidebar navigation
- `all_orders.html` - Orders list with filters, sorting, batch selection
- `check_shipments.html` - Live tracking dashboard
- `ss_batch_detail.html` - ShipStation batch details with order modal
- `settings.html` - Packing slip builder with live preview
- `order_batches.html` / `order_batch_detail.html` - Order batch management

## Database Tables

### Orders & Items
- `orders` - Synced from Shopify
- `order_line_items` - Line items with customs fields (hs_code, country_of_origin, customs_description)
- `order_batches` / `order_batch_items` - Batch grouping for fulfillment

### Customs & Shipping
- `product_customs_info` - Default HS codes per SKU (auto-fills on international orders)
- `hs_code_reference` - Common HS codes lookup (pre-populated with stationery codes)

### Settings
- `app_settings` - Key-value store for packing slip templates, company info, etc.

## Environment Variables

### Database
- `DATABASE_URL` - Neon PostgreSQL connection string

### Shopify
- `SHOPIFY_SHOP_NAME`
- `SHOPIFY_ACCESS_TOKEN`

### UPS
- `UPS_CLIENT_ID`
- `UPS_CLIENT_SECRET`
- `UPS_ACCOUNT_NUMBER` - Required for rating/shipping

### Canada Post
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

## Key Features

### Order Management
- Sync orders from Shopify (incremental and full 90-day sync)
- Server-side sorting and filtering
- Shift+click multi-select for batch creation
- Advanced filters (by item, customer, address, international status)

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

### Live Tracking
- Real-time tracking status from UPS and Canada Post
- Background scheduler for automatic updates
- Exception flagging for stuck shipments

## API Endpoints

### Customs & Shipping
- `GET /api/orders/<order>/customs-info` - Get customs info for order
- `POST /api/orders/<order>/customs-info` - Update customs info
- `GET /api/orders/<order>/rates` - Get shipping rates from all carriers
- `POST /api/orders/<order>/save-customs-defaults` - Save as product defaults
- `GET /api/hs-codes` - List HS codes (supports ?category= and ?search=)
- `POST /api/hs-codes` - Add new HS code
- `GET /api/product-customs` - Get product customs defaults
- `POST /api/product-customs` - Save product customs defaults

### Orders
- `GET /api/orders/<order>/details` - Full order details with line items
- `POST /api/orders/<order>/cancel` - Cancel order (with Shopify sync option)

### Settings
- `GET /api/settings` - Get all settings
- `POST /api/settings` - Update settings
- `GET /api/packing-slip/preview` - Preview packing slip with sample data

## Notes

### Template Syntax
- Templates use Handlebars-like syntax but are rendered by custom JS in `settings.html`
- Use `{% raw %}...{% endraw %}` in Jinja2 templates containing `{{}}` to prevent conflicts

### Database Initialization
- Tables are auto-created via `init_orders_tables()` in `orders_sync.py`
- Uses `CREATE TABLE IF NOT EXISTS` - safe to run multiple times
- Default HS codes are inserted with `ON CONFLICT DO NOTHING`

### International Orders
- Detected by checking if `country_code` is not CA/Canada
- Customs section appears automatically in order details modal
- HS codes default to 4820102010 (planners) if not set
