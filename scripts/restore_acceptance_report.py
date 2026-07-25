"""Phase 9F: build the machine-readable restore acceptance report.

Consumes the TSV item log written by scripts/restore-rehearsal.sh
(``name<TAB>status<TAB>expected<TAB>detail`` per line, status pass|fail|info,
expected yes|no where "yes" marks a failure that is EXPECTED in an isolated
rehearsal — e.g. the full archive presence check while the real archive is
deliberately not attached).

The report is JSON and must stay free of host paths / secrets — the builder
refuses to emit a report whose details contain absolute-path markers.
Exit code 0 = acceptance PASSED (no unexpected failures), 1 = FAILED.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPORT_VERSION = 1
_PATH_MARKERS = ("/Users/", "/home/", "/var/folders/", "/private/var/", "/tmp/", "/secrets/")
_STATUSES = {"pass", "fail", "info"}


def build(items_path: Path, *, project: str, dump: str) -> dict:
    items: list[dict] = []
    problems: list[str] = []
    for i, line in enumerate(items_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            problems.append(f"line {i}: expected 4 tab-separated fields")
            continue
        name, status, expected, detail = (p.strip() for p in parts)
        if status not in _STATUSES:
            problems.append(f"line {i}: bad status '{status}'")
            continue
        for marker in _PATH_MARKERS:
            if marker in detail:
                problems.append(f"line {i}: detail leaks a host path marker")
                detail = "[detail withheld: host path marker]"
                break
        items.append({"name": name, "status": status,
                      "expected_failure": expected == "yes", "detail": detail})

    summary = {
        "total": len(items),
        "pass": sum(1 for x in items if x["status"] == "pass"),
        "info": sum(1 for x in items if x["status"] == "info"),
        "fail_expected": sum(1 for x in items if x["status"] == "fail" and x["expected_failure"]),
        "fail_unexpected": sum(1 for x in items if x["status"] == "fail" and not x["expected_failure"]),
    }
    return {
        "report_version": REPORT_VERSION,
        "kind": "restore_acceptance",
        "project": project,
        "dump_artifact": dump,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "summary": summary,
        "builder_problems": problems,
        "ok": summary["fail_unexpected"] == 0 and not problems and summary["total"] > 0,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--dump", required=True, help="dump artifact BASENAME (no paths)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if "/" in args.dump:
        print("refusing: --dump must be a basename, not a path")
        sys.exit(2)
    report = build(Path(args.items), project=args.project, dump=args.dump)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    s = report["summary"]
    print(f"restore acceptance: ok={report['ok']} total={s['total']} pass={s['pass']} "
          f"info={s['info']} fail_expected={s['fail_expected']} "
          f"fail_unexpected={s['fail_unexpected']}")
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
