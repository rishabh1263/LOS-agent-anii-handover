"""
Run the Credit Underwriting Agent eval suite (in-process, synthetic DEMO data).

    python -m evals.credit.run                    # table + pass/fail
    python -m evals.credit.run --report out.json  # full per-dimension report

Each case gets a fresh temporary SQLite store; case memory is on; the memo's
optional model call is off (deterministic) unless --live is given.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report")
    parser.add_argument("--live", action="store_true", help="allow the memo's Qwen call")
    args = parser.parse_args(argv)

    os.environ.setdefault("AGENT_RUN_AUDIT_ENABLED", "false")
    os.environ["CREDIT_MEMO_LLM_ENABLED"] = "true" if args.live else "false"

    from app.agents.runtime.evals import runner
    from app.agents.runtime.evals.contracts import SuiteReport
    from app.store import set_repository
    from app.store.testing import fresh_repository
    from evals.credit.suite import CASES, execute_in_process

    results = []
    # ignore_cleanup_errors: SQLite keeps its file open on Windows until GC.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp, \
            patch("app.agents.los.config.case_memory_enabled", return_value=True):
        for i, (case, setup) in enumerate(CASES):
            repository = fresh_repository(Path(tmp) / f"{i}.sqlite3")
            repository.initialise()
            set_repository(repository)
            run = await execute_in_process(case, setup, repository)
            result = runner.evaluate(run, case.expect, case_id=case.case_id)
            results.append(result)
            status = (run.output or {}).get("status")
            print(f"{'PASS' if result.passed else 'FAIL'}  {case.case_id:<26} "
                  f"{status or run.status:<20} tools={run.usage.get('tool_calls')} "
                  f"replans={run.usage.get('replans')} retries={run.usage.get('retries')} "
                  f"{run.duration_ms:7.1f} ms")
            for failure in result.failures():
                print(f"      - {failure.dimension}: {failure.detail}")
        set_repository(None)

    report = SuiteReport(agent_id="credit_agent", results=results)
    print(f"\n{report.passed}/{report.total} passed; latency {report.latency()}")
    if args.report:
        Path(args.report).write_text(json.dumps({
            "passed": report.passed, "total": report.total,
            "by_dimension": report.by_dimension(), "latency_ms": report.latency(),
            "cases": [{"case_id": r.case_id, "passed": r.passed, "duration_ms": r.duration_ms,
                       "failures": [vars(c) for c in r.failures()]} for r in results],
        }, indent=2))
    return 0 if report.passed == report.total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
