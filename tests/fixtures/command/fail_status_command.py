"""Fixture command: prints a FAIL Diagnostic and exits 0.

The contract says: emit JSON Diagnostic on stdout, exit 0 even on fail.
This fixture lets the adapter return a real ``Status.FAIL`` Diagnostic via
the parsed-output path.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    payload = json.loads(sys.stdin.read())
    rule = payload.get("rule", {})
    diagnostic = {
        "status": "fail",
        "message": f"rule {rule.get('id', '?')} failed in fixture",
        "evidence": [
            {"kind": "fixture_fail", "data": {"rule_id": rule.get("id")}},
        ],
        "remediation": "edit the artifact to satisfy the rule",
    }
    sys.stdout.write(json.dumps(diagnostic))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
