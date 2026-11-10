import json
from types import SimpleNamespace

from app import Branch, Company, db
from services.sales.receipt_service import ReceiptService


def test_additional_branch_ticket_uses_its_own_name_and_contact(app):
    with app.app_context():
        company = Company(
            name="Comercio Central",
            address="Dirección central 10",
            city="Resistencia",
            phone="3624000000",
            printer_settings_json=json.dumps({
                "ticket_name": "Marca central",
                "printer_type": "thermal",
                "printer_host": "192.168.1.10",
                "printer_port": 9100,
            }),
        )
        db.session.add(company)
        db.session.flush()
        branch = Branch(
            company_id=company.id,
            name="Sucursal Norte",
            code="NORTE",
            address="Av. Norte 123",
            city="Fontana",
            province="Chaco",
            phone="3624111111",
            whatsapp="3624222222",
            active=True,
            is_default=False,
        )
        db.session.add(branch)
        db.session.flush()

        context = ReceiptService.ticket_context(
            company=company,
            sale=SimpleNamespace(company_id=company.id, branch_id=branch.id),
        )

        assert context["ticket_name"] == "Sucursal Norte"
        assert context["branch_name"] == "Sucursal Norte"
        assert context["location_line"] == "Av. Norte 123, Fontana, Chaco"
        assert context["phone"] == "3624111111"
        assert context["whatsapp"] == "3624222222"
        assert context["printer_settings"]["printer_host"] == "192.168.1.10"


def test_default_branch_inherits_company_ticket_identity(app):
    with app.app_context():
        company = Company(
            name="Comercio Central",
            address="Mitre 100",
            printer_settings_json=json.dumps({
                "ticket_name": "Marca comercial central",
                "printer_type": "thermal",
                "printer_host": "10.0.0.8",
            }),
        )
        db.session.add(company)
        db.session.flush()
        branch = Branch(
            company_id=company.id,
            name="Casa central",
            code="CENTRAL",
            active=True,
            is_default=True,
        )
        db.session.add(branch)
        db.session.flush()

        context = ReceiptService.ticket_context(
            company=company,
            sale=SimpleNamespace(company_id=company.id, branch_id=branch.id),
        )

        assert context["ticket_name"] == "Marca comercial central"
        assert context["address"] == "Mitre 100"
        assert context["printer_settings"]["printer_host"] == "10.0.0.8"
        assert context["printer_settings"]["paper_size"] == "Ticket80"


def test_branch_ticket_overrides_take_precedence_and_can_override_company_printer(app):
    with app.app_context():
        company = Company(
            name="Comercio Central",
            printer_settings_json=json.dumps({
                "ticket_name": "Marca central",
                "printer_type": "thermal",
                "printer_host": "192.168.1.10",
                "printer_port": 9100,
                "cashdrawer_enabled": True,
            }),
        )
        db.session.add(company)
        db.session.flush()
        branch = Branch(
            company_id=company.id,
            name="Sucursal Sur",
            code="SUR",
            active=True,
            is_default=False,
            receipt_settings_json=json.dumps({
                "ticket_name": "Marca Sur",
                "logo": "/static/sur.png",
                "footer": "Gracias desde Sucursal Sur",
                "printer_type": "thermal",
                "printer_host": "192.168.1.22",
                "printer_port": 9200,
                "cashdrawer_enabled": False,
            }),
        )
        db.session.add(branch)
        db.session.flush()

        context = ReceiptService.ticket_context(
            company=company,
            sale=SimpleNamespace(company_id=company.id, branch_id=branch.id),
        )

        assert context["ticket_name"] == "Marca Sur"
        assert context["logo_url"] == "/static/sur.png"
        assert context["footer"] == "Gracias desde Sucursal Sur"
        assert context["printer_settings"]["printer_host"] == "192.168.1.22"
        assert context["printer_settings"]["printer_port"] == 9200
        assert context["printer_settings"]["cashdrawer_enabled"] is False


def test_ticket_text_displays_persisted_sale_branch_not_current_branch(app):
    with app.app_context():
        company = Company(name="Comercio Central")
        db.session.add(company)
        db.session.flush()
        branch = Branch(
            company_id=company.id,
            name="Sucursal Costanera",
            code="COSTA",
            address="Costanera 456",
            active=True,
            is_default=False,
        )
        db.session.add(branch)
        db.session.flush()
        sale = SimpleNamespace(
            id=501,
            company_id=company.id,
            branch_id=branch.id,
            date=None,
            customer="Consumidor final",
            items=[],
            subtotal=0,
            discount=0,
            discount_type=None,
            discount_value=None,
            discount_reason=None,
            surcharge=0,
            surcharge_type=None,
            surcharge_value=None,
            surcharge_reason=None,
            charges_json=None,
            tax=0,
            total_amount=0,
            note=None,
        )

        ticket = ReceiptService.ticket_text(sale, "Sucursal Costanera")

        assert "Sucursal: Sucursal Costanera" in ticket
        assert "Dirección: Costanera 456" in ticket
        assert "Sucursal: Casa central" not in ticket
