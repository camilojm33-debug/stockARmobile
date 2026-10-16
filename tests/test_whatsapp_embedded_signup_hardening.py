"""Regression guards for the WhatsApp Embedded Signup onboarding flow."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_embedded_signup_ui_treats_meta_messages_as_untrusted():
    template = (ROOT / "templates/ai_agents/whatsapp_onboarding.html").read_text(encoding="utf-8")

    assert "function isTrustedMetaOrigin(origin)" in template
    assert "parsed.protocol === 'https:'" in template
    assert "host === 'facebook.com' || host.endsWith('.facebook.com')" in template
    assert "if (!isTrustedMetaOrigin(event.origin)) return;" in template
    assert "statusBox.textContent = String(message || '').slice(0, 500);" in template
    assert "statusBox.innerHTML" not in template
    assert "event.origin.endsWith('facebook.com')" not in template
    assert "function isMetaId(value)" in template


def test_whatsapp_onboarding_routes_require_company_admin():
    source = (ROOT / "ai_agents.py").read_text(encoding="utf-8")

    connect_route = source[source.index('@bp.get("/vendedor/conectar-whatsapp")'):source.index("def vendor_whatsapp_connect():")]
    complete_route = source[source.index('@bp.post("/vendedor/conectar-whatsapp/complete")'):source.index("def vendor_whatsapp_complete():")]

    assert "@tenant_required" in connect_route
    assert "@company_admin_required" in connect_route
    assert "@tenant_required" in complete_route
    assert "@company_admin_required" in complete_route


def test_onboarding_checks_all_server_side_meta_configuration_without_exposing_secrets():
    source = (ROOT / "ai_agents.py").read_text(encoding="utf-8")
    template = (ROOT / "templates/ai_agents/whatsapp_onboarding.html").read_text(encoding="utf-8")

    connect_start = source.index('@bp.get("/vendedor/conectar-whatsapp")')
    complete_start = source.index('@bp.post("/vendedor/conectar-whatsapp/complete")', connect_start)
    connect_route = source[connect_start:complete_start]

    assert 'app_secret = (os.getenv("META_APP_SECRET") or os.getenv("WHATSAPP_APP_SECRET") or "").strip()' in source
    assert "onboarding_ready = bool(app_id and config_id and app_secret)" in source
    assert "onboarding_ready=onboarding_ready" in connect_route
    assert "connected=connected" in connect_route
    assert "whatsapp=safe_whatsapp" in connect_route
    assert "whatsapp=whatsapp," not in connect_route
    assert "{% if onboarding_ready and not connected %}" in template


def test_meta_ids_are_validated_before_becoming_graph_api_path_segments():
    source = (ROOT / "ai_agents.py").read_text(encoding="utf-8")
    assert "\\nimport re\\n" in source.split("from itsdangerous", 1)[0]
    start = source.index("def vendor_whatsapp_complete():")
    end = source.index('@bp.get("/public/vendedor/<token>")', start)
    route = source[start:end]

    assert 're.fullmatch(r"\\d{1,30}", waba_id)' in route
    assert 're.fullmatch(r"\\d{1,30}", phone_number_id)' in route
    assert "len(code) > 4096" in route
    assert "business_id = \"\"" in route


def test_whatsapp_number_conflict_is_rejected_before_meta_side_effects():
    source = (ROOT / "ai_agents.py").read_text(encoding="utf-8")
    start = source.index("def vendor_whatsapp_complete():")
    end = source.index('@bp.get("/public/vendedor/<token>")', start)
    route = source[start:end]

    assert "company_for_whatsapp_phone_id(phone_number_id)" in route
    assert "int(existing_company.id) != int(company.id)" in route
    assert "WHATSAPP_COMMERCIAL_PHONE_NUMBER_ID" in route
    assert "Este número de WhatsApp ya está conectado a otra empresa" in route
    assert route.index("existing_company = company_for_whatsapp_phone_id(phone_number_id)") < route.index("subscribe = requests.post(")
    assert "}), 409" in route

 
def test_webhook_signature_uses_same_meta_secret_alias_as_embedded_signup():
    source = (ROOT / "whatsapp_agent.py").read_text(encoding="utf-8")
    assert 'os.getenv("META_APP_SECRET") or os.getenv("WHATSAPP_APP_SECRET")' in source
    assert 'if not app_secret:\n        return not bool(current_app.config.get("IS_PRODUCTION_ENV"))' in source


def test_embedded_signup_launch_uses_v4_and_offers_business_app_coexistence():
    template = (ROOT / "templates/ai_agents/whatsapp_onboarding.html").read_text(encoding="utf-8")

    assert "config_id: configId" in template
    assert "extras: {setup: {}, featureType: 'whatsapp_business_app_onboarding', sessionInfoVersion: '3'}" in template
    assert "onboardingEvent = eventName" in template
    assert "phone_number_id: sessionInfo.phone_number_id || ''" in template
    assert "sessionInfoVersion: '3'" in template
    assert "version: 'v3'" not in template


def test_embedded_signup_completion_resolves_missing_phone_id_server_side_and_skips_register_for_coexistence():
    source = (ROOT / "ai_agents.py").read_text(encoding="utf-8")
    start = source.index('@bp.post("/vendedor/conectar-whatsapp/complete")')
    end = source.index('@bp.get("/public/vendedor/<token>")', start)
    route = source[start:end]

    assert 'onboarding_event == "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"' in route
    assert 'if not code or not waba_id:' in route
    assert 'f"{waba_id}/phone_numbers"' in route
    assert 'if len(matching_phones) != 1:' in route
    assert 'if not is_coexistence and status not in {"CONNECTED"}' in route


def test_vendor_dashboard_connection_badge_requires_decryptable_access_token():
    source = (ROOT / "ai_agents.py").read_text(encoding="utf-8")
    template = (ROOT / "templates/ai_agents/index.html").read_text(encoding="utf-8")
    assert 'whatsapp_connection.get("access_token")' in source
    assert '"whatsapp_connected": whatsapp_connected' in source
    assert "{{ 'Conectado' if whatsapp_connected else 'No conectado' }}" in template
