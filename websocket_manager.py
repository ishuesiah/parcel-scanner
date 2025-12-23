"""
WebSocket Manager for Real-Time Tracking Updates.

Decoupled from Flask routes to maintain ETC (Easier To Change) principle.
Implements security best practices for WebSocket connections.

Security features:
- Session-based authentication
- Rate limiting per session
- Connection limiting per IP
- Room-based isolation
- Input validation
- CORS protection
"""

from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect
from flask import session, request
from functools import wraps
from collections import defaultdict
from threading import Lock
from datetime import datetime
import time
import re
import os
import logging

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# SocketIO Instance (Singleton)
# ══════════════════════════════════════════════════════════════════════════════

socketio = None
_init_lock = Lock()


def get_allowed_origins():
    """
    Get list of allowed origins for WebSocket connections.
    Only allow your own domains - never use '*'.
    """
    app_url = os.environ.get('APP_URL', '').rstrip('/')
    allowed = []

    if app_url:
        allowed.append(app_url)
        # Also allow https version if http provided
        if app_url.startswith('http://'):
            allowed.append(app_url.replace('http://', 'https://'))

    # Development origins
    flask_env = os.environ.get('FLASK_ENV', 'production')
    if flask_env == 'development':
        allowed.extend([
            'http://localhost:5005',
            'http://127.0.0.1:5005',
            'http://localhost:5000',
            'http://127.0.0.1:5000',
        ])

    # If no origins configured, allow same-origin only
    if not allowed:
        allowed = None  # SocketIO will default to same-origin

    return allowed


def init_socketio(app):
    """
    Initialize SocketIO with the Flask app.
    Thread-safe singleton pattern.
    """
    global socketio
    with _init_lock:
        if socketio is None:
            socketio = SocketIO(
                app,
                cors_allowed_origins=get_allowed_origins(),
                manage_session=True,
                async_mode='threading',  # Works with existing Flask setup
                logger=False,
                engineio_logger=False,
                ping_timeout=60,
                ping_interval=25,
                max_http_buffer_size=100000,  # 100KB max message size
            )
            logger.info("WebSocket server initialized")
    return socketio


def get_socketio():
    """Get the SocketIO instance."""
    return socketio


# ══════════════════════════════════════════════════════════════════════════════
# Room Naming Conventions
# ══════════════════════════════════════════════════════════════════════════════

class TrackingRooms:
    """
    Room naming conventions for targeted broadcasts.
    Rooms enable efficient message routing without client list management.
    """

    @staticmethod
    def tracking_number(tracking_num: str) -> str:
        """Room for specific tracking number updates."""
        return f"tracking:{tracking_num}"

    @staticmethod
    def batch(batch_id: int) -> str:
        """Room for all scans in a batch."""
        return f"batch:{batch_id}"

    @staticmethod
    def shipments_page() -> str:
        """Room for Live Tracking / Check Shipments page."""
        return "page:shipments"

    @staticmethod
    def tracking_group(group_id: int) -> str:
        """Room for tracking group updates."""
        return f"group:{group_id}"


class TrackingEvents:
    """Event names for consistency (prevents typos)."""
    TRACKING_UPDATE = 'tracking_update'
    BATCH_SCAN_UPDATE = 'batch_scan_update'
    BATCH_SCAN_MOVED = 'batch_scan_moved'
    CONNECTION_SUCCESS = 'connection_success'
    SUBSCRIPTION_CONFIRMED = 'subscription_confirmed'
    ERROR = 'error'
    RATE_LIMITED = 'rate_limit_exceeded'


# ══════════════════════════════════════════════════════════════════════════════
# Rate Limiting (Custom for WebSocket)
# ══════════════════════════════════════════════════════════════════════════════

