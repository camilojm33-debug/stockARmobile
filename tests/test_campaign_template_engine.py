from services.campaign_template_engine import (
    SCOPE_MERCHANT_CLIENTS,
    SCOPE_SAAS_PROSPECTS,
    goal_options,
    get_goal,
    message_templates,
    normalize_mapping,
    parse_defaults,
    preview_for_context,
    resolve_parameters,
)


def test_campaign_goals_and_copy_library_are_isolated_by_audience_scope():
    saas_goals = {item["key"] for item in goal_options(SCOPE_SAAS_PROSPECTS)}
    merchant_goals = {item["key"] for item in goal_options(SCOPE_MERCHANT_CLIENTS)}

    assert saas_goals == {
        "saas_acquisition",
        "saas_trial_conversion",
        "saas_lead_followup",
    }
    assert merchant_goals == {
        "customer_marketing",
        "customer_reactivation",
        "customer_loyalty",
        "customer_new_product",
    }
    assert all(item["goal_key"].startswith("saas_") for item in message_templates(SCOPE_SAAS_PROSPECTS))
    assert all(item["goal_key"].startswith("customer_") for item in message_templates(SCOPE_MERCHANT_CLIENTS))


def test_normalize_mapping_requires_known_unique_fields_and_exact_parameter_count():
    mapping, errors = normalize_mapping(
        SCOPE_SAAS_PROSPECTS,
        ["contacto", "producto", "mensaje"],
        expected_count=3,
        goal_key="saas_acquisition",
    )
    assert mapping == ["contacto", "producto", "mensaje"]
    assert errors == []

    _, errors = normalize_mapping(
        SCOPE_SAAS_PROSPECTS,
        ["contacto", "contacto", "campo_inventado"],
        expected_count=3,
        goal_key="saas_acquisition",
    )
    assert any("No repitas" in error for error in errors)
    assert any("no soportadas" in error for error in errors)

    _, errors = normalize_mapping(
        SCOPE_MERCHANT_CLIENTS,
        ["contacto", "mensaje"],
        expected_count=3,
        goal_key="customer_marketing",
    )
    assert any("requiere 3 parámetro(s)" in error for error in errors)


def test_parameter_resolution_uses_contact_fields_before_campaign_defaults():
    result = resolve_parameters(
        SCOPE_SAAS_PROSPECTS,
        ["contacto", "producto", "mensaje", "trial_url"],
        {
            "contacto": "Carla",
            "mensaje": "Hola Carla",
            "trial_url": "https://app.example.com/trial",
        },
        {"producto": "StockARmobile"},
        expected_count=4,
        goal_key="saas_trial_conversion",
    )

    assert result["valid"] is True
    assert result["values"] == [
        "Carla",
        "StockARmobile",
        "Hola Carla",
        "https://app.example.com/trial",
    ]
    assert result["details"][1]["source"] == "valor predeterminado"


def test_missing_fields_are_reported_without_empty_positional_values():
    result = resolve_parameters(
        SCOPE_MERCHANT_CLIENTS,
        ["contacto", "email", "mensaje"],
        {"contacto": "Ana", "mensaje": "Novedades"},
        {},
        expected_count=3,
        goal_key="customer_marketing",
    )

    assert result["valid"] is False
    assert result["missing_fields"] == ["email"]
    assert result["values"] == ["Ana", "", "Novedades"]


def test_defaults_are_limited_to_the_selected_scope_and_bounded():
    defaults = parse_defaults(
        SCOPE_MERCHANT_CLIENTS,
        {
            "producto": "Novedades",
            "trial_url": "https://not-for-merchant.example",
            "mensaje": "x" * 450,
        },
    )
    assert defaults == {
        "producto": "Novedades",
        "mensaje": "x" * 300,
    }


def test_personalized_preview_renders_meta_positional_variables():
    preview = preview_for_context(
        SCOPE_SAAS_PROSPECTS,
        "Hola {{1}} de {{2}}. {{3}}",
        ["contacto", "empresa", "mensaje"],
        {
            "contacto": "Carla",
            "empresa": "Ferretería Demo",
            "mensaje": "Conocé StockARmobile.",
        },
        expected_count=3,
    )

    assert preview["valid"] is True
    assert preview["message"] == "Hola Carla de Ferretería Demo. Conocé StockARmobile."
