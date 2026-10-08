"""Enforce the PRD §48 coverage gates from a ``coverage json`` report.

Run after ``pytest --cov`` has produced ``.coverage``::

    coverage json -o .coverage.json
    python scripts/check_coverage.py .coverage.json

Gates are **line** coverage (PRD §48): vision core ≥ 85%, whole backend ≥ 75%.
The script exits non-zero when a gate is missed, so CI fails loudly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

GATES: list[tuple[str, float]] = [
    ("backend/visionops/vision/", 85.0),
    ("backend/visionops/", 75.0),
]


def line_coverage(files: dict[str, object], prefix: str) -> tuple[float, int, int]:
    total = 0
    covered = 0
    for name, raw in files.items():
        if not name.startswith(prefix):
            continue
        summary = raw["summary"]  # type: ignore[index]
        total += int(summary["num_statements"])  # type: ignore[index]
        covered += int(summary["covered_lines"])  # type: ignore[index]
    percent = (100.0 * covered / total) if total else 0.0
    return percent, covered, total


def main(argv: list[str]) -> int:
    report = Path(argv[1] if len(argv) > 1 else ".coverage.json")
    if not report.exists():
        print(f"coverage report not found: {report}", file=sys.stderr)
        return 2
    payload = json.loads(report.read_text(encoding="utf-8"))
    files = payload.get("files", {})

    failures: list[str] = []
    for prefix, minimum in GATES:
        percent, covered, total = line_coverage(files, prefix)
        status = "ok" if percent >= minimum else "FAIL"
        print(f"{status:4} {prefix:32} {percent:5.1f}%", end="")
        print(f" (gate {minimum:.0f}%, {covered}/{total} lines)")
        if percent < minimum:
            failures.append(f"{prefix} line coverage {percent:.1f}% < {minimum:.0f}%")

    if failures:
        for failure in failures:
            print(f"coverage gate failed: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
