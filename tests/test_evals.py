"""Eval scoring math and the fixture baseline."""
from __future__ import annotations

from pathlib import Path

import pytest

from meeting_crm.config import Config
from meeting_crm.evals import EvalReport, KindScore, render_report, run_eval, score_transcript
from meeting_crm.models import Citation, Fact
from tests.conftest import REPO


def _fact(kind: str, ts: str = "00:00:01", **fields: str) -> Fact:
    return Fact("id-" + kind + ts + str(fields), "t", kind, dict(fields), 0.9,
                [Citation(ts, "Dana Whitfield", "q")], "r", [])


def test_precision_recall_math() -> None:
    s = KindScore(tp=3, fp=1, fn=2)
    assert s.precision == 0.75
    assert s.recall == 0.6
    assert s.f1 == pytest.approx(2 * 0.75 * 0.6 / 1.35)
    assert KindScore().precision is None and KindScore().f1 is None


def test_score_transcript_counts_matches_misses_and_attributes() -> None:
    facts = [
        _fact("budget", amount="120000"),
        _fact("competitor", name="ROUTEWISE"),  # matching is case-insensitive
        _fact("competitor", name="Optimo"),  # not labeled: false positive
        _fact("action_item", "00:03:20", owner="Sam Ortiz", task="x", due="2026-09-11"),
        _fact("contact", name="Marcus Lee", role="IT Security Lead", email=""),
    ]
    labels = [
        {"kind": "budget", "amount": "120000"},
        {"kind": "competitor", "name": "Routewise"},
        {"kind": "risk", "category": "legal_review"},  # missed
        {"kind": "action_item", "owner": "Sam Ortiz", "timestamp": "00:03:20", "due": "2026-09-12"},
        {"kind": "contact", "name": "Marcus Lee", "role": "IT Security Lead",
         "email": "marcus.lee@acme.com"},
    ]
    report = EvalReport()
    score_transcript("t", facts, labels, report)
    assert (report.kinds["competitor"].tp, report.kinds["competitor"].fp) == (1, 1)
    assert report.kinds["risk"].fn == 1
    assert report.kinds["budget"].precision == 1.0
    assert report.attributes == {"action_item.due": [0, 1], "contact.role": [1, 1],
                                 "contact.email": [0, 1]}
    o = report.overall
    assert (o.tp, o.fp, o.fn) == (4, 1, 1)


def test_fixture_baseline(config: Config) -> None:
    """Regression guard: rule changes must not silently degrade quality on the fixtures."""
    report = run_eval(REPO / "fixtures", config)
    assert report.transcripts == 3
    overall = report.overall
    assert overall.precision is not None and overall.precision >= 0.9
    assert overall.recall is not None and overall.recall >= 0.9
    text = render_report(report, verbose=True)
    assert "OVERALL" in text and "missed risk budget_constraint" in text


def test_min_confidence_trades_recall_for_precision(config: Config) -> None:
    everything = run_eval(REPO / "fixtures", config).overall
    confident = run_eval(REPO / "fixtures", config, min_confidence=0.8).overall
    assert confident.tp + confident.fp < everything.tp + everything.fp
    assert confident.recall is not None and everything.recall is not None
    assert confident.recall <= everything.recall


def test_missing_labels_is_an_error(tmp_path: Path, config: Config) -> None:
    (tmp_path / "transcripts").mkdir()
    (tmp_path / "transcripts" / "x.txt").write_text(
        "# date: 2026-09-08\n# account: A\n[00:00:01] Bob Smith: hi\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="missing labels"):
        run_eval(tmp_path, config)
