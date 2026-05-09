"""Fixture command: reads stdin JSON, prints a FAIL Diagnostic, exits non-zero.

Tests the strict contract that any non-zero exit collapses to UNAVAILABLE
even if the stdout would have parsed cleanly.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    sys.stdin.read()
    diagnostic = {
        "status": "fail",
        "message": "fixture command reports a violation",
        "evidence": [
            {"kind": "fixture_fail", "data": {"reason": "test"}},
        ],
        "remediation": "fix the violation",
    }
    sys.stdout.write(json.dumps(diagnostic))
    sys.stderr.write("fixture command exiting non-zero on purpose\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
