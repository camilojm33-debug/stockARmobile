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
    assert "\nimport re\n" in source.split("from itsdangerous", 1)[0]
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


def test_embedded_signup_launch_uses_meta_config_and_preserves_coexistence():
    template = (ROOT / "templates/ai_agents/whatsapp_onboarding.html").read_text(encoding="utf-8")

    # Embedded Signup v4 is selected by the Facebook Login for Business configuration
    # in Meta, not by adding a version field to FB.login extras.
    assert "config_id: configId" in template
    assert "version: 'v3'" not in template
    assert "version: 'v4'" not in template
    # Coexistence still uses this feature type and session callback version.
    assert "extras: {setup: {}, featureType: 'whatsapp_business_app_onboarding', sessionInfoVersion: '3'}" in template
    assert "onboardingEvent = eventName" in template
    assert "phone_number_id: sessionInfo.phone_number_id || ''" in template
    assert "sessionInfoVersion: '3'" in template


def test_facebook_sdk_loads_only_after_fb_async_init_is_registered():
    template = (ROOT / "templates/ai_agents/whatsapp_onboarding.html").read_text(encoding="utf-8")

    init_callback = template.index("window.fbAsyncInit = function ()")
    sdk_loader = template.index("const sdkScript = document.createElement('script');")
    sdk_url = template.index("sdkScript.src = 'https://connect.facebook.net/en_US/sdk.js';")
    sdk_append = template.index("document.head.appendChild(sdkScript);")
    assert init_callback < sdk_loader < sdk_url < sdk_append
    assert "sdkScript.onerror = function ()" in template


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


def test_vendor_dashboard_state_reflects_real_channels_and_template_has_no_stray_brace():
    template = (ROOT / "templates/ai_agents/index.html").read_text(encoding="utf-8")

    assert '{% endif %}}\n<div class="app-shell ai-hub">' not in template
    assert "{% set vendor_webchat_live = public_webchat_enabled and public_vendor_url %}" in template
    assert "2 canales activos" in template
    assert "Vendedor web activo" in template
    assert "WhatsApp conectado" in template
    assert "Sin canales activos" in template
    assert 'class="badge text-bg-success fs-6"><i class="bi bi-circle-fill me-1"></i>Trabajando</span>' not in template


def test_whatsapp_onboarding_explains_meta_wait_and_has_recoverable_sdk_states():
    template = (ROOT / "templates/ai_agents/whatsapp_onboarding.html").read_text(encoding="utf-8")

    assert "WhatsApp vinculado correctamente" in template
    assert "Conexión completada" in template
    assert "Próximo paso: revisá tu Vendedor IA" in template
    assert "puede mostrarse en blanco unos segundos" in template.lower()
    assert "para Argentina, seleccioná +54" in template
    assert "Preparando conexión..." in template
    assert "Esperando a Meta..." in template
    assert "Guardando conexión..." in template
    assert "function loadMetaSdk()" in template
    assert "sdkScript.onerror = function ()" in template
    assert "Reintentar conexión" in template
    assert "resetConnectButton(" in template
    assert "statusBox.textContent = String(message || '').slice(0, 500);" in template
    assert "statusBox.innerHTML" not in template
    assert "extras: {setup: {}, featureType: 'whatsapp_business_app_onboarding', sessionInfoVersion: '3'}" in template




def test_whatsapp_token_lifecycle_is_tracked_without_exposing_credentials():
    source = (ROOT / "services/ai_agent/config_service.py").read_text(encoding="utf-8")
    onboarding = (ROOT / "ai_agents.py").read_text(encoding="utf-8")
    template = (ROOT / "templates/ai_agents/whatsapp_onboarding.html").read_text(encoding="utf-8")

    assert "def _whatsapp_token_lifecycle(data: dict)" in source
    assert '"token_health": health' in source
    assert '"token_days_remaining": days_remaining' in source
    assert '"access_token_expires_at": token_expires_at' in onboarding
    assert "def _exchange_long_lived_whatsapp_token" in onboarding
    assert "def _meta_expiration_iso" in onboarding
    assert "Actualizar conexión de Meta" in template
    assert "token_health in ['expired', 'expiring']" in template
    assert "access_token" not in template


def test_webhook_preserves_context_and_supports_common_interactive_and_media_types():
    source = (ROOT / "whatsapp_agent.py").read_text(encoding="utf-8")
    extractor = source[source.index("def _extract_messages(payload):"):source.index("def _extract_statuses(payload):")]

    assert 'context_message_id = str(context.get("id") or "").strip()' in extractor
    assert 'message_type == "interactive"' in extractor
    assert 'message_type in {"image", "document", "video"}' in extractor
    assert 'message_type == "audio"' in extractor
    assert "yield phone_number_id, sender, external_id, body, context_message_id, message_type" in extractor
    assert "resolve_shared_campaign_reply" in source
    assert "sender_connection=platform_connection" in source


def test_shared_campaign_reply_attribution_is_fail_closed_across_tenants():
    source = (ROOT / "services/ai_agent/tenant_campaign_service.py").read_text(encoding="utf-8")
    start = source.index("def resolve_shared_campaign_reply(")
    end = source.index("def _client_phone(", start)
    resolver = source[start:end]

    assert "context_message_id" in resolver
    assert "provider_message_id == referenced_id" in resolver
    assert "tenant_ids = {int(row.company_id) for row in candidates}" in resolver
    assert "if len(tenant_ids) != 1:" in resolver
    assert "return None" in resolver
