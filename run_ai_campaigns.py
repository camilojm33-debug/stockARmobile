"""Render Cron entrypoint for tenant Marketing IA campaigns."""
from __future__ import annotations
import json
import logging
from app import app, db
from services.ai_agent.tenant_campaign_service import dispatch_due_campaigns

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def main() -> int:
    with app.app_context():
        result = dispatch_due_campaigns(db.session)
        logging.getLogger("ai-marketing").info("Tenant AI campaign cycle: %s", result)
        print(json.dumps(result, ensure_ascii=False, default=str))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
