"""Phase 12A: secure-by-default public-beta bind.

A fresh checkout must publish the admin console to loopback only, and the
first-run checklist must escalate to a DANGER warning when the operator opts
into an all-interfaces bind while authentication is still disabled.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.config import get_settings
from app.services import production_check as pc


def test_web_bind_is_all_interfaces_property():
    get_settings.cache_clear()
    s = get_settings()
    assert s.web_bind_host == "127.0.0.1"
    assert s.web_bind_is_all_interfaces is False
    get_settings.cache_clear()


def test_default_compose_publishes_web_to_loopback_only():
    """The shipped docker-compose.yml default must bind web to loopback and must
    not host-publish the datastores (secure-by-default distribution)."""
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    web_ports = compose["services"]["web"]["ports"]
    # the default is expressed via ${WEB_BIND_HOST:-127.0.0.1}
    assert any("WEB_BIND_HOST:-127.0.0.1" in str(p) for p in web_ports), web_ports
    assert all("0.0.0.0" not in str(p) for p in web_ports)
    for svc in ("postgres", "redis"):
        assert "ports" not in compose["services"][svc], f"{svc} must not be host-published"


def test_first_run_loopback_auth_disabled_is_warn(settings, session):
    d = pc.first_run_status(session, settings)  # default bind 127.0.0.1, auth disabled
    assert d["web_bind_all_interfaces"] is False
    assert d["exposure_warning"] is True
    assert d["exposure_level"] == "warn"
    auth = next(i for i in d["items"] if i["key"] == "auth")
    assert auth.get("warn") is True and not auth.get("danger")


def test_first_run_all_interfaces_auth_disabled_is_danger(settings, session, monkeypatch):
    monkeypatch.setenv("WEB_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("AUTH_MODE", "disabled")
    get_settings.cache_clear()
    s2 = get_settings()
    d = pc.first_run_status(session, s2)
    assert d["web_bind_all_interfaces"] is True
    assert d["exposure_level"] == "danger"
    auth = next(i for i in d["items"] if i["key"] == "auth")
    assert auth.get("danger") is True
    assert "0.0.0.0" in d["exposure_note"] or "all interfaces" in d["exposure_note"]
    get_settings.cache_clear()


def test_first_run_auth_enabled_is_none(settings, session, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "local")
    get_settings.cache_clear()
    s2 = get_settings()
    d = pc.first_run_status(session, s2)
    assert d["exposure_warning"] is False
    assert d["exposure_level"] == "none"
    get_settings.cache_clear()


def test_first_run_danger_no_leak(settings, session, monkeypatch):
    import json

    from app.services.vuln_triage import scan_text_for_host_leaks

    monkeypatch.setenv("WEB_BIND_HOST", "0.0.0.0")
    get_settings.cache_clear()
    s2 = get_settings()
    d = pc.first_run_status(session, s2)
    assert scan_text_for_host_leaks(json.dumps(d), repo_root="/some/build/dir") == []
    get_settings.cache_clear()


def test_schema_serializes_new_exposure_fields(settings, session):
    from app.schemas import FirstRunStatusOut

    out = FirstRunStatusOut(**pc.first_run_status(session, settings))
    assert out.exposure_level in ("none", "warn", "danger")
    assert out.web_bind_host == "127.0.0.1"
