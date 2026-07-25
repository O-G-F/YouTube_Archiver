"""Phase 9F.2: deployment worker convergence hardening.

After a deploy recreates the containers, a just-stopped worker's heartbeat
lingers in Redis until its TTL expires. Its build_id differs from the freshly
built web, so the final preflight's worker_build_match would transiently FAIL.
`worker_convergence` + `system worker-convergence` let the deploy WAIT for the
stale registration to expire — while still failing on a genuine, persistent
build mismatch or a missing worker. Counts only; no ids / redis url / paths leak.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from typer.testing import CliRunner

from app.cli import app as cli_app
from app.services import build_info as bi

REPO = Path(__file__).resolve().parents[1]
runner = CliRunner()


class FakeRedis:
    """In-memory Redis stand-in (same shape as test_phase6f)."""

    def __init__(self, *, ping_ok: bool = True):
        self.store: dict[str, str] = {}
        self._ping_ok = ping_ok

    def ping(self):
        if not self._ping_ok:
            raise ConnectionError("redis down")
        return True

    def set(self, k, v, ex=None):
        self.store[k] = v

    def get(self, k):
        return self.store.get(k)

    def scan_iter(self, match=None):
        import fnmatch

        for k in list(self.store):
            if match is None or fnmatch.fnmatch(k, match):
                yield k


def _seed(fake, worker_id, build_id, *, age=5.0, job_types=("takeout_import",)):
    fake.store[f"archiver:worker:heartbeat:{worker_id}"] = json.dumps({
        "build_id": build_id, "app_version": "0.1.0",
        "supported_job_types": list(job_types), "worker_id": worker_id,
        "ts": time.time() - age,
    })


CUR = None  # resolved in tests via bi.build_id()


# --------------------------------------------------------------------------- #
# worker_convergence() semantics
# --------------------------------------------------------------------------- #
def test_convergence_current_only_ready():
    fake = FakeRedis()
    bi.write_worker_heartbeat(fake)  # current build, fresh
    r = bi.worker_convergence(fake)
    assert r["ready"] is True and r["reason"] == "converged"
    assert r["active_current_count"] == 1
    assert r["mismatched_fresh_count"] == 0 and r["mismatched_stale_count"] == 0


def test_convergence_current_plus_stale_old_waiting():
    fake = FakeRedis()
    bi.write_worker_heartbeat(fake)                       # current, fresh
    _seed(fake, "old:1", "src:OLDBUILD", age=70.0)        # old build, STALE (age>60)
    r = bi.worker_convergence(fake)
    assert r["ready"] is False and r["reason"] == "mismatched_stale_present"
    assert r["active_current_count"] == 1 and r["mismatched_stale_count"] == 1
    assert r["mismatched_fresh_count"] == 0


def test_convergence_after_stale_gone_ready():
    fake = FakeRedis()
    bi.write_worker_heartbeat(fake)  # only current remains after the old TTL-expired
    r = bi.worker_convergence(fake)
    assert r["ready"] is True and r["reason"] == "converged"


def test_convergence_no_current_worker_not_ready():
    fake = FakeRedis()
    _seed(fake, "old:1", "src:OLDBUILD", age=5.0)  # a fresh OLD-build worker, no current
    r = bi.worker_convergence(fake)
    assert r["ready"] is False and r["reason"] == "no_active_current_worker"
    assert r["active_current_count"] == 0


def test_convergence_fresh_mismatch_not_ready():
    fake = FakeRedis()
    bi.write_worker_heartbeat(fake)                      # current, fresh
    _seed(fake, "old:1", "src:OLDBUILD", age=5.0)        # old build, FRESH (genuine live mismatch)
    r = bi.worker_convergence(fake)
    assert r["ready"] is False and r["reason"] == "mismatched_fresh_present"
    assert r["mismatched_fresh_count"] == 1


def test_convergence_no_workers_not_ready():
    r = bi.worker_convergence(FakeRedis())
    assert r["ready"] is False and r["reason"] == "no_workers" and r["worker_count"] == 0


def test_convergence_malformed_registration_ignored():
    fake = FakeRedis()
    bi.write_worker_heartbeat(fake)                                  # valid current
    fake.store["archiver:worker:heartbeat:garbage"] = "not-json{"    # malformed -> skipped
    r = bi.worker_convergence(fake)
    assert r["ready"] is True and r["worker_count"] == 1  # bad entry ignored


def test_convergence_explicit_web_build_override():
    fake = FakeRedis()
    _seed(fake, "w:1", "src:XYZ", age=5.0)
    r = bi.worker_convergence(fake, web_build_id="src:XYZ")
    assert r["ready"] is True and r["web_build_id"] == "src:XYZ"


# --------------------------------------------------------------------------- #
# CLI exit codes + no-leak
# --------------------------------------------------------------------------- #
def test_cli_ready_exit_0(monkeypatch):
    fake = FakeRedis()
    bi.write_worker_heartbeat(fake)
    monkeypatch.setattr("app.worker.queue.get_redis", lambda: fake)
    r = runner.invoke(cli_app, ["system", "worker-convergence"])
    assert r.exit_code == 0 and "ready=True" in r.output and "reason=converged" in r.output


def test_cli_not_ready_exit_1(monkeypatch):
    fake = FakeRedis()
    bi.write_worker_heartbeat(fake)
    _seed(fake, "old:1", "src:OLDBUILD", age=5.0)  # fresh mismatch -> not ready
    monkeypatch.setattr("app.worker.queue.get_redis", lambda: fake)
    r = runner.invoke(cli_app, ["system", "worker-convergence"])
    assert r.exit_code == 1 and "ready=False" in r.output


def test_cli_redis_unavailable_exit_2(monkeypatch):
    monkeypatch.setattr("app.worker.queue.get_redis", lambda: FakeRedis(ping_ok=False))
    r = runner.invoke(cli_app, ["system", "worker-convergence", "--json"])
    assert r.exit_code == 2
    assert json.loads(r.output.strip())["reason"] == "redis_unavailable"


def test_cli_wait_times_out_exit_1(monkeypatch):
    fake = FakeRedis()
    bi.write_worker_heartbeat(fake)
    _seed(fake, "old:1", "src:OLDBUILD", age=5.0)  # never converges (persistent fresh mismatch)
    monkeypatch.setattr("app.worker.queue.get_redis", lambda: fake)
    r = runner.invoke(cli_app, ["system", "worker-convergence",
                                "--wait", "--timeout", "1", "--poll", "1"])
    assert r.exit_code == 1 and "ready=False" in r.output


def test_cli_json_shape_and_no_leak(monkeypatch):
    fake = FakeRedis()
    bi.write_worker_heartbeat(fake)
    _seed(fake, "secret-host-42:9", "src:OLDBUILD", age=70.0)
    monkeypatch.setattr("app.worker.queue.get_redis", lambda: fake)
    r = runner.invoke(cli_app, ["system", "worker-convergence", "--json"])
    data = json.loads(r.output.strip())
    for k in ("ready", "reason", "web_build_id", "worker_count", "active_current_count",
              "mismatched_fresh_count", "mismatched_stale_count", "stale_current_count"):
        assert k in data
    # counts only — no worker ids / hostnames / redis url / passwords / host paths
    for bad in ("secret-host-42", "worker_id", "redis://", "password", "/Users/", "/secrets/"):
        assert bad not in r.output


# --------------------------------------------------------------------------- #
# deploy.sh: polls convergence, not a fixed sleep; non-destructive
# --------------------------------------------------------------------------- #
def test_deploy_script_polls_convergence_not_fixed_sleep():
    text = (REPO / "scripts" / "deploy.sh").read_text("utf-8")
    assert "set -euo pipefail" in text
    # convergence is polled (loop over the machine-readable CLI), configurable
    assert "archiver system worker-convergence" in text
    assert "WORKER_CONVERGENCE_TIMEOUT_SECONDS" in text
    assert "WORKER_CONVERGENCE_POLL_SECONDS" in text
    # must NOT hard-code a single long fixed pre-preflight sleep as the mechanism
    import re

    for m in re.finditer(r"\bsleep\s+(\d+)", text):
        assert int(m.group(1)) <= 10, f"fixed long sleep found: sleep {m.group(1)}"
    # deploy_failed on convergence path is recorded only after the timeout branch
    assert "did NOT converge" in text and "deploy_failed" in text


def test_deploy_script_non_destructive():
    text = (REPO / "scripts" / "deploy.sh").read_text("utf-8")
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    for ln in code:
        assert "down -v" not in ln, f"destructive: {ln}"
        assert "--volumes" not in ln
        assert "rm -rf" not in ln
        assert "volume rm" not in ln
