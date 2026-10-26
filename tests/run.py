"""Run the unittest suite and write a JSON report for the webapp.

Writes data/test_results.json:
    {"ran_at": iso, "duration": seconds, "total": int, "passed": int,
     "failed": int, "tests": [{"id", "name", "module", "description",
                               "status": passed|failed|error|skipped,
                               "message", "duration"}]}

Exit code 0 when every test passed, 1 otherwise. Pure stdlib.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

RESULTS_PATH = ROOT / "data" / "test_results.json"


def module_group(test) -> str:
    """Human-readable module name from the test class, e.g.
    ServerTeachAgentApiTests -> 'Teach Agent Api', TestScraperParsers -> 'Scraper Parsers'."""
    cls = type(test).__name__
    name = re.sub(r"^(Test|Server)", "", cls)
    name = re.sub(r"Tests$", "", name)
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).strip()
    return words or "General"


class Collector(unittest.TestResult):
    """Records one entry per test with timing and plain-language names."""

    def __init__(self):
        super().__init__()
        self.entries = []
        self._starts = {}

    def _doc(self, test):
        doc = (getattr(test, "_testMethodDoc", None) or "").strip()
        return " ".join(doc.split())

    def startTest(self, test):
        super().startTest(test)
        self._starts[test.id()] = time.perf_counter()

    def _record(self, test, status, message=""):
        short = test.id().split(".")[-1]
        name = short.replace("test_", "").replace("_", " ")
        self.entries.append({
            "id": test.id(),
            "name": name,
            "module": module_group(test),
            "class": type(test).__name__,
            "description": self._doc(test),
            "status": status,
            "message": message.strip()[:800] if message else "",
            "duration": round(time.perf_counter() - self._starts.get(test.id(), time.perf_counter()), 3),
        })

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "failed", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, "skipped", reason or "")


def main() -> int:
    os.chdir(ROOT)
    suite = unittest.TestLoader().discover(
        str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT)
    )
    collector = Collector()
    t0 = time.perf_counter()
    suite.run(collector)
    duration = round(time.perf_counter() - t0, 2)

    passed = sum(1 for e in collector.entries if e["status"] == "passed")
    failed = len(collector.entries) - passed
    report = {
        "ran_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "duration": duration,
        "total": len(collector.entries),
        "passed": passed,
        "failed": failed,
        "tests": sorted(collector.entries, key=lambda e: e["id"]),
    }
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    # console overview (this is what the user sees when double-clicking the icon)
    print("=" * 64)
    print(f"  TEST RESULTS: {passed}/{len(collector.entries)} passed"
          f" in {duration}s  ({report['ran_at']})")
    print("=" * 64)
    for e in report["tests"]:
        mark = {"passed": "PASS", "failed": "FAIL", "error": "ERR ", "skipped": "SKIP"}[e["status"]]
        line = f"  [{mark}] {e['module']} · {e['name']}"
        desc = e["description"]
        if desc:
            line += f" — {desc}"
        print(line)
    if failed:
        print("-" * 64)
        print("FAILURES:")
        for e in report["tests"]:
            if e["status"] in ("failed", "error"):
                print(f"\n  [{e['status'].upper()}] {e['id']}")
                print("  " + e["message"].replace("\n", "\n  ")[:600])
        print("-" * 64)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
