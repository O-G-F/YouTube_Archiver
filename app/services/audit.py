"""Phase 9E: append-only, tamper-evident audit trail.

* Actor/client identifiers are stored ONLY as stable HMAC pseudonyms — never raw
  email / IP.
* Metadata is sanitised (key denylist + value redaction) so passwords, tokens,
  cookies, hashes, host paths, raw_json, emails, IPs never land in an audit row.
* Each event carries ``previous_hash`` + ``event_hash`` forming a hash chain
  (HMAC-SHA256 when ``AUDIT_HMAC_KEY_FILE`` is set, else an unsigned SHA-256 chain
  for development). ``verify_chain`` detects reordering/tampering and honours a
  retention checkpoint at the pruning boundary.

A hash chain is EVIDENCE, not absolute prevention: a DB admin with write access
can still rewrite rows + recompute hashes if they hold the HMAC key. It raises the
bar and makes casual tampering detectable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.logging_setup import get_logger
from app.models import AuditCheckpoint, AuditEvent, utcnow

logger = get_logger(__name__)

# categories that get the longer (security) retention window
SECURITY_CATEGORIES = {"auth", "security"}

# ---- metadata sanitisation --------------------------------------------------
_META_DENY_KEY_SUBSTR = (
    "password", "passwd", "secret", "token", "cookie", "hash", "authorization",
    "po_token", "visitor", "csrf", "session", "email", "ip", "cidr", "path", "raw_json",
)
_BAD_VALUE_SUBSTR = ("/users/", "/secrets/", "/archive/", "scrypt$", "cookies.txt", "@")


def _safe_scalar(v):
    if isinstance(v, bool) or isinstance(v, int) or isinstance(v, float) or v is None:
        return v
    if isinstance(v, str):
        s = v[:200]
        low = s.lower()
        if any(b in low for b in _BAD_VALUE_SUBSTR):
            return "[redacted]"
        return s
    return None  # drop non-scalars


def safe_metadata(metadata: dict | None) -> dict | None:
    """Allow-list-ish sanitiser: drop sensitive keys, keep only redacted scalars."""
    if not metadata:
        return None
    out: dict = {}
    for k, v in metadata.items():
        kl = str(k).lower()
        if any(d in kl for d in _META_DENY_KEY_SUBSTR):
            continue
        if isinstance(v, (list, tuple)):
            out[str(k)[:48]] = [_safe_scalar(x) for x in list(v)[:20]]
        else:
            sv = _safe_scalar(v)
            if sv is not None or v is None:
                out[str(k)[:48]] = sv
    return out or None


# ---- pseudonymisation + hashing --------------------------------------------
def pseudonymize(settings: Settings, value, *, kind: str) -> str | None:
    """Stable HMAC pseudonym for an email/IP/etc — never reversible to the raw value."""
    if value in (None, ""):
        return None
    key = settings.audit_pseudonymize_key().encode("utf-8")
    return hmac.new(key, f"{kind}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()[:24]


# chain_version 1 (Phase 9E) canonical: no signing metadata. v2 (Phase 9E.1) adds it.
_V1_FIELDS = ("occurred_at", "event_type", "category", "severity", "outcome", "actor_kind",
              "actor_id_hash", "client_id_hash", "request_id", "correlation_id",
              "resource_type", "resource_id", "action", "reason_code", "metadata_json")
_V2_FIELDS = ("chain_version", "signature_scheme", "signing_key_id") + _V1_FIELDS
_CP_FIELDS = ("checkpoint_type", "reason_code", "previous_event_id", "previous_event_hash",
              "next_event_id", "previous_signing_key_id", "next_signing_key_id", "occurred_at")
UNSIGNED = "sha256_unsigned"
HMAC = "hmac_sha256"


def _canon(fields: tuple, d: dict) -> str:
    return json.dumps({k: d.get(k) for k in fields}, sort_keys=True, separators=(",", ":"), default=str)


def _event_payload(ev: AuditEvent) -> dict:
    return {
        "chain_version": ev.chain_version, "signature_scheme": ev.signature_scheme,
        "signing_key_id": ev.signing_key_id,
        "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
        "event_type": ev.event_type, "category": ev.category, "severity": ev.severity,
        "outcome": ev.outcome, "actor_kind": ev.actor_kind, "actor_id_hash": ev.actor_id_hash,
        "client_id_hash": ev.client_id_hash, "request_id": ev.request_id,
        "correlation_id": ev.correlation_id, "resource_type": ev.resource_type,
        "resource_id": ev.resource_id, "action": ev.action, "reason_code": ev.reason_code,
        "metadata_json": ev.metadata_json,
    }


def _event_canonical(ev: AuditEvent) -> str:
    fields = _V2_FIELDS if (ev.chain_version or 1) >= 2 else _V1_FIELDS
    return _canon(fields, _event_payload(ev))


def _hash_with(scheme: str, key: str | None, canonical: str, previous_hash: str | None) -> str | None:
    msg = f"{previous_hash or ''}|{canonical}".encode("utf-8")
    if scheme == HMAC:
        if not key:
            return None  # cannot verify/sign without the key
        return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return hashlib.sha256(msg).hexdigest()  # sha256_unsigned


def _checkpoint_canonical(c) -> str:
    return _canon(_CP_FIELDS, {
        "checkpoint_type": c.checkpoint_type, "reason_code": c.reason_code,
        "previous_event_id": c.previous_event_id, "previous_event_hash": c.previous_event_hash,
        "next_event_id": c.next_event_id, "previous_signing_key_id": c.previous_signing_key_id,
        "next_signing_key_id": c.next_signing_key_id,
        "occurred_at": c.occurred_at.isoformat() if c.occurred_at else None,
    })


def _checkpoint_hash(c, keys: dict) -> str:
    next_key = keys.get(c.next_signing_key_id) if c.next_signing_key_id else None
    scheme = HMAC if next_key else UNSIGNED
    return _hash_with(scheme, next_key, _checkpoint_canonical(c), c.previous_event_hash)


# ---- record -----------------------------------------------------------------
def record_event(session: Session, settings: Settings | None = None, *, event_type: str,
                 category: str, severity: str = "info", outcome: str = "success",
                 actor_kind: str = "system", actor_id_hash: str | None = None,
                 client_id_hash: str | None = None, request_id: str | None = None,
                 correlation_id: str | None = None, resource_type: str | None = None,
                 resource_id=None, action: str | None = None, reason_code: str | None = None,
                 metadata: dict | None = None, occurred_at: datetime | None = None) -> AuditEvent | None:
    """Append one audit event (chained). Returns None when auditing is disabled."""
    settings = settings or get_settings()
    if not settings.audit_enabled:
        return None
    occurred = occurred_at or utcnow()
    meta = safe_metadata(metadata)
    rid = str(resource_id)[:64] if resource_id is not None else None
    prev = session.scalar(select(AuditEvent.event_hash).order_by(AuditEvent.id.desc()).limit(1))
    key_id, key = settings.audit_current_signing()
    scheme = HMAC if key else UNSIGNED
    kid = key_id if key else "dev"
    payload = {
        "chain_version": 2, "signature_scheme": scheme, "signing_key_id": kid,
        "occurred_at": occurred.isoformat(), "event_type": event_type, "category": category,
        "severity": severity, "outcome": outcome, "actor_kind": actor_kind,
        "actor_id_hash": actor_id_hash, "client_id_hash": client_id_hash,
        "request_id": request_id, "correlation_id": correlation_id,
        "resource_type": resource_type, "resource_id": rid, "action": action,
        "reason_code": reason_code, "metadata_json": meta,
    }
    ev = AuditEvent(
        occurred_at=occurred, event_type=event_type, category=category, severity=severity,
        outcome=outcome, actor_kind=actor_kind, actor_id_hash=actor_id_hash,
        client_id_hash=client_id_hash, request_id=request_id, correlation_id=correlation_id,
        resource_type=resource_type, resource_id=rid, action=action, reason_code=reason_code,
        metadata_json=meta, chain_version=2, signature_scheme=scheme, signing_key_id=kid,
        previous_hash=prev, event_hash=_hash_with(scheme, key, _canon(_V2_FIELDS, payload), prev),
    )
    session.add(ev)
    session.flush()
    return ev


def record_request_event(session: Session, settings: Settings | None, request, *, event_type: str,
                          category: str, severity: str = "info", outcome: str = "success",
                          resource_type: str | None = None, resource_id=None, action: str | None = None,
                          reason_code: str | None = None, metadata: dict | None = None):
    """Record an event using the request's principal + request/correlation id
    (from AuthMiddleware scope state). Uses the passed session (commits with the op)."""
    settings = settings or get_settings()
    st = (request.scope.get("state") or {})
    p = st.get("principal")
    actor_kind = getattr(p, "via", "anonymous") if p else "anonymous"
    actor_hash = pseudonymize(settings, getattr(p, "sub", None), kind=actor_kind) if p else None
    client_ip = request.client.host if request.client else None
    try:
        return record_event(
            session, settings, event_type=event_type, category=category, severity=severity,
            outcome=outcome, actor_kind=actor_kind, actor_id_hash=actor_hash,
            client_id_hash=pseudonymize(settings, client_ip, kind="ip"),
            request_id=st.get("request_id"), correlation_id=st.get("correlation_id"),
            resource_type=resource_type, resource_id=resource_id, action=action,
            reason_code=reason_code, metadata=metadata)
    except Exception as exc:  # noqa: BLE001 - audit must not break the primary op
        logger.warning("audit: request event %s failed: %s", event_type, type(exc).__name__)
        return None


def record_event_autocommit(**kwargs) -> None:
    """Record in a fresh session (for callers outside a request/DB scope). Never
    raises to the caller — auditing failure must not break the primary action."""
    try:
        from app.db import session_scope

        with session_scope() as s:
            record_event(s, **kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit: failed to record %s: %s", kwargs.get("event_type"), type(exc).__name__)


# ---- verify -----------------------------------------------------------------
_LIFECYCLE_TYPES = {"signing_enabled", "key_rotated", "restore_boundary"}


def verify_chain(session: Session, settings: Settings | None = None) -> dict:
    """Segment-aware verification (Phase 9E.1).

    Handles: legacy unsigned prefix, explicit signing/key-rotation/restore
    checkpoints, current + previous-key signed segments, retention boundaries.
    Distinguishes a legitimate (checkpoint-vouched) signing-regime change from an
    unvouched one (tamper). Returns valid / valid_with_warnings / checked_count /
    segment_count / checkpoint_count / current_signing_key_id / unsigned_event_count /
    missing_verification_keys / first_invalid_event_id / failure_reason_code.
    """
    settings = settings or get_settings()
    keys = settings.audit_verification_keys()
    cur_key_id, _cur = settings.audit_current_signing()
    events = list(session.scalars(select(AuditEvent).order_by(AuditEvent.id.asc())))
    checkpoints = list(session.scalars(select(AuditCheckpoint).order_by(AuditCheckpoint.id.asc())))

    res = {"valid": True, "valid_with_warnings": False, "checked_count": 0, "segment_count": 1,
           "checkpoint_count": len(checkpoints), "current_signing_key_id": cur_key_id,
           "unsigned_event_count": 0, "missing_verification_keys": [], "first_invalid_event_id": None,
           "failure_reason_code": None, "signed": bool(cur_key_id)}

    def bad(reason, eid=None):
        res.update(valid=False, failure_reason_code=reason, first_invalid_event_id=eid)
        return res

    if not events:
        return res

    by_id = {e.id: e for e in events}
    lifecycle = [c for c in checkpoints if c.checkpoint_type in _LIFECYCLE_TYPES]
    retention = [c for c in checkpoints if c.checkpoint_type == "retention"]

    # The LATEST restore_boundary re-baselines the chain: everything at/below it is
    # attested (not recomputed) and earlier lifecycle checkpoints are superseded.
    attested_up_to = max((c.previous_event_id or 0 for c in lifecycle
                          if c.checkpoint_type == "restore_boundary"), default=0)

    boundary_by_prev: dict[int, object] = {}
    for c in lifecycle:
        pid = c.previous_event_id or 0
        if pid < attested_up_to:
            continue  # superseded by a later restore_boundary (trusted, not re-verified)
        signed_cp = bool(c.next_signing_key_id) and c.next_signing_key_id != "dev"
        next_key = keys.get(c.next_signing_key_id) if signed_cp else None
        if signed_cp and next_key is None:  # checkpoint signed with a key we don't have
            if c.next_signing_key_id not in res["missing_verification_keys"]:
                res["missing_verification_keys"].append(c.next_signing_key_id)
            return bad("missing_verification_key", c.previous_event_id)
        expected = _hash_with(HMAC if signed_cp else UNSIGNED, next_key,
                              _checkpoint_canonical(c), c.previous_event_hash)
        if expected != c.checkpoint_hash:
            return bad("tampered_checkpoint", c.previous_event_id)
        if c.previous_event_id:
            pe = by_id.get(c.previous_event_id)
            if pe is None or pe.event_hash != c.previous_event_hash:
                return bad("checkpoint_boundary_mismatch", c.previous_event_id)
        boundary_by_prev[pid] = c

    ret_prev = None
    if retention:
        rc = max(retention, key=lambda c: c.id)
        if events[0].id > (rc.up_to_event_id or 0):
            ret_prev = rc.boundary_hash

    segments = 1
    for i, ev in enumerate(events):
        if ev.signature_scheme == UNSIGNED:
            res["unsigned_event_count"] += 1
        attested = ev.id <= attested_up_to

        if not attested:
            key = keys.get(ev.signing_key_id)
            if ev.signature_scheme == HMAC and not key:
                if ev.signing_key_id not in res["missing_verification_keys"]:
                    res["missing_verification_keys"].append(ev.signing_key_id)
                return bad("missing_verification_key", ev.id)
            if _hash_with(ev.signature_scheme, key, _event_canonical(ev), ev.previous_hash) != ev.event_hash:
                return bad("event_hash_mismatch", ev.id)

        if i == 0:
            if ret_prev is not None and ev.previous_hash != ret_prev:
                return bad("retention_boundary_mismatch", ev.id)
        else:
            p = events[i - 1]
            inside_attested = attested and p.id <= attested_up_to
            if not inside_attested and ev.previous_hash != p.event_hash:
                return bad("previous_hash_mismatch", ev.id)
            regime_changed = (ev.signature_scheme, ev.signing_key_id) != (p.signature_scheme, p.signing_key_id)
            if regime_changed and not inside_attested:
                b = boundary_by_prev.get(p.id)
                if b is None:
                    return bad("unexpected_regime_change", ev.id)
                if b.previous_signing_key_id != p.signing_key_id or b.next_signing_key_id != ev.signing_key_id:
                    return bad("boundary_regime_mismatch", ev.id)
                segments += 1
        res["checked_count"] += 1

    res["segment_count"] = segments
    if res["unsigned_event_count"] > 0 and cur_key_id:
        res["valid_with_warnings"] = True
    return res


def establish_signing_boundary(session: Session, settings: Settings | None = None, *,
                               reason_code: str | None = None, checkpoint_type: str = "signing_enabled",
                               apply: bool = False) -> dict:
    """Create an explicit signing/restore boundary at the current chain head so a
    subsequent signing-regime change verifies cleanly. Dry-run by default.

    ``restore_boundary`` is break-glass (Phase 9F): it attests everything at/below
    the current head instead of verifying it, so it REQUIRES an explicit reason
    code and must never be used to paper over ordinary chain corruption — the
    pre-boundary verify result is embedded in the plan/audit trail as evidence.
    """
    settings = settings or get_settings()
    if checkpoint_type not in _LIFECYCLE_TYPES:
        return {"ok": False, "reason": "invalid checkpoint_type"}
    if checkpoint_type == "restore_boundary" and not (reason_code or "").strip():
        return {"ok": False, "reason": "restore_boundary requires an explicit reason code"}
    keys = settings.audit_verification_keys()
    cur_key_id, cur_key = settings.audit_current_signing()
    head = session.scalar(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1))
    if head is None:
        return {"ok": False, "reason": "no audit events to bound"}
    pre = verify_chain(session, settings)
    next_key_id = cur_key_id or "dev"
    plan = {"ok": True, "checkpoint_type": checkpoint_type, "previous_event_id": head.id,
            "previous_signing_key_id": head.signing_key_id, "next_signing_key_id": next_key_id,
            "reason_code": reason_code, "apply": bool(apply),
            "pre_boundary_chain_valid": pre["valid"],
            "pre_boundary_failure_reason_code": pre["failure_reason_code"]}
    if not apply:
        plan["dry_run"] = True
        return plan
    c = AuditCheckpoint(
        checkpoint_type=checkpoint_type, reason=checkpoint_type, reason_code=reason_code,
        occurred_at=utcnow(), previous_event_id=head.id, previous_event_hash=head.event_hash,
        next_event_id=head.id + 1, previous_signing_key_id=head.signing_key_id, next_signing_key_id=next_key_id,
    )
    c.checkpoint_hash = _checkpoint_hash(c, keys)
    session.add(c)
    session.flush()
    record_event(session, settings, event_type="audit_signing_boundary", category="security",
                 severity="warning" if checkpoint_type == "restore_boundary" else "info",
                 outcome="success", actor_kind="system", action=checkpoint_type,
                 reason_code=reason_code,
                 metadata={"previous_event_id": head.id, "next_key_id": next_key_id,
                           "pre_boundary_chain_valid": pre["valid"],
                           "pre_boundary_failure_reason_code": pre["failure_reason_code"]})
    session.commit()
    plan["applied"] = True
    return plan


def rotate_key(session: Session, settings: Settings | None = None, *, reason_code: str | None = None,
               apply: bool = False) -> dict:
    """Record a key-rotation boundary. The NEW key must already be the current
    signing key, with the OLD key kept as a previous verification key."""
    return establish_signing_boundary(session, settings, reason_code=reason_code,
                                      checkpoint_type="key_rotated", apply=apply)


def signing_status(session: Session, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    cur_key_id, cur_key = settings.audit_current_signing()
    v = verify_chain(session, settings)
    return {
        "signature_scheme": HMAC if cur_key else UNSIGNED,
        "current_key_id": cur_key_id,
        "current_key_configured": bool(cur_key),
        "previous_key_ids": list(settings.audit_previous_keys().keys()),
        "pseudonym_key_configured": settings.audit_pseudonym_key_configured,
        "key_config_error": settings.audit_key_config_error(),
        "chain_valid": v["valid"],
        "valid_with_warnings": v["valid_with_warnings"],
        "unsigned_event_count": v["unsigned_event_count"],
        "segment_count": v["segment_count"],
        "checkpoint_count": v["checkpoint_count"],
        "missing_verification_keys": v["missing_verification_keys"],
        "boundary_needed": v["failure_reason_code"] == "unexpected_regime_change",
    }


# ---- query / stats / export -------------------------------------------------
def list_events(session: Session, *, limit: int = 50, offset: int = 0, event_type: str | None = None,
                category: str | None = None, severity: str | None = None, outcome: str | None = None,
                request_id: str | None = None, correlation_id: str | None = None,
                since: datetime | None = None, until: datetime | None = None) -> list[AuditEvent]:
    stmt = select(AuditEvent)
    if request_id:
        stmt = stmt.where(AuditEvent.request_id == request_id)
    if correlation_id:
        stmt = stmt.where(AuditEvent.correlation_id == correlation_id)
    if event_type:
        stmt = stmt.where(AuditEvent.event_type == event_type)
    if category:
        stmt = stmt.where(AuditEvent.category == category)
    if severity:
        stmt = stmt.where(AuditEvent.severity == severity)
    if outcome:
        stmt = stmt.where(AuditEvent.outcome == outcome)
    if since:
        stmt = stmt.where(AuditEvent.occurred_at >= since)
    if until:
        stmt = stmt.where(AuditEvent.occurred_at <= until)
    stmt = stmt.order_by(AuditEvent.id.desc()).limit(max(1, min(limit, 500))).offset(max(0, offset))
    return list(session.scalars(stmt))


def event_to_dict(ev: AuditEvent) -> dict:
    """Serialise for API/export — pseudonyms + sanitised metadata only."""
    return {
        "id": ev.id,
        "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
        "event_type": ev.event_type, "category": ev.category, "severity": ev.severity,
        "outcome": ev.outcome, "actor_kind": ev.actor_kind,
        "actor_id_hash": ev.actor_id_hash, "client_id_hash": ev.client_id_hash,
        "request_id": ev.request_id, "correlation_id": ev.correlation_id,
        "resource_type": ev.resource_type, "resource_id": ev.resource_id,
        "action": ev.action, "reason_code": ev.reason_code,
        "metadata": ev.metadata_json, "event_hash": ev.event_hash,
        "chain_version": ev.chain_version, "signature_scheme": ev.signature_scheme,
        "signing_key_id": ev.signing_key_id,
    }


def stats(session: Session, *, days: int = 30, now: datetime | None = None) -> dict:
    now = now or utcnow()
    since = now - timedelta(days=days)
    total = int(session.scalar(select(func.count(AuditEvent.id))) or 0)
    def _group(col):
        rows = session.execute(
            select(col, func.count(AuditEvent.id)).where(AuditEvent.occurred_at >= since).group_by(col)
        ).all()
        return {str(k): int(n) for k, n in rows}
    return {
        "total": total,
        "window_days": days,
        "by_category": _group(AuditEvent.category),
        "by_severity": _group(AuditEvent.severity),
        "by_outcome": _group(AuditEvent.outcome),
    }


def export_events(session: Session, settings: Settings | None = None, *, since: datetime | None = None,
                  until: datetime | None = None):
    """Yield JSONL rows (redacted, same as the API). Capped by AUDIT_MAX_EXPORT_EVENTS."""
    settings = settings or get_settings()
    cap = settings.audit_max_export_events
    stmt = select(AuditEvent).order_by(AuditEvent.id.asc())
    if since:
        stmt = stmt.where(AuditEvent.occurred_at >= since)
    if until:
        stmt = stmt.where(AuditEvent.occurred_at <= until)
    stmt = stmt.limit(cap)
    for ev in session.scalars(stmt):
        yield json.dumps(event_to_dict(ev), separators=(",", ":"), default=str)


# ---- retention cleanup (contiguous prefix + checkpoint) --------------------
def cleanup(session: Session, settings: Settings | None = None, *, now: datetime | None = None) -> dict:
    """Prune the oldest CONTIGUOUS run of expired audit events (so the chain stays
    intact) and record a checkpoint at the boundary. Security-category events keep
    the longer window, which naturally blocks pruning past them."""
    settings = settings or get_settings()
    now = now or utcnow()
    normal_cut = now - timedelta(days=max(1, settings.audit_retention_days))
    sec_cut = now - timedelta(days=max(1, settings.audit_security_retention_days))

    rows = session.execute(
        select(AuditEvent.id, AuditEvent.category, AuditEvent.occurred_at, AuditEvent.event_hash)
        .order_by(AuditEvent.id.asc())
    ).all()
    # Never prune past a signing/restore boundary — its referenced event hash is
    # needed to verify the chain. Cap the prune boundary below the earliest one.
    protect_from = session.scalar(
        select(func.min(AuditCheckpoint.previous_event_id))
        .where(AuditCheckpoint.checkpoint_type.in_(list(_LIFECYCLE_TYPES)))
    )
    last_id = None
    boundary = None
    count = 0
    for eid, cat, occ, eh in rows:
        if protect_from is not None and eid >= protect_from:
            break  # keep events needed by signing boundaries
        cut = sec_cut if cat in SECURITY_CATEGORIES else normal_cut
        if occ is not None and occ < cut:
            last_id, boundary, count = eid, eh, count + 1
        else:
            break  # stop at the first non-expired event -> contiguous prefix only
    if last_id is None:
        return {"deleted": 0, "up_to_event_id": None}

    session.execute(delete(AuditEvent).where(AuditEvent.id <= last_id))
    session.add(AuditCheckpoint(checkpoint_type="retention", reason="retention_cleanup",
                                occurred_at=now, up_to_event_id=last_id,
                                boundary_hash=boundary, deleted_count=count))
    session.flush()
    # record the cleanup itself (chains from the last surviving event / checkpoint)
    record_event(session, settings, event_type="audit_retention_cleanup", category="ops",
                 severity="info", outcome="success", actor_kind="system", action="cleanup",
                 metadata={"deleted": count, "up_to_event_id": last_id})
    session.commit()
    return {"deleted": count, "up_to_event_id": last_id}
