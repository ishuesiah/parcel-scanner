# CLAUDE.md - Hemlock & Oak Parcel Scanner

## Project Overview

**Hemlock & Oak Parcel Scanner** (v1.2.1) is a Flask-based web application for tracking and managing parcel shipments. It integrates with multiple shipping carriers and e-commerce platforms to provide real-time tracking, batch scanning operations, and customer notification workflows.

## Tech Stack

- **Backend**: Python 3.9+, Flask
- **Database**: PostgreSQL (hosted on Neon)
- **Production Server**: Gunicorn
- **External Integrations**:
  - Shopify (order data)
  - Klaviyo (customer event tracking)
  - UPS (tracking API with OAuth 2.0)
  - Canada Post (tracking API)
  - ShipStation (shipment management)

## Project Structure

```
parcel-scanner/
├── web_scanner.py          # Main Flask application (all routes)
├── config.py               # Legacy config (deprecated, use env vars)
├── requirements.txt        # Python dependencies
│
├── # API Integrations
├── shopify_api.py          # Shopify order lookups
├── klaviyo_api.py          # Klaviyo event tracking (low-level)
├── klaviyo_events.py       # Klaviyo events (high-level)
├── ups_api.py              # UPS tracking (OAuth 2.0)
├── canadapost_api.py       # Canada Post tracking
│
├── # Utilities
├── tracking_utils.py       # Tracking number detection & splitting
├── address_utils.py        # PO Box detection
│
├── # Backfill Scripts
├── backfill_emails.py      # Backfill missing customer emails
├── backfill_split_tracking.py  # Split concatenated tracking numbers
│
├── templates/              # Jinja2 HTML templates
│   ├── base.html           # Base layout with sidebar nav
│   ├── new_batch.html      # New batch creation
│   ├── batch_view.html     # Single batch view
│   ├── all_batches.html    # All batches list
│   ├── all_scans.html      # All scans list
│   ├── check_shipments.html # Live tracking dashboard
│   ├── pick_and_pack.html  # Pick and pack workflow
│   ├── item_locations.html # Item location rules
│   ├── stuck_orders.html   # Fix stuck orders
│   └── ss_batch_detail.html # ShipStation batch detail
│
└── static/                 # Static assets
    ├── waypost-logo.jpg
    ├── scan-success.mp3    # Audio feedback
    ├── error-dupe.mp3
    └── error-wrong-scan.mp3
```

## Key Routes

| Route | Method | Description |
|-------|--------|-------------|
| `/login` | GET/POST | User authentication |
| `/logout` | GET | End session |
| `/` | GET | Home/dashboard (redirects to all_batches) |
| `/new_batch` | GET/POST | Create new scanning batch |
| `/all_batches` | GET | View all batches |
| `/view_batch/<id>` | GET | View single batch details |
| `/scan` | POST | Process a parcel scan (AJAX) |
| `/all_scans` | GET | View all scans with filtering |
| `/check_shipments` | GET | Live tracking status dashboard |
| `/pick_and_pack` | GET/POST | Pick and pack workflow |
| `/item_locations` | GET | Manage item location rules |
| `/stuck_orders` | GET | View and fix stuck orders |
| `/ss_batches/<id>` | GET | ShipStation batch details |
| `/debug_tracking/<num>` | GET | Debug tracking number lookup |

## Database Schema

The application uses PostgreSQL with these main tables:

### `scans`
- `id` - Primary key
- `tracking_number` - Parcel tracking number
- `carrier` - Detected carrier (UPS, Canada Post, etc.)
- `order_number` - Shopify order number
- `customer_name` - Customer full name
- `customer_email` - Customer email (for Klaviyo)
- `batch_id` - FK to batch
- `scan_date` - Timestamp
- `status` - Current status
- `order_id` - Shopify order ID
- `shipstation_batch_number` - ShipStation batch ref

### `batches`
- `id` - Primary key
- `name` - Batch name
- `created_at` - Creation timestamp
- `finished_at` - Completion timestamp
- `notes` - Optional notes

### `shipments_cache`
- Caches ShipStation shipment data for faster lookups
- `tracking_number`, `order_number`, `customer_name`, `carrier_code`, `ship_date`

### `tracking_status_cache`
- Caches carrier tracking status to reduce API calls
- 2-hour TTL for cache entries
- `tracking_number`, `carrier`, `status`, `status_description`, `estimated_delivery`, `is_delivered`

## Environment Variables

### Required
```bash
FLASK_SECRET_KEY=        # Flask session security key
DATABASE_URL=            # PostgreSQL connection string (Neon format)
APP_PASSWORD_HASH=       # bcrypt hash for app login
```

### Shopify
```bash
SHOP_URL=                # Shopify store URL (e.g., mystore.myshopify.com)
SHOPIFY_ACCESS_TOKEN=    # Shopify Admin API access token
SHOPIFY_API_KEY=         # (Optional) Shopify API key
SHOPIFY_API_SECRET=      # (Optional) Shopify API secret
```

