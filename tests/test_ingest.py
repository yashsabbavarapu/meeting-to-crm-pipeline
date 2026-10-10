"""Transcript parsing: headers, continuation lines, messy input."""
from __future__ import annotations

from datetime import date

import pytest

from meeting_crm.ingest import IngestError, parse_transcript
from meeting_crm.dates import find_date, find_period


def test_parses_headers_utterances_and_continuations() -> None:
    text = """# call_id: abc-1
# date: 2026-09-08
# account: Acme
# internal: Sam Ortiz
[00:00:04] Sam Ortiz: Hello there.
[00:00:09] Dana Whitfield: First line
second line of the same turn.
garbage [line
[1:02:03] Dana Whitfield: Late in the call.
"""
    result = parse_transcript(text, "t.txt", internal_speakers=frozenset({"Priya Nair"}))
    t = result.transcript
    assert t.transcript_id == "abc-1"
    assert t.call_date == date(2026, 9, 8)
    assert t.deal == "Acme - New Business"
    assert t.internal_speakers == {"Sam Ortiz", "Priya Nair"}
    assert [u.timestamp for u in t.utterances] == ["00:00:04", "00:00:09", "01:02:03"]
    assert "second line of the same turn" in t.utterances[1].text
    assert "garbage [line" in t.utterances[1].text  # continuation, not dropped
    assert t.speakers == ["Sam Ortiz", "Dana Whitfield"]
    assert not result.warnings


def test_bad_lines_warn_instead_of_crashing() -> None:
    text = "# date: 2026-09-08\n# account: Acme\n[00:99:00] Bob Smith: bad\n[00:00:01] Bob Smith: ok\n[oops\n"
    result = parse_transcript(text, "t.txt")
    assert len(result.transcript.utterances) == 1
    assert len(result.warnings) == 2


def test_missing_call_id_uses_stable_content_hash() -> None:
    text = "# date: 2026-09-08\n# account: Acme\n[00:00:01] Bob Smith: hi\n"
    a = parse_transcript(text, "a.txt").transcript.transcript_id
    b = parse_transcript(text, "b.txt").transcript.transcript_id
    assert a == b and a.startswith("t-")


@pytest.mark.parametrize(
    "text, message",
    [
        ("# account: Acme\n[00:00:01] Bob Smith: hi\n", "date"),
        ("# date: 2026-09-08\n[00:00:01] Bob Smith: hi\n", "account"),
        ("# date: 2026-09-08\n# account: Acme\n", "no utterances"),
        ("# date: Sept 8\n# account: Acme\n[00:00:01] Bob Smith: hi\n", "YYYY-MM-DD"),
    ],
)
def test_ingest_errors(text: str, message: str) -> None:
    with pytest.raises(IngestError, match=message):
        parse_transcript(text, "t.txt")


def test_date_resolution_relative_to_call() -> None:
    ref = date(2026, 9, 8)  # a Tuesday
    assert find_date("by Friday", ref) == (date(2026, 9, 11), "Friday")
    assert find_date("send it today", ref) == (ref, "today")
    assert find_date("this week", ref) == (date(2026, 9, 11), "this week")
    assert find_date("on October 15th", ref) == (date(2026, 10, 15), "October 15th")
    assert find_date("on Tuesday", ref) == (date(2026, 9, 15), "Tuesday")  # never the call day
    assert find_date("no dates here", ref) is None
    assert find_period("go live by Q1", ref) == ("2027-Q1", "Q1")
    assert find_period("live by Q4", ref) == ("2026-Q4", "Q4")
    assert find_period("live by January", ref) == ("2027-01", "January")
