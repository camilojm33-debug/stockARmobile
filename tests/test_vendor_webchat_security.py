import app as stock_app

from ai_agents import _decode_public_vendor_token, _public_vendor_token


def test_public_vendor_token_is_signed_and_company_scoped():
    with stock_app.app.app_context():
        token = _public_vendor_token(12345)
        assert _decode_public_vendor_token(token) == 12345
        assert _decode_public_vendor_token(token + "tampered") is None
        other = _public_vendor_token(54321)
        assert _decode_public_vendor_token(other) == 54321
        assert _decode_public_vendor_token(other) != 12345
