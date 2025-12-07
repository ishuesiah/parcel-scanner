# rate_shopping.py
"""
Unified Rate Shopping Service
Compares shipping rates across UPS and Canada Post carriers.
"""

from typing import Dict, List, Any, Optional
from ups_api import get_ups_shipping_api, UPSShippingAPI
from canadapost_api import get_canadapost_shipping_api, CanadaPostShippingAPI


class RateShoppingService:
    """
    Unified rate shopping across multiple carriers.
    Compares UPS and Canada Post rates for a shipment.
    """

    def __init__(self, db_connection_func):
        """
        Initialize rate shopping service.

        Args:
            db_connection_func: Function that returns a database connection
        """
        self.get_db_connection = db_connection_func
        self.ups = get_ups_shipping_api()
        self.canada_post = get_canadapost_shipping_api()

    def get_customs_data_for_order(self, order_id: int) -> List[Dict]:
        """
        Get customs declaration data for an order's line items.
        Looks up HS codes from product_customs_info table or falls back to order_line_items.

        Returns list of customs items ready for carrier APIs.
        """
        conn = self.get_db_connection()
        try:
            cursor = conn.cursor()

            # Get line items with customs info - prefer product_customs_info if available
            cursor.execute("""
                SELECT
                    oli.sku,
                    oli.product_title,
                    oli.quantity,
                    oli.price,
                    oli.grams,
                    COALESCE(pci.customs_description, oli.customs_description, oli.product_title) as customs_description,
                    COALESCE(pci.hs_code, oli.hs_code, '4820102010') as hs_code,
                    COALESCE(pci.country_of_origin, oli.country_of_origin, 'CA') as country_of_origin,
                    COALESCE(pci.weight_grams, oli.grams, 200) as weight_grams
                FROM order_line_items oli
                LEFT JOIN product_customs_info pci ON pci.sku = oli.sku
                WHERE oli.order_id = %s
            """, (order_id,))

            items = []
            for row in cursor.fetchall():
                items.append({
                    "sku": row.get("sku") or "",
                    "description": (row.get("customs_description") or "Goods")[:35],  # Carrier limits
                    "hs_code": row.get("hs_code") or "4820102010",
                    "country_of_origin": row.get("country_of_origin") or "CA",
                    "quantity": row.get("quantity") or 1,
                    "value": float(row.get("price") or 0),
                    "weight_kg": (row.get("weight_grams") or 200) / 1000.0
                })

            cursor.close()
            return items

        finally:
            conn.close()

    def get_all_rates(
        self,
        destination: Dict[str, str],
        packages: List[Dict],
        customs_items: List[Dict] = None
    ) -> Dict[str, Any]:
        """
        Get rates from all carriers and combine results.

        Args:
            destination: {
                "name": "Customer Name",
                "address_line1": "123 Main St",
                "city": "New York",
                "state": "NY",
                "postal_code": "10001",
                "country_code": "US"
            }
            packages: [{
                "weight_kg": 0.5,
                "length_cm": 25,
                "width_cm": 18,
                "height_cm": 5
            }]
            customs_items: list of items (for international)

        Returns:
            {
                "success": True,
                "rates": [
                    {
                        "carrier": "Canada Post",
                        "service_code": "USA.XP",
                        "service_name": "Xpresspost USA",
                        "total_charge": 18.95,
                        "currency": "CAD",
                        "delivery_days": 3
                    },
                    {
                        "carrier": "UPS",
                        "service_code": "11",
                        "service_name": "UPS Standard",
                        "total_charge": 22.43,
                        "currency": "CAD",
                        "delivery_days": 5
                    },
                    ...
                ],
                "cheapest": {...},
                "fastest": {...}
            }
        """
        all_rates = []
        errors = []

        # Calculate total weight for Canada Post (they want single weight)
        total_weight_kg = sum(pkg.get("weight_kg", 0.5) for pkg in packages)

        # Get Canada Post rates
        if self.canada_post.rating_enabled:
            try:
                cp_result = self.canada_post.get_rates(
                    destination_postal=destination.get("postal_code", ""),
                    destination_country=destination.get("country_code", "CA"),
                    weight_kg=total_weight_kg,
                    dimensions_cm={
                        "length": packages[0].get("length_cm", 25) if packages else 25,
                        "width": packages[0].get("width_cm", 18) if packages else 18,
                        "height": packages[0].get("height_cm", 5) if packages else 5
                    },
                    customs_items=customs_items
                )

                if cp_result.get("success"):
                    for rate in cp_result.get("rates", []):
                        rate["carrier"] = "Canada Post"
                        all_rates.append(rate)
                else:
                    errors.append(f"Canada Post: {cp_result.get('error')}")

            except Exception as e:
                errors.append(f"Canada Post: {str(e)}")

        # Get UPS rates
        if self.ups.rating_enabled:
            try:
                ups_result = self.ups.get_rates(
                    destination=destination,
                    packages=packages,
                    customs_items=customs_items
                )

                if ups_result.get("success"):
                    for rate in ups_result.get("rates", []):
                        rate["carrier"] = "UPS"
                        all_rates.append(rate)
                else:
                    errors.append(f"UPS: {ups_result.get('error')}")

            except Exception as e:
                errors.append(f"UPS: {str(e)}")

        # Sort all rates by price
        all_rates.sort(key=lambda x: x["total_charge"])

        # Find cheapest and fastest
        cheapest = all_rates[0] if all_rates else None

        fastest = None
        for rate in all_rates:
            if rate.get("delivery_days"):
                if fastest is None or rate["delivery_days"] < fastest.get("delivery_days", 999):
                    fastest = rate

        return {
            "success": len(all_rates) > 0,
            "rates": all_rates,
            "cheapest": cheapest,
            "fastest": fastest,
            "errors": errors if errors else None
        }


# Singleton instance
_rate_shopping_service = None


def get_rate_shopping_service(db_connection_func) -> RateShoppingService:
    """Get or create singleton rate shopping service instance."""
    global _rate_shopping_service
    if _rate_shopping_service is None:
        _rate_shopping_service = RateShoppingService(db_connection_func)
    return _rate_shopping_service
