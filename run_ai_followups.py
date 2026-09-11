"""Render Cron entrypoint for proactive AI follow-ups."""

from __future__ import annotations

import json
import logging

from app import app
from services.ai_agent.followup_service import AIFollowupService


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ai-followups")


def main() -> int:
    with app.app_context():
        result = AIFollowupService.scan()
        logger.info(
            "AI follow-ups scan complete scanned=%s eligible=%s queued=%s",
            result["scanned"],
            result["eligible"],
            result["queued"],
        )
        print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
