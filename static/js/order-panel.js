/**
 * OrderPanel - Unified Order Detail Slide-Out Panel Controller
 *
 * Follows pragmatic programming principles:
 * - DRY: Single source of truth for order panel across all pages
 * - ETC: Easy to change - modify once, affects everywhere
 * - Orthogonality: Independent module, no coupling to page-specific code
 *
 * Supports real-time tracking updates via WebSocket.
 */
const OrderPanel = (function() {
    'use strict';

    // ─────────────────────────────────────────────────────
    // PRIVATE STATE
    // ─────────────────────────────────────────────────────
    let currentOrderNumber = null;
    let isOpen = false;
    let socket = null;
    let currentTrackingNumber = null;

    // DOM references (cached on init)
    let panelEl, overlayEl, contentEl, headerEl, footerActionsEl;

    // ─────────────────────────────────────────────────────
    // PUBLIC API
    // ─────────────────────────────────────────────────────

    function init() {
        panelEl = document.getElementById('orderDetailPanel');
        overlayEl = document.getElementById('orderPanelOverlay');
        contentEl = document.getElementById('orderPanelContent');
        headerEl = document.getElementById('orderPanelHeader');
        footerActionsEl = document.getElementById('panelFooterActions');

        if (!panelEl) return; // Panel not on this page

        // Keyboard escape to close
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape' && isOpen) close();
        });

        // Click outside to close
        if (overlayEl) {
            overlayEl.addEventListener('click', close);
        }

        // Initialize WebSocket for live tracking
        initWebSocket();
    }

    async function open(orderNumber) {
        if (!panelEl) {
            console.warn('OrderPanel: Panel element not found');
            return;
        }

        currentOrderNumber = orderNumber;
        isOpen = true;

        // Show panel with loading state
        panelEl.classList.add('open');
        if (overlayEl) overlayEl.classList.add('visible');
        document.body.classList.add('panel-open');

        if (headerEl) headerEl.textContent = 'Order #' + orderNumber;
        if (contentEl) contentEl.innerHTML = renderLoading();

        try {
            const response = await fetch('/api/orders/' + orderNumber + '/details');
            const data = await response.json();

            if (data.success) {
                if (contentEl) contentEl.innerHTML = renderOrderContent(data);

                // Subscribe to tracking updates if we have a tracking number
                if (data.shipment && data.shipment.tracking_number) {
                    currentTrackingNumber = data.shipment.tracking_number;
                    subscribeToTracking(currentTrackingNumber);
                }

                // Show/hide customs button based on zone
                const customsBtn = document.getElementById('panelCustomsBtn');
                if (customsBtn) {
                    customsBtn.style.display = data.rate_zone && data.rate_zone.is_international ? 'inline-flex' : 'none';
                }
            } else {
                if (contentEl) contentEl.innerHTML = renderError(data.error || 'Could not load order');
            }
        } catch (err) {
            console.error('OrderPanel: Error loading order', err);
            if (contentEl) contentEl.innerHTML = renderError(err.message);
        }
    }

    function close() {
        isOpen = false;
        currentOrderNumber = null;
        currentTrackingNumber = null;

        if (panelEl) panelEl.classList.remove('open');
        if (overlayEl) overlayEl.classList.remove('visible');
        document.body.classList.remove('panel-open');
    }

    // ─────────────────────────────────────────────────────
    // RENDERING FUNCTIONS (Orthogonal - each handles one section)
    // ─────────────────────────────────────────────────────

    function renderLoading() {
        return '<div class="panel-loading"><div class="spinner"></div><p>Loading order details...</p></div>';
    }

    function renderError(message) {
        return '<div class="panel-error"><p>Error: ' + escapeHtml(message) + '</p></div>';
    }

    function renderOrderContent(data) {
        const sections = [];

        // Tracking section (if shipped)
        sections.push(renderTrackingSection(data.tracking, data.shipment));

        // Customer section
        sections.push(renderCustomerSection(data.order));

        // Address section
        sections.push(renderAddressSection(data.order.shipping_address, data.rate_zone));

        // Items section
        sections.push(renderItemsSection(data.line_items, data.rate_zone && data.rate_zone.is_international));

        // Totals section
        sections.push(renderTotalsSection(data.order));

        // Notes section (if exists)
        if (data.order.note) {
            sections.push(renderNotesSection(data.order.note));
        }

        return '<div class="order-panel-sections">' + sections.join('') + '</div>';
    }

    function renderTrackingSection(tracking, shipment) {
        if (!shipment || !shipment.tracking_number) {
            return '<section class="panel-section tracking-section no-tracking">' +
                '<h4>Shipping</h4>' +
                '<p class="muted">Not yet shipped</p>' +
                '</section>';
        }

        const statusClass = tracking ? tracking.status : 'unknown';
        const progressPercent = tracking ? tracking.progress_percent : 0;
        const trackingUrl = getTrackingUrl(shipment.tracking_number, shipment.carrier_code);

        return '<section class="panel-section tracking-section" id="trackingSection">' +
            '<h4>Live Tracking</h4>' +
            '<div class="tracking-card status-' + statusClass + '">' +
                '<div class="tracking-header">' +
                    '<span class="carrier-badge">' + escapeHtml(shipment.carrier || 'Unknown') + '</span>' +
                    '<a href="' + trackingUrl + '" target="_blank" class="tracking-number">' +
                        escapeHtml(shipment.tracking_number) +
                    '</a>' +
                '</div>' +
                '<div class="tracking-status">' +
                    '<span class="status-badge status-' + statusClass + '" id="trackingStatusBadge">' +
                        escapeHtml(tracking ? tracking.status_text : 'Unknown') +
                    '</span>' +
                    '<div class="progress-bar">' +
                        '<div class="progress-fill" id="trackingProgress" style="width: ' + progressPercent + '%"></div>' +
                    '</div>' +
                '</div>' +
                '<div class="tracking-details">' +
                    (tracking && tracking.estimated_delivery ?
                        '<div class="detail"><span class="label">Est. Delivery:</span> ' + escapeHtml(tracking.estimated_delivery) + '</div>' : '') +
                    (tracking && tracking.last_location ?
                        '<div class="detail"><span class="label">Last Location:</span> ' + escapeHtml(tracking.last_location) + '</div>' : '') +
                    (shipment.ship_date ?
                        '<div class="detail"><span class="label">Ship Date:</span> ' + escapeHtml(shipment.ship_date) + '</div>' : '') +
                '</div>' +
            '</div>' +
            '</section>';
    }

    function renderCustomerSection(order) {
        return '<section class="panel-section">' +
            '<h4>Customer</h4>' +
            '<div class="customer-info">' +
                '<p class="customer-name">' + escapeHtml(order.customer_name || 'Unknown') + '</p>' +
                (order.customer_email ? '<p class="customer-email">' + escapeHtml(order.customer_email) + '</p>' : '') +
                (order.customer_phone ? '<p class="customer-phone">' + escapeHtml(order.customer_phone) + '</p>' : '') +
            '</div>' +
            '</section>';
    }

    function renderAddressSection(address, rateZone) {
        if (!address || Object.keys(address).length === 0) {
            return '<section class="panel-section"><h4>Shipping Address</h4><p class="muted">No address</p></section>';
        }

        const zoneBadge = rateZone ?
            '<span class="zone-badge zone-' + (rateZone.is_international ? 'intl' : 'domestic') + '">' +
                escapeHtml(rateZone.zone) +
            '</span>' : '';

        const lines = [];
        if (address.name) lines.push(escapeHtml(address.name));
        if (address.company) lines.push(escapeHtml(address.company));
        if (address.address1) lines.push(escapeHtml(address.address1));
        if (address.address2) lines.push(escapeHtml(address.address2));

        const cityLine = [address.city, address.province_code || address.province, address.zip]
            .filter(Boolean).map(escapeHtml).join(', ');
        if (cityLine) lines.push(cityLine);

        if (address.country) lines.push(escapeHtml(address.country));

        return '<section class="panel-section">' +
            '<h4>Shipping Address ' + zoneBadge + '</h4>' +
            '<div class="address-block">' + lines.join('<br>') + '</div>' +
            '</section>';
    }

    function renderItemsSection(items, isInternational) {
        if (!items || items.length === 0) {
            return '<section class="panel-section"><h4>Items</h4><p class="muted">No items</p></section>';
        }

        let itemsHtml = '<div class="items-list">';
        let totalQty = 0;

        items.forEach(function(item) {
            totalQty += item.quantity || 1;

            let propsHtml = '';
            if (item.properties && item.properties.length > 0) {
                propsHtml = '<div class="item-properties">';
                item.properties.forEach(function(p) {
                    if (p.name && !p.name.startsWith('_')) {
                        propsHtml += '<span class="prop">' + escapeHtml(p.name) + ': ' + escapeHtml(p.value) + '</span>';
                    }
                });
                propsHtml += '</div>';
            }

            let customsHtml = '';
            if (isInternational && (item.hs_code || item.customs_description)) {
                customsHtml = '<div class="item-customs">' +
                    (item.hs_code ? '<span class="hs-code">HS: ' + escapeHtml(item.hs_code) + '</span>' : '') +
                    (item.country_of_origin ? '<span class="origin">Origin: ' + escapeHtml(item.country_of_origin) + '</span>' : '') +
                    '</div>';
            }

            itemsHtml += '<div class="item-row">' +
                '<div class="item-qty">' + (item.quantity || 1) + 'x</div>' +
                '<div class="item-details">' +
                    '<div class="item-title">' + escapeHtml(item.title || 'Unknown Item') + '</div>' +
                    (item.variant ? '<div class="item-variant">' + escapeHtml(item.variant) + '</div>' : '') +
                    (item.sku ? '<div class="item-sku">SKU: ' + escapeHtml(item.sku) + '</div>' : '') +
                    propsHtml +
                    customsHtml +
                '</div>' +
                '<div class="item-price">$' + (item.price || 0).toFixed(2) + '</div>' +
            '</div>';
        });

        itemsHtml += '</div>';

        return '<section class="panel-section">' +
            '<h4>Items (' + totalQty + ')</h4>' +
            itemsHtml +
            '</section>';
    }

    function renderTotalsSection(order) {
        const currency = order.currency || 'CAD';

        return '<section class="panel-section totals-section">' +
            '<h4>Order Total</h4>' +
            '<div class="totals-grid">' +
                '<div class="total-row"><span>Subtotal</span><span>$' + (order.subtotal_price || 0).toFixed(2) + ' ' + currency + '</span></div>' +
                '<div class="total-row"><span>Tax</span><span>$' + (order.total_tax || 0).toFixed(2) + ' ' + currency + '</span></div>' +
                '<div class="total-row total-final"><span>Total</span><span>$' + (order.total_price || 0).toFixed(2) + ' ' + currency + '</span></div>' +
            '</div>' +
            '</section>';
    }

    function renderNotesSection(note) {
        return '<section class="panel-section notes-section">' +
            '<h4>Order Notes</h4>' +
            '<div class="note-content">' + escapeHtml(note) + '</div>' +
            '</section>';
    }

    // ─────────────────────────────────────────────────────
    // WEBSOCKET - Real-time tracking updates
    // ─────────────────────────────────────────────────────

    function initWebSocket() {
        if (typeof io === 'undefined') return;

        try {
            socket = io({ transports: ['websocket', 'polling'] });

            socket.on('tracking_update', function(data) {
                if (isOpen && currentTrackingNumber && data.tracking_number === currentTrackingNumber) {
                    updateTrackingDisplay(data);
                }
            });

            socket.on('connect', function() {
                console.log('OrderPanel: WebSocket connected');
            });
        } catch (err) {
            console.warn('OrderPanel: WebSocket init failed', err);
        }
    }

    function subscribeToTracking(trackingNumber) {
        if (socket && trackingNumber) {
            socket.emit('subscribe_tracking', {
                tracking_numbers: [trackingNumber]
            });
        }
    }

    function updateTrackingDisplay(data) {
        const section = document.getElementById('trackingSection');
        if (!section) return;

        // Animate update
        section.classList.add('updating');

        // Update status badge
        const badge = document.getElementById('trackingStatusBadge');
        if (badge) {
            badge.className = 'status-badge status-' + (data.status || 'unknown');
            badge.textContent = data.status_text || data.status || 'Unknown';
        }

        // Update progress bar
        const fill = document.getElementById('trackingProgress');
        if (fill) {
            const percent = {
                'delivered': 100, 'out_for_delivery': 90, 'almost_there': 85,
                'in_transit': 50, 'label_created': 10, 'exception': 50
            }[data.status] || 0;
            fill.style.width = percent + '%';
        }

        // Update tracking card class
        const card = section.querySelector('.tracking-card');
        if (card) {
            card.className = 'tracking-card status-' + (data.status || 'unknown');
        }

        setTimeout(function() {
            section.classList.remove('updating');
        }, 500);
    }

    // ─────────────────────────────────────────────────────
    // UTILITY FUNCTIONS
    // ─────────────────────────────────────────────────────

    function getTrackingUrl(tracking, carrier) {
        if (!tracking) return '#';

        const c = (carrier || '').toLowerCase();
        if (c === 'ups' || tracking.startsWith('1Z')) {
            return 'https://www.ups.com/track?tracknum=' + encodeURIComponent(tracking);
        } else if (c.includes('fedex')) {
            return 'https://www.fedex.com/fedextrack/?trknbr=' + encodeURIComponent(tracking);
        } else if (c.includes('canada')) {
            return 'https://www.canadapost-postescanada.ca/track-reperage/en#/search?searchFor=' + encodeURIComponent(tracking);
        }
        return 'https://www.google.com/search?q=' + encodeURIComponent(tracking + ' tracking');
    }

    function escapeHtml(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // ─────────────────────────────────────────────────────
    // ACTION FUNCTIONS (for footer buttons)
    // ─────────────────────────────────────────────────────

    function printPackingSlip() {
        if (!currentOrderNumber) return;
        window.open('/api/orders/' + currentOrderNumber + '/packing-slip', '_blank');
    }

    function printCustomsForm() {
        if (!currentOrderNumber) return;
        window.open('/api/orders/' + currentOrderNumber + '/customs-form', '_blank');
    }

    // ─────────────────────────────────────────────────────
    // EXPOSE PUBLIC API
    // ─────────────────────────────────────────────────────

    return {
        init: init,
        open: open,
        close: close,
        isOpen: function() { return isOpen; },
        getCurrentOrder: function() { return currentOrderNumber; },
        printPackingSlip: printPackingSlip,
        printCustomsForm: printCustomsForm
    };
})();

// Auto-initialize on DOMContentLoaded
document.addEventListener('DOMContentLoaded', OrderPanel.init);

// Global function for backwards compatibility with existing onclick handlers
function showOrderDetails(orderNumber) {
    OrderPanel.open(orderNumber);
}

function closeOrderModal() {
    OrderPanel.close();
}
