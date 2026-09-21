"""
The calibration harness, and the guarantee that it changes nothing.

WHAT THIS GUARDS. The harness exists so the quality thresholds can one
day be set from real data instead of chosen. Its single most important
property is that running it has NO EFFECT on the service — a tuning tool
that can move a production threshold is a tuning tool that will, and
nobody will know which run did it.

The rest is shape: the distribution it reports must be the one a person
calibrating a threshold actually needs, and the OCR outcome must be
carried through, because a defect label alone says an image is blurred,
not whether the blur mattered.
"""

from __future__ import annotations

import json

import pytest

from tools import quality_calibration as harness


def sample(label="good", extracted=None, name="a.jpg"):
    from pathlib import Path

    return harness.Sample(path=Path(name), label=label, extracted=extracted)


def measurement(label="good", extracted=None, findings=(), **metrics):
    return harness.Measurement(
        sample=sample(label, extracted), metrics=metrics,
        findings=tuple(findings), worst="INFO", analysed=True)


# ==========================================================================
# IT CHANGES NOTHING
# ==========================================================================


def test_running_the_harness_does_not_move_a_threshold():
    """
    THE ONE THAT MATTERS. Measure a corpus, then check every production
    threshold is exactly what it was.
    """
    from app.agents.document_agent import quality

    before = dict(quality._thresholds())

    harness.report(harness.measure([]))
    harness.report([measurement(focus=10.0), measurement(focus=5000.0)])

    assert quality._thresholds() == before


def test_the_harness_never_writes_configuration():
    import ast
    import inspect

    source = ast.parse(inspect.getsource(harness))
    called = {node.attr for node in ast.walk(source)
              if isinstance(node, ast.Attribute)}

    # `write_text` is used for the --json report only, into a path the
    # operator names. Nothing may reach the settings layer.
    for forbidden in ("set_threshold", "save", "update_config", "dump_config"):
        assert forbidden not in called


def test_the_harness_measures_with_the_production_analyser():
    """
    A harness that reimplements a metric calibrates something the
    service does not compute.
    """
    import inspect

    assert "quality.analyse" in inspect.getsource(harness.measure)


# ==========================================================================
# THE DISTRIBUTION
# ==========================================================================


def test_the_distribution_reports_every_requested_quantile():
    result = harness.distribution([float(n) for n in range(1, 101)])

    assert set(result) == {"count", "min", "p10", "p25", "median", "p75",
                           "p90", "max"}
    assert result["count"] == 100
    assert result["min"] == 1.0
    assert result["max"] == 100.0
    assert result["median"] == 50.5


def test_the_quantiles_are_ordered():
    result = harness.distribution([3.0, 1.0, 2.0, 9.0, 5.0, 4.0, 8.0])

    assert (result["min"] <= result["p10"] <= result["p25"]
            <= result["median"] <= result["p75"] <= result["p90"]
            <= result["max"])


def test_an_empty_class_reports_a_count_and_nothing_else():
    """
    No invented quantiles for a class nobody collected. A zero would
    read as a measurement.
    """
    assert harness.distribution([]) == {"count": 0}


def test_a_single_sample_is_reported_honestly():
    result = harness.distribution([42.0])

    assert result["count"] == 1
    assert result["min"] == result["max"] == result["median"] == 42.0


# ==========================================================================
# THE REPORT
# ==========================================================================


def test_every_metric_is_reported_per_label():
    summary = harness.report([
        measurement("good", focus=900.0, contrast=120.0),
        measurement("blur", focus=40.0, contrast=118.0),
    ])

    assert set(summary["by_label"]) == {"good", "blur"}
    for label in ("good", "blur"):
        assert set(summary["by_label"][label]["metrics"]) == set(harness.METRICS)


def test_classes_are_kept_apart():
    summary = harness.report([
        measurement("good", focus=900.0), measurement("good", focus=1100.0),
        measurement("blur", focus=40.0),
    ])

    assert summary["by_label"]["good"]["metrics"]["focus"]["count"] == 2
    assert summary["by_label"]["blur"]["metrics"]["focus"]["max"] == 40.0


def test_a_missing_defect_class_is_named():
    """
    A corpus of good controls alone cannot locate a single threshold,
    and the report has to say so rather than look complete.
    """
    summary = harness.report([measurement("good", focus=900.0)])

    assert "blur" in summary["totals"]["missing_labels"]
    assert "glare" in summary["totals"]["missing_labels"]


def test_the_extraction_outcome_is_carried_through():
    summary = harness.report([
        measurement("blur", extracted=True, focus=300.0),
        measurement("blur", extracted=False, focus=40.0),
        measurement("blur", extracted=False, focus=20.0),
    ])
    rate = summary["by_label"]["blur"]["extraction"]

    assert rate["labelled"] == 3
    assert rate["extracted"] == 1
    assert rate["failed"] == 2
    assert rate["rate"] == pytest.approx(0.3333, abs=1e-4)


