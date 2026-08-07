"""Discount helper facade for sales domain."""


class DiscountService:
    @staticmethod
    def line_discount_override(discount_overrides, product_id):
        if not discount_overrides:
            return None
        override_value = discount_overrides.get(str(product_id))
        if override_value is None:
            override_value = discount_overrides.get(int(product_id))
        return override_value