### Klaviyo
```bash
KLAVIYO_API_KEY=         # Private API key (starts with pk_)
KLAVIYO_ENABLE=true      # Enable/disable Klaviyo events
```

### UPS
```bash
UPS_CLIENT_ID=           # UPS OAuth client ID
UPS_CLIENT_SECRET=       # UPS OAuth client secret
```

### Canada Post
```bash
CANADAPOST_USERNAME=     # Canada Post API username
CANADAPOST_PASSWORD=     # Canada Post API password
CANADAPOST_ENV=production # 'production' or 'development'
```

### ShipStation
```bash
SHIPSTATION_API_KEY=     # ShipStation v1 API key
SHIPSTATION_API_SECRET=  # ShipStation v1 API secret
SHIPSTATION_V2_API_KEY=  # ShipStation v2 API key
```

## Development Commands

### Install Dependencies
```bash
pip install -r requirements.txt
```

### Run Development Server
```bash
# Set required env vars first
export FLASK_SECRET_KEY="dev-secret-key"
export DATABASE_URL="postgresql://..."
export APP_PASSWORD_HASH="..."

python web_scanner.py
```

### Run Production Server
```bash
gunicorn web_scanner:app --bind 0.0.0.0:8080
```

### Backfill Scripts
```bash
# Backfill missing customer emails
python backfill_emails.py --limit 100 --delay 0.5

# Find and split concatenated tracking numbers
python backfill_split_tracking.py           # Execute
python backfill_split_tracking.py --dry-run # Preview only
```

## Code Patterns & Conventions

### API Client Singletons
All API clients use singleton pattern for connection reuse:
```python
_shopify_api = None
def get_shopify_api():
    global _shopify_api
    if _shopify_api is None:
        _shopify_api = ShopifyAPI()
    return _shopify_api
```

### Database Connection with Retry
```python
def get_db_connection():
    max_retries = 3
    for retry in range(max_retries):
        try:
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
            conn.autocommit = True
            return conn
        except psycopg2.OperationalError:
            if retry < max_retries - 1:
                time.sleep(min(2 ** retry, 4))
            else:
                raise
```

### Tracking Number Detection
Use `tracking_utils.detect_carrier()` to identify carrier from tracking format:
- UPS: Starts with `1Z`, 18 characters
- Canada Post: 16 characters (normalized from 28-char barcode)
- FedEx: 12 or 15 digits
- Purolator: 12 digits

### PO Box Validation
Use `address_utils.check_po_box_compatibility()` before shipping:
- Canada Post and USPS can deliver to PO Boxes
- UPS, FedEx, DHL, Purolator cannot

### Timezone Handling
All times use Vancouver/PST timezone:
```python
from zoneinfo import ZoneInfo
PST = ZoneInfo("America/Vancouver")
now_pst()      # Current time in PST
format_pst(dt) # Format datetime to PST string
```

### Carrier Name Normalization
Use `normalize_carrier()` for consistent display names:
```python
normalize_carrier("CANADA_POST_WALLETED")  # Returns "Canada Post"
normalize_carrier("UPS_GROUND")             # Returns "UPS"
```

## Testing

Currently no formal test suite. Test manually via:
1. `/debug_tracking/<tracking_number>` - Debug tracking lookups
2. Backfill scripts with `--dry-run` flag
3. Check browser console for AJAX errors on scan operations

## Security Notes

- Session cookies are `Secure`, `HttpOnly`, and `SameSite=Lax`
- Password hashed with bcrypt
- 30-minute inactivity timeout
- All API credentials must be in environment variables, never in code

## Common Issues & Solutions

### "UPS API not enabled"
Missing `UPS_CLIENT_ID` or `UPS_CLIENT_SECRET` environment variables.

### "Shopify rate limit hit"
The app implements automatic retry with exponential backoff. If persistent, reduce batch sizes.

### "Canada Post API timeout"
Canada Post API can be slow. Timeout is set to 15 seconds with retry logic.

### Concatenated Tracking Numbers
If a scan shows 36 characters (two UPS numbers stuck together), run:
```bash
python backfill_split_tracking.py
```

### Missing Customer Emails
Emails are required for Klaviyo events. Run:
```bash
python backfill_emails.py --limit 500
```

## Integration Notes

### Klaviyo Events
The app sends these Klaviyo events:
- `Parcel Scanned` - When a parcel is scanned successfully
- `Duplicate Scan Detected` - When a duplicate scan is attempted

### ShipStation Sync
Background thread syncs shipments every 5 minutes from ShipStation API. Data cached in `shipments_cache` table.

### Tracking Status Cache
Carrier tracking statuses cached for 2 hours in `tracking_status_cache` to reduce API calls. Force refresh available on Live Tracking page.
