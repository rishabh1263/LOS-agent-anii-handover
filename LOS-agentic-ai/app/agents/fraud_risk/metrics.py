"""
In-process metrics for the Fraud & Risk Agent.

Deliberately dependency-free counters exposed in Prometheus text format, so
the agent is observable immediately. Replace with prometheus_client when the
platform team standardises on it -- the metric names below are already
Prometheus-compatible.
"""

from __future__ import annotations

import threading
from collections import defaultdict

_LOCK = threading.Lock()

_assessments_total: dict[str, int] = defaultdict(int)   # keyed by outcome
_category_total: dict[str, int] = defaultdict(int)      # keyed by risk_category
_rule_triggered_total: dict[str, int] = defaultdict(int)  # keyed by rule
_summary_source_total: dict[str, int] = defaultdict(int)
_duration_ms_sum: float = 0.0
_duration_count: int = 0


def record_assessment(
    *,
    outcome: str,
    category: str,
    rules: list[str],
    summary_source: str,
    duration_ms: float,
) -> None:
    global _duration_ms_sum, _duration_count
    with _LOCK:
        _assessments_total[outcome] += 1
        _category_total[category] += 1
        _summary_source_total[summary_source] += 1
        for rule in rules:
            _rule_triggered_total[rule] += 1
        _duration_ms_sum += duration_ms
        _duration_count += 1


def snapshot() -> dict:
    with _LOCK:
        return {
            "assessments_by_outcome": dict(_assessments_total),
            "assessments_by_category": dict(_category_total),
            "rules_triggered": dict(_rule_triggered_total),
            "summary_source": dict(_summary_source_total),
            "assessments_total": _duration_count,
            "avg_duration_ms": round(_duration_ms_sum / _duration_count, 2)
            if _duration_count
            else 0.0,
        }


def render_prometheus() -> str:
    with _LOCK:
        lines: list[str] = []
        lines.append("# HELP risk_assessments_total Assessments by final outcome.")
        lines.append("# TYPE risk_assessments_total counter")
        for outcome, count in _assessments_total.items():
            lines.append(f'risk_assessments_total{{outcome="{outcome}"}} {count}')

        lines.append("# HELP risk_category_total Assessments by risk category.")
        lines.append("# TYPE risk_category_total counter")
        for category, count in _category_total.items():
            lines.append(f'risk_category_total{{category="{category}"}} {count}')

        lines.append("# HELP risk_rule_triggered_total Times each rule fired.")
        lines.append("# TYPE risk_rule_triggered_total counter")
        for rule, count in _rule_triggered_total.items():
            lines.append(f'risk_rule_triggered_total{{rule="{rule}"}} {count}')

        lines.append("# HELP risk_summary_source_total Summary provenance.")
        lines.append("# TYPE risk_summary_source_total counter")
        for source, count in _summary_source_total.items():
            lines.append(f'risk_summary_source_total{{source="{source}"}} {count}')

        avg = _duration_ms_sum / _duration_count if _duration_count else 0.0
        lines.append("# HELP risk_assessment_duration_ms_avg Mean assessment duration.")
        lines.append("# TYPE risk_assessment_duration_ms_avg gauge")
        lines.append(f"risk_assessment_duration_ms_avg {avg:.2f}")

        return "\n".join(lines) + "\n"


def reset() -> None:
    """Test helper."""
    global _duration_ms_sum, _duration_count
    with _LOCK:
        _assessments_total.clear()
        _category_total.clear()
        _rule_triggered_total.clear()
        _summary_source_total.clear()
        _duration_ms_sum = 0.0
        _duration_count = 0


__all__ = ["record_assessment", "snapshot", "render_prometheus", "reset"]


# ---------------------------------------------------------------------------
# ORCHESTRATION METRICS
# ---------------------------------------------------------------------------

_orch_total: dict[str, int] = defaultdict(int)       # "agent|status"
_orch_errors: dict[str, int] = defaultdict(int)      # error_type
_orch_fallbacks: dict[str, int] = defaultdict(int)   # agent
_orch_attempts: int = 0
_orch_duration_sum: float = 0.0
_orch_count: int = 0


def record_orchestration(
    *,
    agent_id: str,
    status: str,
    error_type: str | None,
    used_fallback: bool,
    attempts: int,
    duration_ms: float,
) -> None:
    global _orch_attempts, _orch_duration_sum, _orch_count
    with _LOCK:
        _orch_total[f"{agent_id}|{status}"] += 1
        if error_type:
            _orch_errors[error_type] += 1
        if used_fallback:
            _orch_fallbacks[agent_id] += 1
        _orch_attempts += attempts
        _orch_duration_sum += duration_ms
        _orch_count += 1


def orchestration_snapshot() -> dict:
    with _LOCK:
        return {
            "by_agent_status": dict(_orch_total),
            "errors": dict(_orch_errors),
            "fallbacks": dict(_orch_fallbacks),
            "total": _orch_count,
            "total_attempts": _orch_attempts,
            "avg_duration_ms": round(_orch_duration_sum / _orch_count, 2)
            if _orch_count
            else 0.0,
        }


def render_orchestration_prometheus() -> str:
    with _LOCK:
        lines = [
            "# HELP orchestration_total Agent executions by agent and status.",
            "# TYPE orchestration_total counter",
        ]
        for key, count in _orch_total.items():
            agent, _, status = key.partition("|")
            lines.append(
                f'orchestration_total{{agent="{agent}",status="{status}"}} {count}'
            )

        lines.append("# HELP orchestration_errors_total Failures by error type.")
        lines.append("# TYPE orchestration_errors_total counter")
        for error_type, count in _orch_errors.items():
            lines.append(f'orchestration_errors_total{{type="{error_type}"}} {count}')

        lines.append("# HELP orchestration_fallback_total Fallback activations.")
        lines.append("# TYPE orchestration_fallback_total counter")
        for agent, count in _orch_fallbacks.items():
            lines.append(f'orchestration_fallback_total{{agent="{agent}"}} {count}')

        avg = _orch_duration_sum / _orch_count if _orch_count else 0.0
        lines.append("# HELP orchestration_duration_ms_avg Mean orchestration time.")
        lines.append("# TYPE orchestration_duration_ms_avg gauge")
        lines.append(f"orchestration_duration_ms_avg {avg:.2f}")
        return "\n".join(lines) + "\n"