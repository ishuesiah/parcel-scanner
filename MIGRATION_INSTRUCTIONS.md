# Database Migration Instructions

## Overview
These migrations add new features to your parcel scanner app:
- ✉️ Klaviyo email notifications
- 📝 Batch notes field
- ✓ Batch status tracking (in_progress → recorded → notified)
- 🔒 Duplicate notification prevention

---

## How to Run Migrations

### Option 1: Via Kinsta MySQL Manager (Recommended)

1. **Log into your Kinsta dashboard**
2. Go to your application → **Database** tab
3. Click **"Open phpMyAdmin"** or **"Open Adminer"**
4. Select your database from the left sidebar
5. Click the **"SQL"** tab at the top
6. **Copy/paste each command below ONE AT A TIME**
7. Click **"Go"** or **"Execute"** after each command
8. Check for success message (if you see "Duplicate column" error, that's okay - skip to next command)

---

### Option 2: Via MySQL Command Line

If you have SSH access or local MySQL client:

```bash
# Connect to your database
mysql -h YOUR_HOST -u YOUR_USER -p YOUR_DATABASE

# Then paste each command from migrations.sql one at a time
```

---

## SQL Commands to Run

### Step 1: Add customer_email to scans table
```sql
ALTER TABLE scans
ADD COLUMN customer_email VARCHAR(255) DEFAULT '' AFTER order_id;
```
**What this does:** Stores customer email addresses for Klaviyo notifications

---

### Step 2: Add status to batches table
```sql
ALTER TABLE batches
ADD COLUMN status VARCHAR(20) DEFAULT 'in_progress' AFTER carrier;
```
**What this does:** Tracks batch lifecycle (in_progress → recorded → notified)

---

### Step 3: Add notified_at to batches table
```sql
ALTER TABLE batches
ADD COLUMN notified_at DATETIME NULL AFTER status;
```
**What this does:** Records when Klaviyo notifications were sent

---

### Step 4: Add notes to batches table
```sql
ALTER TABLE batches
ADD COLUMN notes TEXT NULL AFTER notified_at;
```
**What this does:** Allows you to save notes/comments for each batch

---

### Step 5: Create notifications table
```sql
CREATE TABLE IF NOT EXISTS notifications (
    id INT AUTO_INCREMENT PRIMARY KEY,
    batch_id INT NOT NULL,
    order_number VARCHAR(100) NOT NULL,
    customer_email VARCHAR(255) NOT NULL,
    tracking_number VARCHAR(100) NOT NULL,
    notified_at DATETIME NOT NULL,
    success BOOLEAN DEFAULT TRUE,
    error_message TEXT NULL,
    INDEX idx_order_number (order_number),
    INDEX idx_batch_id (batch_id),
    UNIQUE KEY unique_order_notification (order_number, batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```
**What this does:** Prevents sending duplicate notifications to customers

---

## Verification

Run this query to verify all migrations completed successfully:

```sql
SELECT
    'scans.customer_email' as column_name,
    COUNT(*) as exists_count
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'scans'
  AND COLUMN_NAME = 'customer_email'
UNION ALL
SELECT 'batches.status', COUNT(*)
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'batches'
  AND COLUMN_NAME = 'status'
UNION ALL
SELECT 'batches.notified_at', COUNT(*)
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'batches'
  AND COLUMN_NAME = 'notified_at'
UNION ALL
SELECT 'batches.notes', COUNT(*)
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'batches'
  AND COLUMN_NAME = 'notes'
UNION ALL
SELECT 'notifications table', COUNT(*)
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'notifications';
```

**Expected result:** All rows should show `1` in the `exists_count` column.

---

## Troubleshooting

### "Duplicate column name" error
**This is normal!** It means the column already exists. Skip to the next step.

### "Table already exists" error
**This is fine!** The notifications table is already created. You're good to go.

### "Access denied" error
Contact your database administrator or Kinsta support - you need ALTER TABLE permissions.

### Verification query shows `0` for a column
The migration for that column failed. Try running that specific ALTER TABLE command again and check for errors.

---

## After Migration

1. **Restart your application** (if deployed)
2. **Add KLAVIYO_API_KEY** to your environment variables
3. **Test the new features:**
   - Create a new batch
   - Add notes
   - Mark as picked up
   - Send notifications (if Klaviyo is configured)

---

## Need Help?

If you encounter issues:
1. Copy the exact error message
2. Note which step you're on
3. Check Kinsta documentation or contact support
