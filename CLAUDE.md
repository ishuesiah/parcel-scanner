# CLAUDE.md - Hemlock & Oak Parcel Scanner

## Project Overview

This is a **Flask-based parcel scanning and tracking application** (v1.2.1) for Hemlock & Oak. It enables warehouse staff to scan parcel barcodes, look up order information from Shopify and ShipStation, track shipments via UPS API, and send customer notifications through Klaviyo.

**Tech Stack:**
- **Backend:** Python 3.9+, Flask
- **Database:** PostgreSQL (hosted on Neon)
- **Hosting:** Kinsta (production)
- **Integrations:** Shopify, ShipStation, UPS, Klaviyo

---

## Project Structure

```
parcel-scanner/
├── web_scanner.py          # Main Flask application (~6000 lines)
├── shopify_api.py          # Shopify API client for order lookup
├── ups_api.py              # UPS OAuth2 tracking API client
├── klaviyo_api.py          # Klaviyo Events API client (track events)
├── klaviyo_events.py       # Klaviyo event tracking wrapper
├── tracking_utils.py       # Tracking number detection and splitting
├── address_utils.py        # PO Box detection and carrier compatibility
├── backfill_emails.py      # Standalone script: backfill missing emails
├── backfill_split_tracking.py  # Standalone script: split concatenated tracking
├── config.py               # Legacy config (not actively used)
├── requirements.txt        # Python dependencies
└── static/
    ├── parcel-scan.jpg     # Scanner image
    └── scan-success.mp3    # Audio feedback
```

---

## Key Components

### 1. Main Application (`web_scanner.py`)

The core Flask app handles:
- **Authentication:** Password-protected login with bcrypt hashing
- **Batch Management:** Create, edit, view, delete scan batches
- **Scanning:** Process tracking numbers, lookup orders, detect duplicates
- **Customer Notifications:** Send Klaviyo events when parcels are scanned
- **Background Sync:** Periodic ShipStation sync, UPS tracking refresh

**Key Routes:**
| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Dashboard with active batch |
| `/login` | GET/POST | User authentication |
| `/scan` | POST | Process scanned tracking number |
| `/new_batch` | GET/POST | Create new scan batch |
| `/all_batches` | GET | View all batches |
| `/all_scans` | GET | View all scans with search/filter |
| `/pick_and_pack` | GET/POST | Pick and pack workflow |
| `/check_shipments` | GET | View shipments with UPS status |
| `/stuck_orders` | GET | Orders with processing issues |
| `/notify_customers` | POST | Send Klaviyo notifications |

### 2. Shopify Integration (`shopify_api.py`)

- Searches orders by tracking number (up to 365 days back)
- Supports exact and fuzzy tracking number matching
- Includes order caching to reduce API calls
- Rate limiting with exponential backoff

**Environment Variables:**
- `SHOPIFY_ACCESS_TOKEN` (required)
- `SHOP_URL` (required)
- `SHOPIFY_API_KEY`, `SHOPIFY_API_SECRET` (optional)

### 3. UPS Tracking (`ups_api.py`)

- OAuth 2.0 client credentials authentication
- Token caching with auto-refresh
- Parses tracking status: `label_created`, `in_transit`, `delivered`, `exception`
- Extracts estimated delivery dates

**Environment Variables:**
- `UPS_CLIENT_ID`
- `UPS_CLIENT_SECRET`

### 4. Klaviyo Integration (`klaviyo_api.py`, `klaviyo_events.py`)

Sends events to Klaviyo for email automation:
- `Parcel Scanned` - When a parcel is scanned
- `Duplicate Scan Detected` - When same tracking is scanned twice

**Environment Variable:**
- `KLAVIYO_API_KEY` (must start with `pk_`)

### 5. Tracking Utilities (`tracking_utils.py`)

- **Carrier Detection:** Identifies UPS (1Z...), Canada Post, FedEx, Purolator, DHL
- **Concatenation Splitting:** Detects and splits accidentally joined tracking numbers (e.g., two UPS numbers scanned as one)

### 6. Address Utilities (`address_utils.py`)

- **PO Box Detection:** Regex-based detection of various PO Box formats
- **Carrier Compatibility:** UPS/FedEx/DHL cannot deliver to PO Boxes

---

## Database Schema

Uses PostgreSQL with the following main tables:

### `scans`
Stores individual parcel scans:
- `tracking_number`, `carrier`, `order_number`
- `customer_name`, `customer_email`
- `batch_id`, `scan_date`, `status`, `order_id`

### `batches`
Groups scans into batches:
- `batch_name`, `created_at`, `finished_at`
- `notes`, `status`

### `shipments_cache`
Caches ShipStation shipment data:
- `tracking_number`, `order_number`, `carrier_code`
- `ship_date`, `shipstation_batch_number`

### `tracking_status_cache`
Caches UPS tracking results (2-hour TTL):
- `tracking_number`, `status`, `status_description`
- `estimated_delivery`, `is_delivered`

