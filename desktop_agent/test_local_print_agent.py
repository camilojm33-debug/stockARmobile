import base64
from io import BytesIO

from PIL import Image

from local_print_agent import _build_ticket_bytes


def test_build_ticket_contains_sale_and_total():
    payload = _build_ticket_bytes(
        {
            "brand": "Mi Comercio",
            "sale_id": 520,
            "date": "2026-08-25 23:43",
            "customer": "Consumidor final",
            "payment_method": "Efectivo",
            "items": [
                {"name": "machimbre", "quantity": 1, "unit_price": 5600, "total": 5600},
                {"name": "coca", "quantity": 2, "unit_price": 5300, "total": 10600},
            ],
            "subtotal": 16200,
            "discount": 0,
            "surcharge": 0,
            "tax": 0,
            "total": 16200,
        }
    )
    assert b"Mi Comercio" in payload
    assert b"Venta: #520" in payload
    assert "machimbre: $5.600,00".encode("cp1252") in payload
    assert "2 x $5.300,00 = $10.600,00".encode("cp1252") in payload
    assert "TOTAL: $16.200,00".encode("cp1252") in payload
    assert payload.startswith(b"\x1b@")
    assert payload.endswith(b"\x1dV\x00")


def test_build_ticket_prints_branch_logo_as_escpos_raster():
    image = Image.new("RGB", (24, 12), "white")
    for x in range(5, 19):
        for y in range(3, 9):
            image.putpixel((x, y), (0, 0, 0))
    handle = BytesIO()
    image.save(handle, format="PNG")
    logo_url = "data:image/png;base64," + base64.b64encode(handle.getvalue()).decode("ascii")

    payload = _build_ticket_bytes({
        "brand": "Logo Test",
        "sale_id": 522,
        "logo_data_url": logo_url,
        "paper_size": "Ticket80",
        "items": [],
        "subtotal": 100,
        "discount": 0,
        "surcharge": 0,
        "tax": 0,
        "total": 100,
    })

    assert b"\x1dv0\x00" in payload
    assert b"Logo Test" in payload


def test_build_ticket_includes_originating_branch_details_and_footer():
    payload = _build_ticket_bytes(
        {
            "brand": "Sucursal Norte",
            "sale_id": 521,
            "date": "2026-11-09 14:30",
            "customer": "Consumidor final",
            "branch": "Sucursal Norte",
            "address": "Av. Norte 123, Fontana, Chaco",
            "phone": "3624111111",
            "whatsapp": "3624222222",
            "website": "www.comercio.test",
            "footer": "Gracias por comprar en Sucursal Norte",
            "items": [],
            "subtotal": 1000,
            "discount": 0,
            "surcharge": 0,
            "tax": 0,
            "total": 1000,
        }
    )

    decoded = payload.decode("cp1252")
    assert "Sucursal: Sucursal Norte" in decoded
    assert "Dirección: Av. Norte 123, Fontana, Chaco" in decoded
    assert "Teléfono: 3624111111" in decoded
    assert "WhatsApp: 3624222222" in decoded
    assert "Web: www.comercio.test" in decoded
    assert "Gracias por comprar en Sucursal Norte" in decoded