class WebSocketRateLimiter:
    """
    Rate limiter for WebSocket events.
    Flask-Limiter doesn't work with SocketIO events.
    """

    def __init__(self):
        self.events = defaultdict(lambda: defaultdict(list))
        self.lock = Lock()
        self.cleanup_interval = 300
        self.last_cleanup = time.time()

    def is_allowed(self, session_id: str, event_name: str,
                   max_requests: int = 60, window_seconds: int = 60) -> bool:
        """Check if request is within rate limit."""
        now = time.time()
        cutoff = now - window_seconds

        with self.lock:
            timestamps = self.events[session_id][event_name]
            timestamps = [ts for ts in timestamps if ts > cutoff]
            self.events[session_id][event_name] = timestamps

            if len(timestamps) >= max_requests:
                return False

            timestamps.append(now)

            if now - self.last_cleanup > self.cleanup_interval:
                self._cleanup()
                self.last_cleanup = now

        return True

    def _cleanup(self):
        """Remove old entries to prevent memory growth."""
        now = time.time()
        cutoff = now - 3600

        for session_id in list(self.events.keys()):
            for event_name in list(self.events[session_id].keys()):
                timestamps = self.events[session_id][event_name]
                timestamps = [ts for ts in timestamps if ts > cutoff]

                if timestamps:
                    self.events[session_id][event_name] = timestamps
                else:
                    del self.events[session_id][event_name]

            if not self.events[session_id]:
                del self.events[session_id]


ws_rate_limiter = WebSocketRateLimiter()


# ══════════════════════════════════════════════════════════════════════════════
# Connection Limiting (DoS Protection)
# ══════════════════════════════════════════════════════════════════════════════

class ConnectionLimiter:
    """Limit WebSocket connections per IP address."""

    def __init__(self, max_connections_per_ip=10):
        self.max_connections_per_ip = max_connections_per_ip
        self.connections = defaultdict(int)
        self.lock = Lock()

    def can_connect(self, ip_address: str) -> bool:
        """Check if IP can open a new connection."""
        with self.lock:
            return self.connections.get(ip_address, 0) < self.max_connections_per_ip

    def add_connection(self, ip_address: str):
        """Register a new connection."""
        with self.lock:
            self.connections[ip_address] += 1

    def remove_connection(self, ip_address: str):
        """Remove a connection."""
        with self.lock:
            if ip_address in self.connections:
                self.connections[ip_address] -= 1
                if self.connections[ip_address] <= 0:
                    del self.connections[ip_address]

    def get_count(self, ip_address: str) -> int:
        """Get current connection count for IP."""
        with self.lock:
            return self.connections.get(ip_address, 0)


connection_limiter = ConnectionLimiter(max_connections_per_ip=15)


# ══════════════════════════════════════════════════════════════════════════════
# Security Decorators
# ══════════════════════════════════════════════════════════════════════════════

def get_client_ip():
    """Get client IP, accounting for proxies."""
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr


def socketio_login_required(f):
    """Decorator to require authentication for WebSocket events."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("authenticated"):
            emit(TrackingEvents.ERROR, {'message': 'Authentication required'})
            disconnect()
            return None

        # Update last activity
        session["last_active"] = time.time()
        return f(*args, **kwargs)
    return decorated_function


def socketio_rate_limit(max_requests=60, window_seconds=60):
    """Decorator for rate limiting WebSocket events."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            session_id = request.sid
            event_name = f.__name__

            if not ws_rate_limiter.is_allowed(session_id, event_name,
                                               max_requests, window_seconds):
                emit(TrackingEvents.RATE_LIMITED, {
                    'message': f'Rate limit exceeded. Max {max_requests} per {window_seconds}s.',
                    'retry_after': window_seconds
                })
                logger.warning(f"Rate limit exceeded for {session_id} on {event_name}")
                return None

            return f(*args, **kwargs)
        return wrapper
    return decorator


# ══════════════════════════════════════════════════════════════════════════════
# Input Validation
# ══════════════════════════════════════════════════════════════════════════════

