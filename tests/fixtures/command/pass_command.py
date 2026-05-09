"""Fixture command: reads stdin JSON, prints a PASS Diagnostic.

Used by ``tests/test_command_adapter.py`` end-to-end test that exercises a
real subprocess. Always exits 0 regardless of pass/fail outcome — the
command adapter contract requires a JSON Diagnostic on stdout.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    payload = json.loads(sys.stdin.read())
    target = payload.get("target", "")
    diagnostic = {
        "status": "pass",
        "message": f"fixture command saw target={target}",
        "evidence": [
            {"kind": "fixture_pass", "data": {"echo_target": target}},
        ],
    }
    sys.stdout.write(json.dumps(diagnostic))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
