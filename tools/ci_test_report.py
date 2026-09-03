"""Run the test suite and emit a per-test report.

Written for CI. The stock unittest output says how many tests failed but not, in
one readable place, which ones passed, which failed and why. This produces:

  - a Markdown report (for the GitHub Actions job summary and the notification
    email body)
  - a plain-text report (for the build log and as an artifact)

Stdlib only, using unittest directly, to match how the rest of the suite is
written. Exit code is 0 when everything passed or skipped, 1 otherwise.

Usage:
    python tools/ci_test_report.py [--start-dir .] [--pattern "test_*.py"]
                                   [--markdown out.md] [--text out.txt]
                                   [--title "Unit tests"]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
import unittest


class RecordingResult(unittest.TestResult):
    """Collects an outcome and duration for every test."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict] = []
        self._started_at = 0.0

    def startTest(self, test) -> None:
        self._started_at = time.perf_counter()
        super().startTest(test)

    def _record(self, test, outcome: str, detail: str = "") -> None:
        self.records.append(
            {
                "id": test.id(),
                "name": str(test),
                "outcome": outcome,
                "detail": detail.strip(),
                "seconds": time.perf_counter() - self._started_at,
            }
        )

    def addSuccess(self, test) -> None:
        super().addSuccess(test)
        self._record(test, "passed")

    def addFailure(self, test, err) -> None:
        super().addFailure(test, err)
        self._record(test, "failed", self._exc_text(err))

    def addError(self, test, err) -> None:
        super().addError(test, err)
        self._record(test, "error", self._exc_text(err))

    def addSkip(self, test, reason) -> None:
        super().addSkip(test, reason)
        self._record(test, "skipped", reason)

    def addExpectedFailure(self, test, err) -> None:
        super().addExpectedFailure(test, err)
        self._record(test, "expected failure", self._exc_text(err))

    def addUnexpectedSuccess(self, test) -> None:
        super().addUnexpectedSuccess(test)
        self._record(test, "unexpected success", "This test was expected to fail but passed.")

    @staticmethod
    def _exc_text(err) -> str:
        return "".join(traceback.format_exception(*err))


BAD = {"failed", "error", "unexpected success"}


def collect_load_errors(suite) -> list[str]:
    """unittest turns an unimportable module into a _FailedTest placeholder.

    Those still run as errors, so they are reported normally; this just makes
    the module-level import problem obvious in the summary.
    """
    problems = []
    for test in iterate(suite):
        if type(test).__name__ == "_FailedTest":
            problems.append(test.id())
    return problems


def iterate(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iterate(item)
        else:
            yield item


def render_markdown(title: str, records: list[dict], duration: float) -> str:
    counts: dict[str, int] = {}
    for r in records:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1

    failed = [r for r in records if r["outcome"] in BAD]
    ok = not failed

    lines = [
        f"# {title}",
        "",
        f"**{'All tests passed' if ok else 'Tests failed'}** "
        f"- {len(records)} tests in {duration:.2f}s",
        "",
        "| Outcome | Count |",
        "| --- | --- |",
    ]
    for outcome in ("passed", "failed", "error", "skipped", "expected failure", "unexpected success"):
        if counts.get(outcome):
            lines.append(f"| {outcome} | {counts[outcome]} |")
    lines.append("")

    if failed:
        lines += ["## Failures", ""]
        for r in failed:
            lines += [
                f"### {r['name']}",
                "",
                f"`{r['id']}` - {r['outcome']} after {r['seconds']:.3f}s",
                "",
                "```text",
                r["detail"] or "(no detail)",
                "```",
                "",
            ]

    lines += ["## All tests", "", "| Test | Outcome | Seconds |", "| --- | --- | --- |"]
    for r in records:
        note = ""
        if r["outcome"] == "skipped" and r["detail"]:
            note = f" - {r['detail']}"
        lines.append(f"| `{r['id']}` | {r['outcome']}{note} | {r['seconds']:.3f} |")
    lines.append("")
    return "\n".join(lines)


def render_text(title: str, records: list[dict], duration: float) -> str:
    failed = [r for r in records if r["outcome"] in BAD]
    lines = [
        title,
        "=" * len(title),
        "",
        f"{'All tests passed' if not failed else 'TESTS FAILED'}"
        f" - {len(records)} tests in {duration:.2f}s",
        "",
    ]
    for r in records:
        suffix = f"  ({r['detail']})" if r["outcome"] == "skipped" and r["detail"] else ""
        lines.append(f"{r['outcome'].upper():<18} {r['id']}{suffix}")
    if failed:
        lines += ["", "Failure detail", "--------------", ""]
        for r in failed:
            lines += [f"{r['outcome'].upper()}: {r['id']}", r["detail"] or "(no detail)", ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-dir", default=".")
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--top-level-dir", default=None)
    parser.add_argument("--markdown", default="")
    parser.add_argument("--text", default="")
    parser.add_argument("--title", default="Test results")
    args = parser.parse_args()

    suite = unittest.defaultTestLoader.discover(
        args.start_dir, pattern=args.pattern, top_level_dir=args.top_level_dir
    )
    load_errors = collect_load_errors(suite)
    if load_errors:
        print("Modules that could not be imported:", file=sys.stderr)
        for name in load_errors:
            print(f"  {name}", file=sys.stderr)

    result = RecordingResult()
    started = time.perf_counter()
    suite.run(result)
    duration = time.perf_counter() - started

    markdown = render_markdown(args.title, result.records, duration)
    text = render_text(args.title, result.records, duration)

    print(text)

    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as f:
            f.write(markdown)
    if args.text:
        with open(args.text, "w", encoding="utf-8") as f:
            f.write(text)

    # GitHub Actions job summary, when running there.
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")

    return 1 if any(r["outcome"] in BAD for r in result.records) else 0


if __name__ == "__main__":
    sys.exit(main())
