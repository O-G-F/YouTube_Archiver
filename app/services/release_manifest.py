"""Phase 10A: release-candidate manifest — provenance + supply-chain identity.

A release manifest uniquely identifies everything shipped for one release
candidate so it can be rebuilt from the same commit and re-verified later:
version identity, dependency-lock hashes, Dockerfile / compose / migration
hashes, per-service image ids/digests/base refs, SBOM + vulnerability-scan
identifiers, the migration-rehearsal result, and a release-check summary.

Like the backup manifest it is versioned and carries its own canonical
integrity hash (SHA-256, or HMAC-SHA256 when RELEASE_MANIFEST_HMAC_KEY_FILE is
set). It contains ONLY basenames / hashes / counts / scalars — never host
paths, repo paths, usernames, registry credentials, secrets, raw environment,
emails/IPs, or tokens.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from app.config import Settings, get_settings
from app.models import utcnow
from app.services import build_info as bi

MANIFEST_VERSION = 1
_INTEGRITY_FIELD = "integrity"
_CHUNK = 1024 * 1024

# Files whose content pins the release (repo-relative). Missing files hash to None.
PYTHON_LOCK = "requirements.txt"
PYTHON_LOCK = "requirements.lock"          # Phase 10A.1: the hash-pinned lock
PYTHON_DIRECT_INPUT = "requirements.txt"   # human-edited DIRECT dependency input
FRONTEND_LOCK = "frontend/package-lock.json"
DOCKERFILE = "Dockerfile"
COMPOSE_TEMPLATE = "docker-compose.production.example.yml"
MIGRATIONS_DIR = "alembic/versions"

SERVICE_NAMES = ("web", "worker", "scheduler", "migrate")


def python_lock_status(root: Path | None = None) -> dict:
    """Phase 10A.1: assess whether requirements.lock is a real hash-pinned lock.

    Returns ``{present, exact, hashed, package_count, unpinned[]}`` — ``exact`` =
    every requirement is ``name==version``; ``hashed`` = every requirement has at
    least one ``--hash=sha256:``. No paths returned."""
    r = root or _repo_root()
    p = r / PYTHON_LOCK
    if not p.is_file():
        return {"present": False, "exact": False, "hashed": False,
                "package_count": 0, "unpinned": []}
    reqs: list[str] = []
    hashes_per_req: list[int] = []
    cur_has_hash = 0
    started = False
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("--hash="):
                cur_has_hash += 1
                continue
            # a new requirement line (may end with a backslash continuation)
            name = line.split("\\", 1)[0].strip()
            if started:
                hashes_per_req.append(cur_has_hash)
            reqs.append(name)
            cur_has_hash = 0
            started = True
        if started:
            hashes_per_req.append(cur_has_hash)
    except OSError:
        return {"present": True, "exact": False, "hashed": False,
                "package_count": 0, "unpinned": []}
    unpinned = [rq for rq in reqs if "==" not in rq]
    exact = bool(reqs) and not unpinned
    hashed = bool(hashes_per_req) and all(n >= 1 for n in hashes_per_req)
    return {"present": True, "exact": exact, "hashed": hashed,
            "package_count": len(reqs), "unpinned": unpinned[:20]}


def _repo_root() -> Path:
    # app/services/release_manifest.py -> repo root is two levels up from app/
    return Path(__file__).resolve().parent.parent.parent


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _sha256_dir(path: Path, *, suffix: str = ".py") -> str | None:
    if not path.is_dir():
        return None
    h = hashlib.sha256()
    try:
        for p in sorted(x for x in path.rglob(f"*{suffix}") if "__pycache__" not in x.parts):
            h.update(p.relative_to(path).as_posix().encode("utf-8"))
            h.update(b"\0")
            h.update(p.read_bytes())
            h.update(b"\0")
    except OSError:
        return None
    return h.hexdigest()


def source_hashes(root: Path | None = None) -> dict:
    """Content hashes that pin this release's source/config (repo-relative).

    Phase 10A.1: ``python_lock_sha256`` hashes the HASH-PINNED requirements.lock;
    the human-edited requirements.txt direct input is hashed separately."""
    r = root or _repo_root()
    return {
        "python_lock_sha256": _sha256_file(r / PYTHON_LOCK),
        "python_direct_input_sha256": _sha256_file(r / PYTHON_DIRECT_INPUT),
        "frontend_lock_sha256": _sha256_file(r / FRONTEND_LOCK),
        "dockerfile_sha256": _sha256_file(r / DOCKERFILE),
        "compose_template_sha256": _sha256_file(r / COMPOSE_TEMPLATE),
        "migration_dir_sha256": _sha256_dir(r / MIGRATIONS_DIR),
    }


def _safe_basename(name: str | None) -> str | None:
    if not name:
        return None
    s = str(name)
    if "/" in s or "\\" in s or s in (".", "..") or s.startswith(".."):
        return None
    return s


# ---- integrity --------------------------------------------------------------
def _canonical_bytes(manifest: dict) -> bytes:
    body = {k: v for k, v in manifest.items() if k != _INTEGRITY_FIELD}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _integrity_for(manifest: dict, hmac_key: str | None) -> dict:
    import hmac as _hmac

    data = _canonical_bytes(manifest)
    if hmac_key:
        return {"scheme": "hmac_sha256",
                "hash": _hmac.new(hmac_key.encode("utf-8"), data, hashlib.sha256).hexdigest()}
    return {"scheme": "sha256", "hash": hashlib.sha256(data).hexdigest()}


# ---- create -----------------------------------------------------------------
def create_release_manifest(settings: Settings | None = None, *,
                            release_id: str | None = None,
                            created_at: datetime | None = None,
                            backend_test_count: int | None = None,
                            frontend_test_count: int | None = None,
                            images: dict | None = None,
                            base_images: dict | None = None,
                            sbom: dict | None = None,
                            vulnerability_scan: dict | None = None,
                            migration_rehearsal: dict | None = None,
                            release_check: dict | None = None,
                            base_remediation_status: str | None = None,
                            dependency_remediation_status: str | None = None,
                            apt: dict | None = None,
                            completed: bool = True,
                            hmac_key: str | None = None) -> dict:
    """Build the release manifest dict (does not write it). ``images`` /
    ``base_images`` / ``sbom`` / ``vulnerability_scan`` come from the build
    script (they need docker); the rest is computed in-process."""
    settings = settings or get_settings()
    v = bi.version_info()
    created = created_at or utcnow()
    import secrets as _secrets

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "kind": "release_candidate",
        "release_id": release_id or f"rel-{created.strftime('%Y%m%d%H%M%S')}-{_secrets.token_hex(4)}",
        "created_at": created.isoformat(),
        "completed": bool(completed),
        # version identity
        "app_version": v["app_version"],
        "git_commit": v["git_commit"],
        "git_tree_clean": v["git_tree_clean"],
        "build_id": v["build_id"],
        "build_timestamp": v["build_timestamp"],
        "schema_head": v["schema_head"],
        "frontend_build_id": v["frontend_build_id"],
        # test evidence
        "backend_test_count": backend_test_count,
        "frontend_test_count": frontend_test_count,
        # source/config pins + Python lock status (Phase 10A.1)
        **source_hashes(),
        "python_lock": python_lock_status(),
        # per-service image identity (from `docker inspect`)
        "images": images or {},
        "base_images": base_images or {},
        # supply-chain artifacts
        "sbom": sbom,
        "vulnerability_scan": vulnerability_scan,
        "backup_manifest_version": _backup_manifest_version(),
        # migration acceptance linkage (Phase 9F rehearsal)
        "migration_rehearsal": migration_rehearsal,
        # Phase 10B remediation rollups (informational status strings)
        "base_remediation_status": base_remediation_status,
        "dependency_remediation_status": dependency_remediation_status,
        # Phase 10B.2 apt reproducibility (recorded package set + sha256; the apt
        # transaction is NOT snapshot-pinned, so this is recorded, not "pinned").
        "apt_packages": apt,
        # deploy-gate summary
        "release_check": release_check,
    }
    manifest[_INTEGRITY_FIELD] = _integrity_for(manifest, hmac_key)
    return manifest


def _backup_manifest_version() -> int:
    try:
        from app.services import backup_manifest as bm

        return int(bm.MANIFEST_VERSION)
    except Exception:  # noqa: BLE001
        return 0


def write_release_manifest(manifest: dict, *, out_path: Path,
                           summary_file: Path | None = None) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    out_path.write_text(body, encoding="utf-8")
    if summary_file:
        # Best-effort: a non-writable summary dir (e.g. an unmounted /config) must
        # not abort the primary manifest write.
        try:
            summary_file = Path(summary_file)
            summary_file.parent.mkdir(parents=True, exist_ok=True)
            summary_file.write_text(json.dumps(read_summary_dict(manifest), indent=2, sort_keys=True) + "\n",
                                    encoding="utf-8")
        except OSError:
            pass


# ---- verify -----------------------------------------------------------------
def verify_release_manifest(manifest_path: Path, *, hmac_key: str | None = None,
                            root: Path | None = None) -> dict:
    """Recompute the manifest integrity hash + the source/config hashes and the
    per-service image build-id agreement. Returns ``{ok, reason, warnings, ...}``.

    reasons: bad_manifest / integrity_key_missing / manifest_integrity_mismatch /
    incomplete_release / python_lock_mismatch / frontend_lock_mismatch /
    dockerfile_mismatch / compose_mismatch / migration_dir_mismatch /
    service_build_mismatch / schema_head_mismatch."""
    manifest_path = Path(manifest_path)
    res = {"ok": False, "reason": None, "manifest_version": None, "release_id": None,
           "app_version": None, "build_id": None, "schema_head": None,
           "git_tree_clean": None, "completed": None, "warnings": []}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
    except Exception:  # noqa: BLE001
        res["reason"] = "bad_manifest"
        return res
    res.update(manifest_version=data.get("manifest_version"), release_id=data.get("release_id"),
               app_version=data.get("app_version"), build_id=data.get("build_id"),
               schema_head=data.get("schema_head"), git_tree_clean=data.get("git_tree_clean"),
               completed=data.get("completed"))

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
        res["reason"] = "incomplete_release"
        return res

    # source/config hashes vs the current tree (rebuild-from-commit check)
    cur = source_hashes(root)
    for field, reason in (("python_lock_sha256", "python_lock_mismatch"),
                          ("frontend_lock_sha256", "frontend_lock_mismatch"),
                          ("dockerfile_sha256", "dockerfile_mismatch"),
                          ("compose_template_sha256", "compose_mismatch"),
                          ("migration_dir_sha256", "migration_dir_mismatch")):
        want = data.get(field)
        have = cur.get(field)
        if want is None:
            res["warnings"].append(f"{field}_not_recorded")
            continue
        if have is None:
            res["warnings"].append(f"{field}_absent_in_tree")
            continue
        if want != have:
            res["reason"] = reason
            return res

    # schema head vs code (the release must target the current head)
    code_head = bi.code_schema_head()
    if data.get("schema_head") and code_head and data["schema_head"] != code_head:
        res["reason"] = "schema_head_mismatch"
        return res

    # every recorded service image must share one build id
    images = data.get("images") or {}
    build_ids = {v.get("build_id") for v in images.values() if isinstance(v, dict) and v.get("build_id")}
    if len(build_ids) > 1:
        res["reason"] = "service_build_mismatch"
        return res
    if images and data.get("build_id") and build_ids and data["build_id"] not in build_ids:
        res["warnings"].append("manifest_build_id_differs_from_images")

    # base images: manifest must record a digest-pinned ref per base (10A.1)
    base = data.get("base_images") or {}
    for key in ("python", "node"):
        b = base.get(key) if isinstance(base.get(key), dict) else None
        if not b:
            res["warnings"].append(f"base_{key}_not_recorded")
        elif not (str(b.get("digest") or "").startswith(f"{key}@sha256:")
                  or "@sha256:" in str(b.get("ref") or "")):
            res["warnings"].append(f"base_{key}_not_digest_pinned")

    # Python lock must be a real hash-pinned lock (10A.1)
    pl = data.get("python_lock") if isinstance(data.get("python_lock"), dict) else None
    if pl is None:
        res["warnings"].append("python_lock_status_not_recorded")
    else:
        if not pl.get("exact"):
            res["warnings"].append("python_lock_not_exact")
        if not pl.get("hashed"):
            res["warnings"].append("python_lock_not_hashed")

    if not data.get("sbom"):
        res["warnings"].append("no_sbom")
    scan = data.get("vulnerability_scan")
    if not scan:
        res["warnings"].append("no_vulnerability_scan")
    elif scan.get("status") == "unavailable":
        res["warnings"].append("vulnerability_scan_unavailable")
    if data.get("git_tree_clean") is False:
        res["warnings"].append("dirty_build")

    res["ok"] = True
    return res


# ---- summaries (release-check + API/UI + summary file) ----------------------
def read_summary_dict(data: dict) -> dict:
    """Whitelisted, leak-free summary of a manifest dict."""
    images = data.get("images") or {}
    scan = data.get("vulnerability_scan") or {}
    sbom = data.get("sbom") or {}
    apt = data.get("apt_packages") if isinstance(data.get("apt_packages"), dict) else {}
    integ = data.get(_INTEGRITY_FIELD) or {}
    build_ids = sorted({v.get("build_id") for v in images.values()
                        if isinstance(v, dict) and v.get("build_id")})
    digests = {k: _safe_basename(v.get("image_digest")) or v.get("image_digest")
               for k, v in images.items() if isinstance(v, dict) and v.get("image_digest")}
    rc = data.get("release_check") or {}
    base = data.get("base_images") or {}
    pl = data.get("python_lock") if isinstance(data.get("python_lock"), dict) else {}

    def _base_pinned(key: str) -> bool:
        b = base.get(key) if isinstance(base.get(key), dict) else None
        if not b:
            return False
        return (str(b.get("digest") or "").startswith(f"{key}@sha256:")
                or "@sha256:" in str(b.get("ref") or ""))

    return {
        "manifest_version": data.get("manifest_version"),
        "release_id": (str(data.get("release_id"))[:64] if data.get("release_id") else None),
        "app_version": (str(data.get("app_version"))[:40] if data.get("app_version") else None),
        "git_commit": (str(data.get("git_commit"))[:40] if data.get("git_commit") else None),
        "git_tree_clean": data.get("git_tree_clean") if isinstance(data.get("git_tree_clean"), bool) else None,
        "build_id": (str(data.get("build_id"))[:40] if data.get("build_id") else None),
        "schema_head": (str(data.get("schema_head"))[:32] if data.get("schema_head") else None),
        "frontend_build_id": (str(data.get("frontend_build_id"))[:40] if data.get("frontend_build_id") else None),
        "completed": data.get("completed") if isinstance(data.get("completed"), bool) else None,
        "service_build_ids": build_ids,
        "service_count": len(images),
        "image_digests_captured": len(digests),
        "sbom_present": bool(sbom and sbom.get("sha256")),
        "sbom_sha256": (str(sbom.get("sha256"))[:64] if sbom.get("sha256") else None),
        "vulnerability_status": scan.get("status"),
        "vulnerability_severities": scan.get("severities") if isinstance(scan.get("severities"), dict) else None,
        "vulnerability_tool": (str(scan.get("tool"))[:40] if scan.get("tool") else None),
        "vulnerability_tool_version": (str(scan.get("tool_version"))[:40] if scan.get("tool_version") else None),
        "vulnerability_db_updated_at": (str(scan.get("db_updated_at"))[:40] if scan.get("db_updated_at") else None),
        # Phase 10B: triage / provenance / exceptions (counts + statuses only)
        "scanner_provenance_status": (str((scan.get("scanner") or {}).get("provenance_status"))[:32]
                                      if isinstance(scan.get("scanner"), dict) else None),
        # Phase 10B.2: keep the scanner's content id and (real) registry digest
        # DISTINCT and non-secret. repo_digest is None unless a genuine, non-
        # synthetic RepoDigest was recorded (see vuln_triage.classify_scanner_provenance).
        "scanner_image_id": (str((scan.get("scanner") or {}).get("image_id"))[:80]
                             if isinstance(scan.get("scanner"), dict)
                             and (scan.get("scanner") or {}).get("image_id") else None),
        "scanner_repo_digest": (str((scan.get("scanner") or {}).get("repo_digest"))[:160]
                                if isinstance(scan.get("scanner"), dict)
                                and (scan.get("scanner") or {}).get("repo_digest") else None),
        "critical_unapproved": (scan.get("critical_unapproved")
                                if isinstance(scan.get("critical_unapproved"), int) else None),
        "critical_covered": (scan.get("critical_covered")
                             if isinstance(scan.get("critical_covered"), int) else None),
        "vulnerability_report_integrity": (scan.get("report_integrity_valid")
                                           if isinstance(scan.get("report_integrity_valid"), bool) else None),
        "vulnerability_exceptions_active": (scan.get("exceptions_active")
                                            if isinstance(scan.get("exceptions_active"), int) else None),
        "vulnerability_exceptions_expired": (scan.get("exceptions_expired")
                                             if isinstance(scan.get("exceptions_expired"), int) else None),
        "vulnerability_exceptions_invalid": (scan.get("exceptions_invalid")
                                             if isinstance(scan.get("exceptions_invalid"), int) else None),
        "base_remediation_status": (str(data.get("base_remediation_status"))[:24]
                                    if data.get("base_remediation_status") else None),
        "dependency_remediation_status": (str(data.get("dependency_remediation_status"))[:24]
                                          if data.get("dependency_remediation_status") else None),
        "release_check_overall": rc.get("overall"),
        "integrity_scheme": (str(integ.get("scheme"))[:20] if integ.get("scheme") else None),
        "backend_test_count": data.get("backend_test_count") if isinstance(data.get("backend_test_count"), int) else None,
        "frontend_test_count": data.get("frontend_test_count") if isinstance(data.get("frontend_test_count"), int) else None,
        # Phase 10A.1 supply-chain gates
        "python_lock_exact": bool(pl.get("exact")) if pl else None,
        "python_lock_hashed": bool(pl.get("hashed")) if pl else None,
        "python_lock_package_count": pl.get("package_count") if isinstance(pl.get("package_count"), int) else None,
        "base_python_digest_pinned": _base_pinned("python"),
        "base_node_digest_pinned": _base_pinned("node"),
        # Phase 10B.2 apt reproducibility (recorded package set + sha256; status is
        # `recorded_unpinned` because the apt transaction is not snapshot-pinned —
        # deps/base are fixed but a rebuild MAY differ if the apt repo moved).
        "apt_package_count": (apt.get("package_count") if isinstance(apt.get("package_count"), int) else None),
        "apt_packages_sha256": (str(apt.get("sha256"))[:64] if apt.get("sha256") else None),
        "apt_packages_pinned": ("pinned" if apt.get("pinned") is True
                                else ("recorded_unpinned" if apt.get("sha256") else "not_recorded")),
        "apt_reproducibility": (str(apt.get("reproducibility"))[:48] if apt.get("reproducibility") else None),
        "apt_targeted_versions": (apt.get("targeted_versions")
                                  if isinstance(apt.get("targeted_versions"), dict) else None),
    }


def read_release_manifest_summary(settings: Settings | None = None) -> dict | None:
    settings = settings or get_settings()
    path = (settings.release_manifest_summary_file or "").strip()
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert isinstance(data, dict)
    except Exception:  # noqa: BLE001
        return None
    # summary file may already be a summary; normalise either way
    return read_summary_dict(data) if "images" in data or _INTEGRITY_FIELD in data else data
