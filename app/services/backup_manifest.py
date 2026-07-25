"""Phase 9F: backup-artifact + archive manifests and their verification.

Manifests are operator artifacts written next to the backup files (and a small
summary under ``CONFIG_ROOT`` that the app/release-check can read). They contain
basenames / archive-RELATIVE paths only — never host-absolute paths, secrets,
key values, or raw_json. Verification recomputes sizes/checksums and reports
counts + reason codes; it never modifies or deletes anything.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import AuditEvent, MediaFile, Video, utcnow

_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> tuple[str, int]:
    """Streaming sha256 -> (hexdigest, size_bytes)."""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def _safe_basename(name: str) -> str | None:
    """Reject path separators / traversal so a manifest can only reference a
    sibling file by basename."""
    if not name or "/" in name or "\\" in name or name in (".", "..") or name.startswith(".."):
        return None
    return name


# ---- backup (DB dump) manifest ----------------------------------------------
# v1 (Phase 9F): artifact/sha256/size/schema_head/created_at only.
# v2 (Phase 9F.1): identifies the whole BACKUP SET needed for recovery — dump +
# archive-manifest linkage + audit chain head + build + operational state — and
# carries its own canonical integrity hash (SHA-256, or HMAC-SHA256 when
# BACKUP_MANIFEST_HMAC_KEY_FILE is set; key values/paths never appear anywhere).
MANIFEST_VERSION = 2
REDIS_RECOVERY_MODE = "empty_redis_then_reconcile"
_INTEGRITY_FIELD = "integrity"


def _canonical_manifest_bytes(manifest: dict) -> bytes:
    body = {k: v for k, v in manifest.items() if k != _INTEGRITY_FIELD}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _integrity_for(manifest: dict, hmac_key: str | None) -> dict:
    import hmac as _hmac

    data = _canonical_manifest_bytes(manifest)
    if hmac_key:
        return {"scheme": "hmac_sha256",
                "hash": _hmac.new(hmac_key.encode("utf-8"), data, hashlib.sha256).hexdigest()}
    return {"scheme": "sha256", "hash": hashlib.sha256(data).hexdigest()}


def write_backup_manifest(artifact: Path, *, schema_head: str | None = None,
                          created_at: datetime | None = None,
                          summary_file: Path | None = None,
                          backup_id: str | None = None,
                          build: dict | None = None,
                          active_jobs: int | None = None,
                          audit_head: tuple[int, str] | None = None,
                          archive_manifest_path: Path | None = None,
                          hmac_key: str | None = None,
                          completed: bool = True) -> dict:
    """Write ``<artifact>.manifest.json`` next to the artifact (+ optional small
    summary copy under CONFIG_ROOT for release-check). Returns the manifest.

    All linkage fields are basenames / hashes / counts — never host paths."""
    import secrets as _secrets

    artifact = Path(artifact)
    digest, size = sha256_file(artifact)
    created = created_at or utcnow()
    archive_link = None
    if archive_manifest_path is not None:
        ap = Path(archive_manifest_path)
        a_sha, a_size = sha256_file(ap)
        try:
            a_data = json.loads(ap.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            a_data = {}
        archive_link = {
            "artifact": ap.name,
            "sha256": a_sha,
            "size_bytes": a_size,
            "db_video_media_files": a_data.get("db_video_media_files"),
            "total_bytes": a_data.get("total_bytes"),
        }
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "kind": "backup_db_dump",
        "backup_id": backup_id or f"bk-{created.strftime('%Y%m%d%H%M%S')}-{_secrets.token_hex(4)}",
        "created_at": created.isoformat(),
        "completed": bool(completed),
        "app_version": (build or {}).get("app_version"),
        "build_id": (build or {}).get("build_id"),
        "schema_head": schema_head,
        "artifact": artifact.name,
        "size_bytes": size,
        "sha256": digest,
        "active_jobs_at_backup": active_jobs,
        "audit_head_event_id": audit_head[0] if audit_head else None,
        "audit_head_event_hash": audit_head[1] if audit_head else None,
        "archive_manifest": archive_link,
        "redis_recovery_mode": REDIS_RECOVERY_MODE,
        "encrypted": False,
    }
    manifest[_INTEGRITY_FIELD] = _integrity_for(manifest, hmac_key)
    out = artifact.with_name(artifact.name + ".manifest.json")
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if summary_file:
        summary_file = Path(summary_file)
        summary_file.parent.mkdir(parents=True, exist_ok=True)
        summary_file.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def verify_backup_manifest(manifest_path: Path, *, artifact_dir: Path | None = None,
                           hmac_key: str | None = None, session: Session | None = None) -> dict:
    """Verify a backup manifest AND the backup set it identifies.

    v1 manifests: artifact existence/size/sha only (+ ``legacy_manifest_v1``
    warning). v2 adds: canonical integrity hash (HMAC when the manifest was
    signed), completed flag, archive-manifest linkage (sha of the sibling file),
    and — when a DB ``session`` is supplied — the audit chain head capture.

    Failure reasons: ``bad_manifest / unsafe_artifact_name /
    manifest_integrity_mismatch / integrity_key_missing / incomplete_backup /
    artifact_missing / size_mismatch / sha256_mismatch /
    archive_manifest_missing / archive_manifest_mismatch / audit_head_mismatch``.
    Non-fatal findings land in ``warnings``."""
    manifest_path = Path(manifest_path)
    res = {"ok": False, "reason": None, "manifest_version": None, "backup_id": None,
           "artifact": None, "size_bytes": None, "sha256": None, "schema_head": None,
           "created_at": None, "completed": None, "audit_head_event_id": None,
           "warnings": []}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
    except Exception:  # noqa: BLE001
        res["reason"] = "bad_manifest"
        return res
    version = data.get("manifest_version") or 1
    res.update(manifest_version=version, backup_id=data.get("backup_id"),
               schema_head=data.get("schema_head"), created_at=data.get("created_at"),
               completed=data.get("completed"),
               audit_head_event_id=data.get("audit_head_event_id"))
    name = _safe_basename(str(data.get("artifact") or ""))
    res["artifact"] = name
    if name is None:
        res["reason"] = "unsafe_artifact_name"
        return res

    if version >= 2:
        integ = data.get(_INTEGRITY_FIELD) or {}
        scheme = integ.get("scheme")
        if scheme == "hmac_sha256" and not hmac_key:
            res["reason"] = "integrity_key_missing"
            return res
        expected = _integrity_for(data, hmac_key if scheme == "hmac_sha256" else None)
        if expected["scheme"] != scheme or expected["hash"] != integ.get("hash"):
            res["reason"] = "manifest_integrity_mismatch"
            return res
        if data.get("completed") is not True:
            res["reason"] = "incomplete_backup"
            return res
    else:
        res["warnings"].append("legacy_manifest_v1")

    want_sha = str(data.get("sha256") or "")
    want_size = data.get("size_bytes")
    base_dir = Path(artifact_dir or manifest_path.parent)
    target = base_dir / name
    if not target.is_file():
        res["reason"] = "artifact_missing"
        return res
    digest, size = sha256_file(target)
    res.update(size_bytes=size, sha256=digest)
    if isinstance(want_size, int) and size != want_size:
        res["reason"] = "size_mismatch"
        return res
    if digest != want_sha:
        res["reason"] = "sha256_mismatch"
        return res

    if version >= 2:
        link = data.get("archive_manifest")
        if isinstance(link, dict):
            a_name = _safe_basename(str(link.get("artifact") or ""))
            if a_name is None:
                res["reason"] = "unsafe_artifact_name"
                return res
            a_path = base_dir / a_name
            if not a_path.is_file():
                res["reason"] = "archive_manifest_missing"
                return res
            if sha256_file(a_path)[0] != str(link.get("sha256") or ""):
                res["reason"] = "archive_manifest_mismatch"
                return res
        else:
            res["warnings"].append("no_archive_manifest_link")

        head_id, head_hash = data.get("audit_head_event_id"), data.get("audit_head_event_hash")
        if head_id and head_hash:
            row = None
            checked = False
            if session is not None:
                try:
                    row = session.execute(
                        select(AuditEvent.event_hash).where(AuditEvent.id == int(head_id))
                    ).scalar()
                    checked = True
                except Exception:  # noqa: BLE001 - DB unreachable -> offline verify
                    checked = False
            if not checked:
                res["warnings"].append("audit_head_not_checked")
            elif row is None:
                res["warnings"].append("audit_head_event_not_found")
            elif row != head_hash:
                res["reason"] = "audit_head_mismatch"
                return res
        else:
            res["warnings"].append("no_audit_head_captured")

        aj = data.get("active_jobs_at_backup")
        if isinstance(aj, int) and aj > 0:
            res["warnings"].append(f"active_jobs_at_backup={aj}")

    res["ok"] = True
    return res


# ---- archive (media files) manifest -----------------------------------------
def write_archive_manifest(session: Session, settings: Settings | None = None, *,
                           out_path: Path, hash_limit: int = 0) -> dict:
    """Snapshot DB video media_files -> archive manifest (relative paths only).

    ``hash_limit`` > 0 additionally records sha256 for the first N PRESENT files
    (newest-first, same ordering as archive_media_check). Hashing terabytes is
    expensive, so it is opt-in. Returns a summary WITHOUT the entries list."""
    settings = settings or get_settings()
    from app.services import storage

    rows = session.execute(
        select(MediaFile.id, MediaFile.path, Video.youtube_video_id)
        .join(Video, Video.id == MediaFile.video_id)
        .where(MediaFile.media_type == "video")
        .order_by(MediaFile.id.desc())
    ).all()

    entries: list[dict] = []
    present = missing = hashed = 0
    total_bytes = 0
    for mid, rel, yid in rows:
        entry: dict = {"media_file_id": mid, "youtube_video_id": yid, "path": rel,
                       "size_bytes": None, "sha256": None}
        try:
            p = storage.to_absolute(settings, rel)
            if p.is_file():
                entry["size_bytes"] = p.stat().st_size
                total_bytes += entry["size_bytes"]
                present += 1
                if hash_limit and hashed < hash_limit:
                    entry["sha256"] = sha256_file(p)[0]
                    hashed += 1
            else:
                missing += 1
        except OSError:
            missing += 1
        entries.append(entry)

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "kind": "archive_media",
        "generated_at": utcnow().isoformat(),
        "db_video_media_files": len(rows),
        "present": present,
        "missing_at_generation": missing,
        "hashed_count": hashed,
        "total_bytes": total_bytes,
        "entries": entries,
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    summary = {k: v for k, v in manifest.items() if k != "entries"}
    return summary


def verify_archive_manifest(settings: Settings | None = None, *, manifest_path: Path,
                            check_hashes: bool = True) -> dict:
    """Compare current archive state against a manifest.

    Entries recorded as missing at generation are skipped (``recorded_missing``).
    Reports counts + up to 50 missing/mismatched public youtube ids — no paths."""
    settings = settings or get_settings()
    from app.services import storage

    res = {"ok": False, "reason": None, "checked": 0, "present": 0, "missing": 0,
           "size_mismatch": 0, "hash_mismatch": 0, "hash_checked": 0,
           "recorded_missing": 0, "mismatch_youtube_ids": []}
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        entries = data["entries"]
        assert isinstance(entries, list)
    except Exception:  # noqa: BLE001
        res["reason"] = "bad_manifest"
        return res

    def _note(yid):
        if yid and len(res["mismatch_youtube_ids"]) < 50:
            res["mismatch_youtube_ids"].append(yid)

    for e in entries:
        rel = e.get("path")
        want_size = e.get("size_bytes")
        if want_size is None:
            res["recorded_missing"] += 1
            continue
        res["checked"] += 1
        try:
            p = storage.to_absolute(settings, rel)
            ok_file = p.is_file()
        except OSError:
            ok_file = False
        if not ok_file:
            res["missing"] += 1
            _note(e.get("youtube_video_id"))
            continue
        res["present"] += 1
        if p.stat().st_size != want_size:
            res["size_mismatch"] += 1
            _note(e.get("youtube_video_id"))
            continue
        if check_hashes and e.get("sha256"):
            res["hash_checked"] += 1
            if sha256_file(p)[0] != e["sha256"]:
                res["hash_mismatch"] += 1
                _note(e.get("youtube_video_id"))

    res["ok"] = res["missing"] == 0 and res["size_mismatch"] == 0 and res["hash_mismatch"] == 0
    return res


# ---- marker / summary readers (release-check + API) --------------------------
def read_backup_manifest_summary(settings: Settings | None = None) -> dict | None:
    """Read the small manifest summary (written by backup.sh via the CLI). Only
    whitelisted scalar fields are returned — never paths."""
    settings = settings or get_settings()
    path = (settings.backup_manifest_summary_file or "").strip()
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert isinstance(data, dict)
    except Exception:  # noqa: BLE001
        return None
    name = _safe_basename(str(data.get("artifact") or ""))
    link = data.get("archive_manifest") if isinstance(data.get("archive_manifest"), dict) else {}
    integ = data.get(_INTEGRITY_FIELD) if isinstance(data.get(_INTEGRITY_FIELD), dict) else {}
    return {
        "artifact": name,
        "size_bytes": data.get("size_bytes") if isinstance(data.get("size_bytes"), int) else None,
        "sha256": str(data.get("sha256") or "")[:64] or None,
        "schema_head": (str(data.get("schema_head"))[:32] if data.get("schema_head") else None),
        "created_at": (str(data.get("created_at"))[:40] if data.get("created_at") else None),
        "manifest_version": data.get("manifest_version"),
        # v2 backup-set fields (all leak-free scalars)
        "backup_id": (str(data.get("backup_id"))[:48] if data.get("backup_id") else None),
        "completed": data.get("completed") if isinstance(data.get("completed"), bool) else None,
        "app_version": (str(data.get("app_version"))[:40] if data.get("app_version") else None),
        "build_id": (str(data.get("build_id"))[:40] if data.get("build_id") else None),
        "active_jobs_at_backup": (data.get("active_jobs_at_backup")
                                  if isinstance(data.get("active_jobs_at_backup"), int) else None),
        "audit_head_event_id": (data.get("audit_head_event_id")
                                if isinstance(data.get("audit_head_event_id"), int) else None),
        "redis_recovery_mode": (str(data.get("redis_recovery_mode"))[:48]
                                if data.get("redis_recovery_mode") else None),
        "encrypted": data.get("encrypted") if isinstance(data.get("encrypted"), bool) else None,
        "archive_manifest_artifact": _safe_basename(str(link.get("artifact") or "")) or None,
        "archive_manifest_sha256": str(link.get("sha256") or "")[:64] or None,
        "integrity_scheme": (str(integ.get("scheme"))[:20] if integ.get("scheme") else None),
    }


def marker_age_hours(path_value: str) -> float | None:
    """mtime age of a marker file in hours; None when unset/missing/unreadable."""
    import time as _t

    p = (path_value or "").strip()
    if not p:
        return None
    try:
        f = Path(p)
        if not f.is_file():
            return None
        return max(0.0, (_t.time() - f.stat().st_mtime) / 3600.0)
    except OSError:
        return None


def touch_marker(path_value: str) -> bool:
    p = (path_value or "").strip()
    if not p:
        return False
    try:
        f = Path(p)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(utcnow().isoformat() + "\n", encoding="utf-8")
        return True
    except OSError:
        return False
