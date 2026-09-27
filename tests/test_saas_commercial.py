from io import BytesIO
from openpyxl import Workbook

from services.saas_commercial_service import (
    lead_score, normalize_email, normalize_phone, parse_consent, parse_prospect_file
)

def test_normalize_email_and_phone():
    assert normalize_email(" VENTAS@EMPRESA.COM ") == "ventas@empresa.com"
    assert normalize_email("not-an-email") is None
    assert normalize_phone("+54 9 11-1234-5678") == "5491112345678"

def test_parse_consent_values():
    assert parse_consent("sí") == "opted_in"
    assert parse_consent("no") == "opted_out"
    assert parse_consent("quizas") == "unknown"

def test_target_lead_score():
    row={"industry":"Ferretería industrial","email":"ventas@e.com","phone":"5491112345678","website":"https://tienda.e.com","province":"Buenos Aires","locality":"Morón","instagram":"","facebook":""}
    assert lead_score(row) >= 80

def test_parse_csv():
    payload=("Comercio;Rubro;Localidad;Provincia;Email;WhatsApp;Web;Consentimiento Email
"
             "Ferretería Uno;Ferretería;Morón;Buenos Aires;ventas@uno.com;+54 9 11 5555-1111;https://uno.com;si
"
             "Fila incompleta;;;;;;;si
").encode("utf-8")
    result=parse_prospect_file(payload,"prospectos.csv")
    assert result["format"]=="csv"
    assert len(result["rows"])==1
    assert result["rows"][0]["email_consent_status"]=="opted_in"
    assert result["invalid_count"]==1

def test_parse_xlsx():
    wb=Workbook(); ws=wb.active
    ws.append(["Comercio","Rubro","Email","Localidad","Provincia"])
    ws.append(["Pet Shop Demo","Pet Shop","hola@petshop.com","CABA","CABA"])
    buf=BytesIO(); wb.save(buf)
    result=parse_prospect_file(buf.getvalue(),"prospectos.xlsx")
    assert result["format"]=="xlsx"
    assert result["rows"][0]["company_name"]=="Pet Shop Demo"

def test_parse_rejects_unsupported():
    try:
        parse_prospect_file(b"","prospectos.pdf")
    except ValueError as exc:
        assert "CSV" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
