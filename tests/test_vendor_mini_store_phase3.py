import json
from types import SimpleNamespace

from services.ai_agent import vendor_publication


def test_product_payload_exposes_only_customer_facing_fields():
    product = SimpleNamespace(
        id=7,
        name="Producto demo",
        description="Descripción pública",
        category="Bebidas",
        brand="Marca",
        photo="/static/uploads/demo.jpg",
        unit_measure="u",
        price=1250,
        stock=4,
        discount=50,
        favorite=True,
        cost_price=900,
        supplier="Proveedor secreto",
    )
    payload = vendor_publication._product_payload(product)
    assert payload["id"] == 7
    assert payload["name"] == "Producto demo"
    assert payload["price"] == 1250.0
    assert payload["stock"] == 4.0
    assert payload["available"] is True
    assert "cost_price" not in payload
    assert "supplier" not in payload


def test_public_conversation_metadata_accepts_dict_and_json():
    conversation = SimpleNamespace(metadata_json={"source": "public_webchat_stable", "value": 1})
    assert vendor_publication._conversation_metadata(conversation)["value"] == 1

    conversation.metadata_json = json.dumps({"source": "public_webchat_stable", "value": 2})
    assert vendor_publication._conversation_metadata(conversation)["value"] == 2


def test_public_conversation_metadata_ignores_invalid_json():
    conversation = SimpleNamespace(metadata_json="{invalid")
    assert vendor_publication._conversation_metadata(conversation) == {}
