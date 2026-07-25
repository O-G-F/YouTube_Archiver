#!/usr/bin/env python3
"""Phase 10B.2: derive a RUNTIME lock from the committed requirements.lock.

Why this exists
---------------
`requirements.lock` pins psycopg2 (source) by its *sdist* sha256 — that is the
reproducible build INPUT and it is what `pip wheel --require-hashes` verifies in
the builder stage before compiling. But once psycopg2 is compiled into a local
wheel, that wheel has a DIFFERENT sha256 (a source build has no pre-known wheel
hash). To keep `--require-hashes` fully enforced at the *runtime* install too, we
emit a runtime lock whose psycopg2 block carries the BUILT WHEEL's sha256 while
every other package block is copied byte-for-byte (unchanged, still PyPI-hashed).

The runtime image then runs:
    pip install --require-hashes --no-index --find-links /wheelhouse \
        -r requirements.runtime.lock
so every installed package — including the source-built psycopg2 — is hash
verified against a closed, offline wheelhouse. This is NOT a hash-lock relaxation:
the committed lock is unchanged; this derived file only swaps the sdist hash for
the exact built-wheel hash produced from that same hash-verified sdist.

The built wheel's sha256 is build-derived (may vary if the compiler / libpq
headers change; SOURCE_DATE_EPOCH pins timestamps). scripts/build-release.sh
records it in the release manifest as provenance.

usage: make-runtime-lock.py <requirements.lock> <built-wheel-sha256> <out.lock>
"""

from __future__ import annotations

import re
import sys


def derive(src_text: str, wheel_sha: str) -> str:
    wheel_sha = wheel_sha.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", wheel_sha):
        raise ValueError(f"expected a 64-hex sha256, got {wheel_sha!r}")
    lines = src_text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    replaced = False
    while i < n:
        ln = lines[i]
        if re.match(r"^psycopg2==", ln):
            head = ln.split()[0]  # 'psycopg2==2.9.12' (drop any trailing ' \\')
            out.append(f"{head} \\")
            out.append(f"    --hash=sha256:{wheel_sha}")
            replaced = True
            i += 1
            # consume the original (sdist/wheel) --hash continuation lines
            while i < n and lines[i].lstrip().startswith("--hash="):
                i += 1
            continue
        out.append(ln)
        i += 1
    if not replaced:
        raise ValueError("no `psycopg2==` block found in the lock")
    return "\n".join(out) + "\n"


def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(2)
    src, wheel_sha, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    text = open(src, encoding="utf-8").read()
    result = derive(text, wheel_sha)
    open(out_path, "w", encoding="utf-8").write(result)
    print(f"wrote {out_path}: psycopg2 hash -> built wheel {wheel_sha[:12]}…")


if __name__ == "__main__":
    main()
