from __future__ import annotations

import io
from contextlib import redirect_stderr


def test_campaign_runner_rejects_non_https_worker_url_before_network_call(monkeypatch):
    import run_saas_campaigns as runner

    monkeypatch.setattr(runner, "WORKER_URL", "file:///etc/passwd")
    monkeypatch.setattr(runner, "WORKER_TOKEN", "worker-token")
    monkeypatch.setenv("DATABASE_URL", "")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("invalid URLs must not reach urllib")

    monkeypatch.setattr(runner.urllib.request, "urlopen", fail_if_called)
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        result = runner.main()
    assert result == 2
    assert "valid HTTPS" in stderr.getvalue()


def test_campaign_runner_accepts_https_worker_url_without_userinfo():
    from run_saas_campaigns import _validated_worker_url

    assert _validated_worker_url("https://www.stockarmobile.com/internal/crm/campaign-worker") == (
        "https://www.stockarmobile.com/internal/crm/campaign-worker"
    )
    assert _validated_worker_url("http://www.stockarmobile.com/internal/crm/campaign-worker") is None
    assert _validated_worker_url("https://user:password@www.stockarmobile.com/worker") is None
