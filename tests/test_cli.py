"""CLI end to end on the fixtures: ingest, extract, review, sync, show, audit, eval."""
from __future__ import annotations

from pathlib import Path

import pytest

from meeting_crm.__main__ import main
from meeting_crm.review import ReviewQueue
from tests.conftest import REPO


def test_cli_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = ["--config", str(REPO / "config.yaml"), "--workdir", str(tmp_path / "state")]
    files = [str(p) for p in sorted((REPO / "fixtures" / "transcripts").glob("*.txt"))]

    assert main(base + ["ingest", *files]) == 0
    assert main(base + ["extract", "-v"]) == 0
    out = capsys.readouterr().out
    assert "REVIEW" in out and "cite:" in out

    assert main(base + ["sync"]) == 0
    out = capsys.readouterr().out
    assert "held for review (destructive change): 1" in out

    queue = ReviewQueue(tmp_path / "state" / "review_queue.jsonl")
    pending = {e.fact.kind: e.fact_id for e in queue.pending()}
    assert set(pending) == {"budget", "action_item", "timeline"}

    assert main(base + ["review", "approve", pending["timeline"][:8], "--by", "tester"]) == 0
    assert main(base + ["review", "reject", pending["budget"][:8], "--by", "tester"]) == 0
    assert main(base + ["review", "approve", "nope", "--by", "tester"]) == 1
    assert main(base + ["sync"]) == 0
    assert "applied 1 facts" in capsys.readouterr().out  # only the approved timeline
    assert main(base + ["sync"]) == 0
    assert "applied 0 facts (0 CRM writes)" in capsys.readouterr().out  # idempotent re-run

    assert main(base + ["show"]) == 0
    out = capsys.readouterr().out
    assert "close_target 2027-01" in out
    assert "amount       110000" in out
    assert "Elena Ruiz" in out and "elena.ruiz@acmelogistics.com" in out

    assert main(base + ["audit", "-v"]) == 0
    out = capsys.readouterr().out
    assert "rejected" in out and "cite: gong-" in out

    assert main(base + ["review", "list", "--all"]) == 0
    assert main(base + ["eval", "--fixtures", str(REPO / "fixtures")]) == 0
    assert "OVERALL" in capsys.readouterr().out


def test_cli_reports_config_errors(tmp_path: Path) -> None:
    assert main(["--config", str(tmp_path / "missing.yaml"), "show"]) == 2


def test_cli_extract_without_ingest(tmp_path: Path) -> None:
    assert main(["--config", str(REPO / "config.yaml"), "--workdir", str(tmp_path), "extract"]) == 1
