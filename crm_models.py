"""Modelos del CRM comercial tenant.

Este módulo es deliberadamente independiente del CRM de SuperAdmin (SaaS).
Solo representa oportunidades y actividades del comercio autenticado.
"""
from stockarmobile.extensions import db


class CRMOpportunity(db.Model):
    __tablename__ = "crm_opportunities"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(180), nullable=False)
    stage = db.Column(db.String(30), nullable=False, default="nuevo")
    status = db.Column(db.String(20), nullable=False, default="open")
    value = db.Column(db.Numeric(18, 2), nullable=False, default=0)
    probability = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    expected_close_date = db.Column(db.DateTime, nullable=True)
    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    source = db.Column(db.String(60), nullable=True)
    quote_id = db.Column(db.Integer, db.ForeignKey("quotes.id", ondelete="SET NULL"), nullable=True, index=True)
    sale_id = db.Column(db.Integer, db.ForeignKey("sales.id", ondelete="SET NULL"), nullable=True, index=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False)

    client = db.relationship("Client", foreign_keys=[client_id])
    owner = db.relationship("User", foreign_keys=[owner_user_id])
    quote = db.relationship("Quote", foreign_keys=[quote_id])
    sale = db.relationship("Sale", foreign_keys=[sale_id])

    @property
    def weighted_value(self):
        return float(self.value or 0) * float(self.probability or 0) / 100.0


class CRMActivity(db.Model):
    __tablename__ = "crm_activities"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True)
    opportunity_id = db.Column(db.Integer, db.ForeignKey("crm_opportunities.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    type = db.Column(db.String(30), nullable=False, default="tarea")
    status = db.Column(db.String(20), nullable=False, default="pending")
    subject = db.Column(db.String(180), nullable=False)
    description = db.Column(db.Text, nullable=True)
    due_at = db.Column(db.DateTime, nullable=True, index=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False)

    client = db.relationship("Client", foreign_keys=[client_id])
    opportunity = db.relationship("CRMOpportunity", foreign_keys=[opportunity_id], backref=db.backref("activities", lazy="dynamic"))
    user = db.relationship("User", foreign_keys=[user_id])