### `item_location_rules`
Warehouse location mapping:
- `rule_type` (sku/keyword), `rule_value`
- `aisle`, `shelf`

---

## Environment Variables

Required for production:
```bash
# Flask
FLASK_SECRET_KEY=<secure-random-string>
APP_PASSWORD_HASH=<bcrypt-hash>

# Database
DATABASE_URL=postgres://user:pass@host/db

# Shopify
SHOPIFY_ACCESS_TOKEN=<token>
SHOP_URL=your-store.myshopify.com

# ShipStation
SHIPSTATION_API_KEY=<key>
SHIPSTATION_API_SECRET=<secret>

# UPS
UPS_CLIENT_ID=<client-id>
UPS_CLIENT_SECRET=<secret>

# Klaviyo
KLAVIYO_API_KEY=pk_<private-key>
KLAVIYO_ENABLE=true  # optional, defaults to true
```

---

## Development Guidelines

### Running Locally

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   pip install python-dotenv  # for local .env support
   ```

2. Create `.env` file with required variables

3. Run the app:
   ```bash
   python web_scanner.py
   # or
   flask run
   ```

### Code Conventions

1. **Database Connections:**
   - Always use `get_db_connection()` which includes retry logic
   - For queries that might fail mid-execution, use `execute_with_retry()`
   - Close connections in `finally` blocks

2. **API Clients:**
   - Use singleton pattern via `get_shopify_api()`, `get_ups_api()`, `get_klaviyo_events()`
   - Include exponential backoff for rate limiting (429) and server errors (5xx)

3. **Tracking Numbers:**
   - UPS: 18 chars starting with "1Z"
   - Canada Post: 16 chars (normalized from 28-char barcode)
   - Always check for concatenated tracking numbers using `split_concatenated_tracking_numbers()`

4. **Timezone:**
   - All times displayed in Vancouver/PST timezone
   - Use `now_pst()` and `format_pst()` helpers

5. **Error Handling:**
   - Print descriptive logs with emoji prefixes for visibility
   - Never crash on API failures; return graceful defaults

### Templates

Templates are embedded as string constants in `web_scanner.py`:
- `LOGIN_TEMPLATE`, `MAIN_TEMPLATE`, `BATCHES_TEMPLATE`, etc.
- Use Jinja2 templating with `render_template_string()`

### Background Tasks

The app starts a background thread on startup that:
1. Syncs ShipStation shipments every 5 minutes
2. Refreshes UPS tracking every 15 minutes
3. Runs backfills (split tracking, missing emails) on startup and daily

---

## Common Tasks

### Adding a New Route

1. Define route in `web_scanner.py`:
   ```python
   @app.route("/my_route", methods=["GET"])
   def my_route():
       if not session.get("logged_in"):
           return redirect(url_for("login"))
       # ... implementation
   ```

2. Add template if needed (as string constant)

### Adding a New API Integration

1. Create new file (e.g., `new_api.py`)
2. Follow existing patterns: singleton getter, retry logic, environment variables
3. Import and use in `web_scanner.py`

### Database Migrations

No formal migration system. Add new tables/columns via:
```python
def init_new_table():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS my_table (...)
    """)
    conn.commit()
    cursor.close()
    conn.close()
```

---

## Testing

No automated test suite currently. Testing is done manually:

1. **Unit testing utilities:**
   ```bash
   python tracking_utils.py  # Runs built-in tests
   python address_utils.py   # Runs built-in tests
   ```

2. **Manual testing:** Access routes via browser after local startup

---

## Deployment

- **Platform:** Kinsta
- **Process Manager:** Gunicorn
- **Entry Point:** `web_scanner.py` exposes the `app` object

Production command (managed by Kinsta):
```bash
gunicorn web_scanner:app
```

---

## Troubleshooting

### Common Issues

1. **"Missing SHOPIFY_ACCESS_TOKEN"**
   - Ensure environment variables are set in Kinsta dashboard

2. **Database connection errors**
   - Check `DATABASE_URL` format
   - Verify Neon PostgreSQL is accessible

3. **Klaviyo 401 errors**
   - Ensure using private key (`pk_...`), not public key

4. **UPS tracking not updating**
   - Check `UPS_CLIENT_ID` and `UPS_CLIENT_SECRET`
   - Verify OAuth token is being obtained (check logs)

### Log Patterns

Look for emoji prefixes in logs:
- `✓` / `✅` - Success
- `⚠️` - Warning
- `❌` - Error
- `🔍` - Search/lookup
- `📦` - Tracking/shipping
- `🔄` - Sync/refresh
- `📤` / `📨` - API calls

---

## Security Notes

- Passwords hashed with bcrypt
- Session cookies: `Secure`, `HttpOnly`, `SameSite=Lax`
- 30-minute inactivity timeout
- Never commit `.env` files or expose API keys
