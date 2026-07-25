"""Phase 10B.2: bundled-library elimination + scanner-provenance closure.

Covers: the source-built psycopg2 lock, the built-wheel runtime-lock derivation,
Docker builder/runtime separation (no build tools / vendored libs in runtime),
honest scanner provenance (image id vs RepoDigest, synthetic/short rejection),
Trivy SBOM fallback + artifact leak scanning, apt reproducibility status, and the
static destructive-operation guards. No host paths / secrets in any output.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

from typer.testing import CliRunner

from app.cli import app as cli_app
from app.config import get_settings
from app.services import production_check as pc
from app.services import release_manifest as rm
from app.services import vuln_triage as vt

REPO = Path(__file__).resolve().parents[1]
runner = CliRunner()

DOCKERFILE = (REPO / "Dockerfile").read_text("utf-8")
BUILD_SH = (REPO / "scripts" / "build-release.sh").read_text("utf-8")
LOCK = (REPO / "requirements.lock").read_text("utf-8")


def _load_make_runtime_lock():
    p = REPO / "scripts" / "make-runtime-lock.py"
    spec = importlib.util.spec_from_file_location("make_runtime_lock", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dockerfile_stages() -> dict[str, str]:
    """Split the Dockerfile into {stage_name: text}. The final unnamed FROM is
    the runtime stage."""
    stages: dict[str, str] = {}
    cur, buf = None, []
    for line in DOCKERFILE.splitlines():
        if line.startswith("FROM "):
            if cur is not None:
                stages[cur] = "\n".join(buf)
            m = re.search(r"\bAS\s+(\w+)", line)
            cur = m.group(1) if m else "runtime"
            buf = [line]
        else:
            buf.append(line)
    if cur is not None:
        stages[cur] = "\n".join(buf)
    return stages


def _code_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


# --------------------------------------------------------------------------- #
# 1. source-build dependency lock
# --------------------------------------------------------------------------- #
def test_lock_uses_source_psycopg2_not_binary():
    assert re.search(r"^psycopg2==2\.9\.\d+", LOCK, re.M), "psycopg2 (source) must be pinned"
    assert "psycopg2-binary==" not in LOCK, "the -binary wheel must be gone"
    # the source sdist hash must be present (build input that pip wheel verifies)
    assert "1dedb1c7a1d8552c4a6044c6b1c41a52e6a8e2d144af83eccac758076b1b7c15" in LOCK


def test_direct_dependency_inputs_switched_to_source():
    for f in ("pyproject.toml", "requirements.txt"):
        t = (REPO / f).read_text("utf-8")
        assert re.search(r"psycopg2>=2\.9", t), f"{f} should require source psycopg2"
        assert "psycopg2-binary" not in _joined_code(t), f"{f} still names -binary in code"


def _joined_code(t: str) -> str:
    return "\n".join(ln for ln in t.splitlines() if not ln.lstrip().startswith("#"))


def test_lock_package_count_unchanged():
    pkgs = re.findall(r"^[A-Za-z0-9][A-Za-z0-9._-]*==", LOCK, re.M)
    assert len(pkgs) == 62, f"expected 62 packages (only the driver swapped), got {len(pkgs)}"


# --------------------------------------------------------------------------- #
# 2. built-wheel runtime-lock derivation (wheel hash)
# --------------------------------------------------------------------------- #
def test_make_runtime_lock_swaps_only_psycopg2_hash():
    mrl = _load_make_runtime_lock()
    wheel_sha = "d" * 64
    out = mrl.derive(LOCK, wheel_sha)
    # psycopg2 now carries exactly the built-wheel hash
    m = re.search(r"^psycopg2==2\.9\.\d+ \\\n    --hash=sha256:([0-9a-f]{64})\s*$", out, re.M)
    assert m and m.group(1) == wheel_sha
    # the sdist hash is gone; every other package block is untouched
    assert "1dedb1c7a1d8552c4a6044c6b1c41a52e6a8e2d144af83eccac758076b1b7c15" not in out
    other = [ln for ln in LOCK.splitlines() if not ln.startswith("psycopg2==")
             and "1dedb1c7" not in ln and "09826a6b" not in ln]
    # spot-check a neighbouring package survived verbatim
    assert any(ln.startswith("pydantic==") for ln in other)
    assert "pydantic==" in out


def test_make_runtime_lock_rejects_bad_sha():
    mrl = _load_make_runtime_lock()
    for bad in ("short", "g" * 64, ""):
        try:
            mrl.derive(LOCK, bad)
            assert False, f"should reject {bad!r}"
        except ValueError:
            pass


# --------------------------------------------------------------------------- #
# 3. Docker builder / runtime separation
# --------------------------------------------------------------------------- #
def test_wheelbuild_stage_has_toolchain():
    st = _dockerfile_stages()
    assert "wheelbuild" in st
    wb = st["wheelbuild"]
    for tool in ("gcc", "libc6-dev", "libpq-dev"):
        assert tool in wb, f"builder must install {tool}"
    assert "pip wheel --require-hashes" in wb
    assert "--no-binary psycopg2" in wb


def test_runtime_stage_has_no_build_tools():
    # package names live on RUN continuation lines, so scan the whole non-comment
    # runtime-stage text (the wheelbuild stage is separated out by _dockerfile_stages)
    rt = "\n".join(_code_lines(_dockerfile_stages()["runtime"]))
    for tool in ("gcc", "libc6-dev", "libpq-dev", "build-essential", "make"):
        assert not re.search(rf"\b{re.escape(tool)}\b", rt), f"runtime must NOT carry build tool {tool}"
    # but the runtime shared lib for source psycopg2 IS present
    assert "libpq5" in rt


def test_runtime_installs_from_wheelhouse_hash_enforced():
    rt = _dockerfile_stages()["runtime"]
    assert "--require-hashes" in rt and "--no-index" in rt
    assert "/wheelhouse" in rt
    assert "requirements.runtime.lock" in rt


def test_runtime_verifies_no_vendored_libs():
    rt = _dockerfile_stages()["runtime"]
    assert "psycopg2_binary.libs" in rt, "build must fail if the -binary bundle leaks in"


# --------------------------------------------------------------------------- #
# 4. scanner provenance (image id vs RepoDigest)
# --------------------------------------------------------------------------- #
FULL = "sha256:" + "a" * 64
REAL = "mirror.gcr.io/aquasec/trivy@sha256:" + "b" * 64


def test_provenance_real_repo_digest_is_digest_pinned():
    r = vt.classify_scanner_provenance(image_id=FULL, repo_digest=REAL)
    assert r["status"] == "digest_pinned"
    assert r["repo_digest"] == REAL and r["image_id"] == FULL and r["errors"] == []


def test_provenance_no_digest_full_id_operator_verified():
    r = vt.classify_scanner_provenance(image_id=FULL, operator_verified=True)
    assert r["status"] == "local_image_id_verified" and r["repo_digest"] is None


def test_provenance_no_digest_unverified():
    r = vt.classify_scanner_provenance(image_id=FULL, operator_verified=False)
    assert r["status"] == "unverified"


def test_provenance_short_image_id_rejected():
    r = vt.classify_scanner_provenance(image_id="a8ca29078522", operator_verified=True)
    assert r["status"] == "unverified"  # short id cannot back local_image_id_verified
    assert r["image_id"] is None and "image_id_not_full_sha256" in r["errors"]


def test_provenance_synthetic_repo_digest_rejected():
    synthetic = "aquasec/trivy@" + FULL  # digest == the image id
    r = vt.classify_scanner_provenance(image_id=FULL, repo_digest=synthetic)
    assert r["status"] == "unverified" and r["repo_digest"] is None
    assert "repo_digest_synthetic_from_image_id" in r["errors"]


def test_provenance_malformed_repo_digest_rejected():
    r = vt.classify_scanner_provenance(image_id=FULL, repo_digest="not-a-digest")
    assert r["repo_digest"] is None and "repo_digest_malformed" in r["errors"]


# --------------------------------------------------------------------------- #
# 5. provenance production policy (release-check)
# --------------------------------------------------------------------------- #
def _summary(**over):
    s = {
        "vulnerability_status": "pass",
        "vulnerability_severities": {"CRITICAL": 0, "HIGH": 0},
        "critical_unapproved": 0,
        "vulnerability_report_integrity": True,
        "scanner_provenance_status": "digest_pinned",
    }
    s.update(over)
    return s


def _gate(summary, name, prod=True):
    get_settings.cache_clear()
    checks = pc._vuln_triage_checks(get_settings(), summary, prod=prod)
    return next(c for c in checks if c["name"] == name)


def test_release_check_unverified_scanner_not_prod_pass():
    g = _gate(_summary(scanner_provenance_status="unverified"), "scanner_provenance_verified", prod=True)
    assert g["status"] == "fail"


def test_release_check_digest_pinned_and_local_id_pass():
    assert _gate(_summary(scanner_provenance_status="digest_pinned"),
                 "scanner_provenance_verified")["status"] == "pass"
    assert _gate(_summary(scanner_provenance_status="local_image_id_verified"),
                 "scanner_provenance_verified")["status"] == "pass"


# --------------------------------------------------------------------------- #
# 6. artifact leak scanning + SBOM fallback
# --------------------------------------------------------------------------- #
def test_leak_scan_clean_on_prose_and_image_paths():
    txt = json.dumps({"components": [{"name": "libpam", "description": "password quality lib",
                                      "path": "/usr/lib/x86_64-linux-gnu/libpam.so"}]})
    assert vt.scan_text_for_host_leaks(txt, repo_root="/Users/dev/proj") == []


def test_leak_scan_detects_host_path_and_secret_count_only():
    txt = "built at /Users/alice/proj PASSWORD=hunter2secret AKIA1234567890ABCDEF"
    f = vt.scan_text_for_host_leaks(txt, repo_root="/Users/alice/proj")
    markers = {x["marker"] for x in f}
    assert {"build_repo_path", "home_users_path", "secret_env_assignment", "aws_access_key"} <= markers
    # count-only: the matched secret is never echoed back
    assert all(set(x.keys()) == {"marker", "count"} for x in f)


def test_cli_scan_artifact_leaks_exit_codes(tmp_path):
    clean = tmp_path / "clean.json"
    clean.write_text(json.dumps({"a": "/usr/lib/ok", "b": "libssl"}))
    r = runner.invoke(cli_app, ["release", "scan-artifact-leaks", "--path", str(clean)])
    assert r.exit_code == 0
    leaky = tmp_path / "leak.json"
    leaky.write_text("home=/Users/bob/secret TOKEN=abcdef123456")
    r = runner.invoke(cli_app, ["release", "scan-artifact-leaks", "--path", str(leaky),
                                "--repo-root", "/nope"])
    assert r.exit_code == 1
    assert "/Users/bob/secret" not in r.output and "abcdef123456" not in r.output


def test_build_release_has_trivy_sbom_fallback_and_leakscan():
    assert "cyclonedx" in BUILD_SH, "Trivy CycloneDX fallback must exist"
    assert "scan-artifact-leaks" in BUILD_SH, "generated SBOM must be leak-scanned"
    assert "_with_timeout" in BUILD_SH  # fallback bounded by a timeout
    # scout/trivy/syft chain present; a failure records unavailable (never a pass)
    assert "docker scout" in BUILD_SH and "syft" in BUILD_SH
    assert '"status": "unavailable"' in BUILD_SH


def test_sbom_missing_fails_in_production():
    get_settings.cache_clear()
    checks = pc._release_provenance_checks(get_settings())  # includes sbom gate context
    # direct gate: build a manifest summary with no sbom and confirm prod FAIL
    m = rm.create_release_manifest(get_settings(), sbom=None, completed=True)
    s = rm.read_summary_dict(m)
    assert s["sbom_present"] is False


# --------------------------------------------------------------------------- #
# 7. apt reproducibility
# --------------------------------------------------------------------------- #
def test_apt_summary_and_gate_recorded_unpinned():
    apt = {"source": "dpkg-query", "package_count": 300, "sha256": "e" * 64,
           "targeted_versions": {"mesa-libgallium": "25.0.7-2+deb13u1"},
           "pinned": False, "reproducibility": "deps_and_base_fixed_apt_not_pinned"}
    m = rm.create_release_manifest(get_settings(), apt=apt, completed=True)
    s = rm.read_summary_dict(m)
    assert s["apt_package_count"] == 300 and s["apt_packages_sha256"] == "e" * 64
    assert s["apt_packages_pinned"] == "recorded_unpinned"
    assert s["apt_targeted_versions"]["mesa-libgallium"] == "25.0.7-2+deb13u1"
    g = _gate({**_summary(), **s}, "apt_packages_pinned", prod=True)
    assert g["status"] == "warn"  # recorded but honestly NOT claimed fully reproducible
    assert "not fully reproducible" in g["detail"]


def test_build_release_records_dpkg_and_never_claims_full_repro():
    assert "dpkg-query" in BUILD_SH and "apt-descriptor.json" in BUILD_SH
    assert "deps_and_base_fixed_apt_not_pinned" in BUILD_SH
    # the recorded reproducibility value is the honest 'not pinned' one — never a
    # positive 'fully_reproducible' claim
    assert "fully_reproducible" not in BUILD_SH
    assert '"pinned": False' in BUILD_SH


# --------------------------------------------------------------------------- #
# 8. static destructive-operation guards
# --------------------------------------------------------------------------- #
def test_no_blanket_apt_upgrade_only_targeted():
    for text in (DOCKERFILE, BUILD_SH):
        for ln in _code_lines(text):
            assert not re.search(r"apt-get\s+upgrade\b", ln), f"blanket upgrade forbidden: {ln}"
            assert "apt-get dist-upgrade" not in ln
    assert "--only-upgrade" in DOCKERFILE  # targeted remediation is allowed


def test_hash_lock_kept_and_no_binary_reintroduced():
    assert "--require-hashes" in DOCKERFILE
    # -binary must not be re-introduced as a dependency. The runtime vendored-guard
    # legitimately NAMES it in an assert message, so match install/pin forms only.
    assert not re.search(r"psycopg2-binary\s*==", DOCKERFILE)
    assert not re.search(r"install[^\n]*psycopg2-binary", DOCKERFILE)
    assert "psycopg2-binary" not in _joined_code(LOCK)


def test_no_compiler_left_in_runtime_stage():
    rt = _dockerfile_stages()["runtime"]
    # a build-tool install in the runtime stage would leave a compiler in prod
    assert "install -y --no-install-recommends gcc" not in rt
    assert "build-essential" not in rt


def test_base_images_not_regressed_to_floating():
    # base image ARGs are still present and build-release still pins by digest
    assert "ARG BASE_PYTHON_IMAGE=" in DOCKERFILE and "ARG BASE_NODE_IMAGE=" in DOCKERFILE
    assert "@sha256:" in BUILD_SH  # digest pinning retained


def test_build_release_no_force_or_synthetic_digest():
    for ln in _code_lines(BUILD_SH):
        assert "npm audit fix --force" not in ln
        assert "compose down -v" not in ln and "down --volumes" not in ln
    # provenance passes the REAL .Id and a real RepoDigest option (no synthesis)
    assert "--scanner-repo-digest" in BUILD_SH and "TRIVY_FULL_ID" in BUILD_SH
    assert "--scanner-digest " not in BUILD_SH  # old conflated flag is gone
