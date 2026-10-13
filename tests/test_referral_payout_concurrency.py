from __future__ import annotations

from datetime import datetime

import pytest


def test_referral_payout_rejects_a_commission_already_paid_instead_of_recording_empty_payout(app):
    from app import (
        Company,
        ReferralAttribution,
        ReferralCommission,
        ReferralPayout,
        ReferralPayoutItem,
        ReferralSeller,
        User,
        db,
    )
    from services.referral_service import ReferralService

    with app.app_context():
        user = User(
            username="referral-payout-qa",
            email="referral-payout-qa@example.test",
            password_hash="test-hash",
            role="seller",
            active=True,
        )
        company = Company(name="Referral payout QA", active=True)
        db.session.add_all([user, company])
        db.session.flush()

        seller = ReferralSeller(
            user_id=user.id,
            dni="QA-REF-001",
            referral_code="REFQA001",
            referral_url="https://example.test/?ref=REFQA001",
            active=True,
        )
        db.session.add(seller)
        db.session.flush()
        attribution = ReferralAttribution(
            seller_id=seller.id,
            company_id=company.id,
            user_id=user.id,
            referral_code=seller.referral_code,
        )
        db.session.add(attribution)
        db.session.flush()
        commission = ReferralCommission(
            seller_id=seller.id,
            attribution_id=attribution.id,
            company_id=company.id,
            sold_amount=1000,
            commission_percent=0.30,
            commission_amount=300,
            status="disponible",
            available_at=datetime.utcnow(),
        )
        db.session.add(commission)
        db.session.commit()
        commission_id = commission.id
        seller_id = seller.id
        user_id = user.id

        payout = ReferralService.register_payout(
            db.session,
            seller_id=seller_id,
            commission_ids=[commission_id, commission_id],
            processed_by_user_id=user_id,
            transfer_date=datetime.utcnow(),
        )
        db.session.commit()
        assert payout.amount == 300
        assert db.session.get(ReferralCommission, commission_id).status == "pagada"
        assert ReferralPayoutItem.query.filter_by(commission_id=commission_id).count() == 1

        with pytest.raises(ValueError, match="ya no están disponibles"):
            ReferralService.register_payout(
                db.session,
                seller_id=seller_id,
                commission_ids=[commission_id],
                processed_by_user_id=user_id,
                transfer_date=datetime.utcnow(),
            )
        db.session.rollback()
        assert ReferralPayout.query.count() == 1
        assert ReferralPayoutItem.query.filter_by(commission_id=commission_id).count() == 1
