from pathlib import Path

from services.backup_service import BackupService


def test_backup_schedule_is_monthly_and_plan_based():
    assert BackupService.monthly_backup_slots("trial") == (1,)
    assert BackupService.monthly_backup_slots("entrepreneur") == (1,)
    assert BackupService.monthly_backup_slots("business") == (1, 15)
    assert BackupService.monthly_backup_slots("premium") == (1, 10, 20)


def test_render_backup_cron_is_not_daily():
    render = Path("render.yaml").read_text(encoding="utf-8")
    assert 'name: stockarmobile-daily-backup' in render
    assert 'schedule: "15 3 1,10,15,20 * *"' in render
    assert 'schedule: "15 3 * * *"' not in render