def test_an_unlabelled_corpus_reports_no_rate():
    """
    Not zero. Zero would read as "nothing extracted", which is a
    measurement; this is the absence of one.
    """
    summary = harness.report([measurement("blur", focus=40.0)])
    rate = summary["by_label"]["blur"]["extraction"]

    assert rate["rate"] is None
    assert rate["unlabelled"] == 1


def test_the_current_thresholds_verdict_is_reported_with_its_severity():
    """
    An INFO finding never reaches a caller, so counting it beside a
    SEVERE would read as a false positive the service does not make.
    """
    summary = harness.report([
        measurement("good", findings=[("LOW_IMAGE_RESOLUTION", "INFO")]),
        measurement("good", findings=[("SEVERE_GLARE", "SEVERE")]),
    ])
    counts = summary["by_label"]["good"]["current_findings"]

    assert counts == {"LOW_IMAGE_RESOLUTION[INFO]": 1, "SEVERE_GLARE[SEVERE]": 1}


def test_an_unanalysable_image_is_counted_not_hidden():
    bad = harness.Measurement(sample=sample(), metrics={}, findings=(),
                              worst="INFO", analysed=False)

    summary = harness.report([bad])

    assert summary["by_label"]["good"]["unanalysable"] == 1


def test_the_report_is_json_serialisable():
    """It is written to a file for later comparison between runs."""
    summary = harness.report([measurement("good", focus=900.0)])

    assert json.loads(json.dumps(summary))["totals"]["samples"] == 1


def test_the_rendered_report_names_the_classes():
    text = harness.render(harness.report([
        measurement("good", focus=900.0), measurement("blur", focus=40.0)]))

    assert "[good]" in text
    assert "[blur]" in text
    assert "median" in text


def test_the_rendered_report_warns_when_nothing_is_labelled():
    text = harness.render(harness.report([measurement("good", focus=900.0)]))

    assert "NO EXTRACTION LABELS" in text


# ==========================================================================
# READING A CORPUS
# ==========================================================================


def test_a_directory_per_label_is_read(tmp_path):
    for label in ("good", "blur"):
        (tmp_path / label).mkdir()
        (tmp_path / label / "one.jpg").write_bytes(b"")

    samples = harness.read_corpus(tmp_path)

    assert {s.label for s in samples} == {"good", "blur"}
    assert all(s.extracted is None for s in samples)


def test_a_manifest_carries_the_extraction_outcome(tmp_path):
    (tmp_path / "manifest.csv").write_text(
        "path,label,extracted\na.jpg,blur,false\nb.jpg,good,true\n",
        encoding="utf-8")

    samples = {s.label: s for s in harness.read_corpus(tmp_path)}

    assert samples["blur"].extracted is False
    assert samples["good"].extracted is True


def test_a_manifest_row_with_no_outcome_stays_unlabelled(tmp_path):
    (tmp_path / "manifest.csv").write_text(
        "path,label,extracted\na.jpg,blur,\n", encoding="utf-8")

    assert harness.read_corpus(tmp_path)[0].extracted is None


def test_a_manifest_wins_over_the_directory_layout(tmp_path):
    """The manifest is the richer source; it carries the OCR outcome."""
    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "one.jpg").write_bytes(b"")
    (tmp_path / "manifest.csv").write_text(
        "path,label,extracted\ngood/one.jpg,good,true\n", encoding="utf-8")

    samples = harness.read_corpus(tmp_path)

    assert len(samples) == 1
    assert samples[0].extracted is True


@pytest.mark.parametrize("text, expected", [
    ("true", True), ("YES", True), ("1", True), ("pass", True),
    ("false", False), ("no", False), ("0", False), ("fail", False),
    ("", None), ("maybe", None),
])
def test_the_outcome_column_is_read_forgivingly(text, expected):
    assert harness._as_bool(text) is expected


def test_non_images_are_ignored(tmp_path):
    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "one.jpg").write_bytes(b"")
    (tmp_path / "good" / "notes.txt").write_text("x", encoding="utf-8")

    assert len(harness.read_corpus(tmp_path)) == 1


# ==========================================================================
# END TO END, ON THE REAL SAMPLES
# ==========================================================================


def test_the_harness_runs_over_real_images(tmp_path):
    from pathlib import Path

    source = Path("samples/real_batch/pan_bw2.jpg")
    if not source.exists():
        pytest.skip("sample documents not available")

    (tmp_path / "good").mkdir()
    (tmp_path / "good" / "pan.jpg").write_bytes(source.read_bytes())

    summary = harness.report(harness.measure(harness.read_corpus(tmp_path)))
    focus = summary["by_label"]["good"]["metrics"]["focus"]

    assert focus["count"] == 1
    assert focus["min"] > 0
