from pathlib import Path


def test_public_vendor_chat_turns_payment_urls_into_clickable_links():
    template = Path("templates/ai_agents/public_vendor_chat.html").read_text(encoding="utf-8")

    assert "function renderMessageContent" in template
    assert "link.target = '_blank'" in template
    assert "noopener noreferrer" in template
    assert "Pagar ahora con Mercado Pago" in template
