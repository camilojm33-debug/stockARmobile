"""Structured tenant-scoped memory used by AI agents."""
from __future__ import annotations

from datetime import datetime

from stockarmobile.extensions import db


class AIBusinessMemory(db.Model):
    __tablename__ = "ai_business_memories"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, nullable=False, index=True)
    category = db.Column(db.String(60), nullable=False, index=True)
    memory_key = db.Column(db.String(120), nullable=False)
    value = db.Column(db.Text, nullable=False, default="")
    source = db.Column(db.String(40), nullable=False, default="manual")
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_by_user_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("company_id", "category", "memory_key", name="uq_ai_business_memory_company_category_key"),
        db.Index("ix_ai_business_memory_company_active", "company_id", "active"),
    )

    def __repr__(self):
        return f"<AIBusinessMemory {self.company_id}:{self.category}:{self.memory_key}>"