def validate_tracking_number(tracking: str) -> tuple:
    """
    Validate tracking number format.

    Returns:
        (is_valid, sanitized_tracking, error_message)
    """
    if not tracking:
        return False, "", "Tracking number is required"

    if not isinstance(tracking, str):
        return False, "", "Invalid tracking number format"

    # Sanitize: remove whitespace and special chars, uppercase
    sanitized = re.sub(r'[^\w-]', '', tracking.strip().upper())

    # Length limits (prevent DoS)
    if len(sanitized) < 8:
        return False, "", "Tracking number too short"

    if len(sanitized) > 40:
        return False, "", "Tracking number too long"

    # UPS: 1Z + 16 alphanumeric = 18 chars
    if sanitized.startswith("1Z"):
        if len(sanitized) == 18 and sanitized[2:].isalnum():
            return True, sanitized, ""
        return False, "", "Invalid UPS tracking number format"

    # Canada Post: 16 digits
    if sanitized.isdigit() and len(sanitized) == 16:
        return True, sanitized, ""

    # Generic: alphanumeric, reasonable length
    if sanitized.isalnum() and 8 <= len(sanitized) <= 40:
        return True, sanitized, ""

    return False, "", "Unrecognized tracking number format"


def validate_batch_id(batch_id) -> tuple:
    """Validate batch ID is a positive integer."""
    try:
        batch_id = int(batch_id)
        if batch_id > 0:
            return True, batch_id, ""
        return False, None, "Invalid batch ID"
    except (TypeError, ValueError):
        return False, None, "Invalid batch ID format"


# ══════════════════════════════════════════════════════════════════════════════
# Broadcast Functions (called from background jobs)
# ══════════════════════════════════════════════════════════════════════════════

def broadcast_tracking_update(tracking_number: str, status_data: dict):
    """
    Broadcast tracking status update to subscribed clients.
    Called from background jobs when tracking cache is updated.
    """
    sio = get_socketio()
    if not sio:
        return

    room = TrackingRooms.tracking_number(tracking_number)

    # Sanitize data before broadcast
    safe_data = {
        'tracking_number': tracking_number,
        'status': status_data.get('status'),
        'status_text': status_data.get('status_text') or status_data.get('status_description'),
        'last_location': status_data.get('last_location'),
        'estimated_delivery': status_data.get('estimated_delivery'),
        'is_delivered': status_data.get('is_delivered', False),
        'updated_at': datetime.utcnow().isoformat()
    }

    sio.emit(TrackingEvents.TRACKING_UPDATE, safe_data, room=room, namespace='/')

    # Also emit to shipments page room
    sio.emit(TrackingEvents.TRACKING_UPDATE, safe_data,
             room=TrackingRooms.shipments_page(), namespace='/')


def broadcast_batch_scan_update(batch_id: int, scan_data: dict, action: str = 'update'):
    """
    Broadcast scan update to clients viewing a batch.
    Called when scan is added, updated, or moved.
    """
    sio = get_socketio()
    if not sio:
        return

    room = TrackingRooms.batch(batch_id)

    safe_data = {
        'batch_id': batch_id,
        'action': action,
        'scan': {
            'id': scan_data.get('id'),
            'tracking_number': scan_data.get('tracking_number'),
            'carrier': scan_data.get('carrier'),
            'order_number': scan_data.get('order_number'),
            'customer_name': scan_data.get('customer_name'),
            'customer_email': scan_data.get('customer_email'),
            'status': scan_data.get('status'),
        },
        'timestamp': datetime.utcnow().isoformat()
    }

    sio.emit(TrackingEvents.BATCH_SCAN_UPDATE, safe_data, room=room, namespace='/')


def broadcast_scans_moved(source_batch_id: int, target_batch_id: int,
                          scan_ids: list, moved_count: int):
    """Broadcast when scans are moved between batches."""
    sio = get_socketio()
    if not sio:
        return

    # Notify source batch
    sio.emit(TrackingEvents.BATCH_SCAN_MOVED, {
        'source_batch_id': source_batch_id,
        'target_batch_id': target_batch_id,
        'scan_ids': scan_ids,
        'moved_count': moved_count,
        'action': 'removed'
    }, room=TrackingRooms.batch(source_batch_id), namespace='/')

    # Notify target batch
    sio.emit(TrackingEvents.BATCH_SCAN_MOVED, {
        'source_batch_id': source_batch_id,
        'target_batch_id': target_batch_id,
        'scan_ids': scan_ids,
        'moved_count': moved_count,
        'action': 'added'
    }, room=TrackingRooms.batch(target_batch_id), namespace='/')
