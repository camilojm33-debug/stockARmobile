import pytest


def test_support_ticket_persistence_and_notification(app):
    # app fixture provides Flask app and test DB configuration
    from app import db, Company, User, SupportTicket, utcnow
    from services.notification_service import _build_superadmin_notifications

    # Create company and user
    company = Company(name="Empresa Test")
    db.session.add(company)
    db.session.flush()
    user = User(username="testuser", email="test@example.local", password_hash="x", company_id=company.id, role="user")
    db.session.add(user)
    db.session.flush()

    ticket = SupportTicket(
        company_id=company.id,
        user_id=user.id,
        email=user.email,
        reason="Otra consulta",
        description="Este es un ticket de prueba",
        status="pendiente",
        created_at=utcnow(),
    )
    db.session.add(ticket)
    db.session.commit()

    # Verify ticket persisted
    t = SupportTicket.query.filter_by(id=ticket.id).first()
    assert t is not None
    assert t.company_id == company.id
    assert t.user_id == user.id

    # Verify superadmin notifications include support item
    items = _build_superadmin_notifications()
    titles = [it.get("title") for it in items]
    assert "Pedidos de ayuda" in titles
