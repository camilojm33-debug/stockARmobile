"""Inventory calculations and stock consistency for sales."""

from stockarmobile.helpers.money import safe_decimal
from stockarmobile.helpers.numbers import safe_float

from .discount_service import DiscountService


class InventoryService:
    @staticmethod
    def calculate_lines(*, items, current_user, fetch_products_func, discount_overrides=None):
        lines = []
        product_ids = sorted(int(prod_id) for prod_id in items.keys())
        products = fetch_products_func(product_ids)

        for prod_id, qty in items.items():
            product = products.get(int(prod_id))
            if not product:
                raise ValueError("Producto no encontrado.")
            if int(getattr(product, "company_id", 0) or 0) != int(getattr(current_user, "company_id", 0) or -1):
                raise ValueError("Producto fuera del contexto de empresa.")
            qty = safe_float(qty)
            if qty <= 0:
                raise ValueError("La cantidad debe ser mayor a cero.")
            if float(product.stock or 0) < qty:
                raise ValueError(f"Stock insuficiente para {product.name}. Disponible: {product.stock:g} {product.unit_measure or ''}.")

            quantity_dec = safe_decimal(qty)
            unit_price = safe_decimal(product.price)
            unit_discount = safe_decimal(product.discount)
            override_value = DiscountService.line_discount_override(discount_overrides, prod_id)
            if override_value is not None:
                unit_discount = safe_decimal(override_value)

            line_subtotal = unit_price * quantity_dec
            line_discount = min(unit_discount * quantity_dec, line_subtotal)
            lines.append({"product": product, "quantity": qty, "price": unit_price, "discount": line_discount})
        return lines

    @staticmethod
    def reverse_stock(*, sale_items, resolve_product_func):
        for item in sale_items:
            product = resolve_product_func(item)
            if product is not None:
                product.stock = float(product.stock or 0) + float(item.quantity or 0)

    @staticmethod
    def apply_stock(*, lines):
        for line in lines:
            product = line["product"]
            quantity = line["quantity"]
            product.stock = float(product.stock or 0) - float(quantity)

    @staticmethod
    def build_edit_lines(
        *,
        product_ids,
        quantities,
        prices,
        discounts,
        row_deletes,
        products_by_id,
        to_decimal,
        is_truthy,
    ):
        new_lines = []
        row_count = max(len(product_ids), len(quantities), len(prices), len(discounts), len(row_deletes))
        for index in range(row_count):
            if index < len(row_deletes) and is_truthy(row_deletes[index]):
                continue

            raw_product_id = product_ids[index] if index < len(product_ids) else ""
            raw_quantity = quantities[index] if index < len(quantities) else ""
            raw_price = prices[index] if index < len(prices) else ""
            raw_discount = discounts[index] if index < len(discounts) else ""
            if not (raw_product_id or raw_quantity or raw_price or raw_discount):
                continue
            if not raw_product_id:
                raise ValueError("Cada linea debe tener un producto seleccionado.")

            product = products_by_id.get(int(raw_product_id))
            if product is None:
                raise ValueError("Producto inválido para esta empresa.")

            quantity = to_decimal(raw_quantity)
            if quantity <= 0:
                raise ValueError("La cantidad debe ser mayor a cero.")
            price = to_decimal(raw_price)
            if price < 0:
                raise ValueError("El precio no puede ser negativo.")
            line_discount = to_decimal(raw_discount)
            if line_discount < 0:
                raise ValueError("El descuento no puede ser negativo.")

            gross = price * quantity
            if line_discount > gross:
                line_discount = gross
            if float(product.stock or 0) < float(quantity):
                raise ValueError(f"Stock insuficiente para {product.name}. Disponible: {product.stock:g} {product.unit_measure or ''}.")

            new_lines.append({"product": product, "quantity": quantity, "price": price, "discount": line_discount})
        return new_lines
