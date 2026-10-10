"""Extraction rules: each rule fires on the right language and explains its score."""
from __future__ import annotations

from meeting_crm.config import Config
from meeting_crm.extract import Extractor
from meeting_crm.models import Fact
from tests.conftest import MakeTranscript


def _facts(config: Config, make: MakeTranscript, body: str, kind: str | None = None) -> list[Fact]:
    facts = Extractor(config).extract(make(body))
    return [f for f in facts if kind is None or f.kind == kind]


def test_first_person_commitment_with_due_date(config: Config, make_transcript: MakeTranscript) -> None:
    [f] = _facts(config, make_transcript,
                 "[00:03:20] Sam Ortiz: Perfect. I'll send over the SOC 2 report by Friday.", "action_item")
    assert f.fields == {"owner": "Sam Ortiz", "task": "send over the SOC 2 report by Friday",
                        "due": "2026-09-11"}
    assert f.confidence == 0.85
    assert f.citations[0].timestamp == "00:03:20"
    assert f.citations[0].quote == "I'll send over the SOC 2 report by Friday."
    assert any("due date stated" in r for r in f.reasons)


def test_named_request_resolves_owner_and_scores_lower(config: Config, make_transcript: MakeTranscript) -> None:
    body = """
[00:00:01] Dana Whitfield: Hi.
[00:00:05] Sam Ortiz: Dana, can you share the route exports?
"""
    [f] = _facts(config, make_transcript, body, "action_item")
    assert f.fields["owner"] == "Dana Whitfield"
    assert f.rule == "named_request"
    assert f.confidence < config.threshold_for("action_item")


def test_hedged_and_non_action_statements_score_low(config: Config, make_transcript: MakeTranscript) -> None:
    body = """
[00:00:01] Dana Whitfield: I'll probably send the numbers.
[00:00:05] Dana Whitfield: I will think about it.
[00:00:09] Dana Whitfield: We'll circle back.
"""
    facts = _facts(config, make_transcript, body, "action_item")
    assert len(facts) == 3
    assert all(f.confidence < config.threshold_for("action_item") for f in facts)


def test_contact_intro_role_and_spoken_email(config: Config, make_transcript: MakeTranscript) -> None:
    body = """
[00:00:12] Marcus Lee: I'm Marcus Lee, IT Security Lead at Acme.
[00:00:48] Marcus Lee: Send it to marcus dot lee at acmelogistics dot com please.
[00:01:00] Sam Ortiz: I'm Sam Ortiz, the Account Executive here.
"""
    [f] = _facts(config, make_transcript, body, "contact")
    assert f.fields == {"name": "Marcus Lee", "role": "IT Security Lead",
                        "email": "marcus.lee@acmelogistics.com"}
    assert [c.timestamp for c in f.citations] == ["00:00:12", "00:00:48"]  # both moments cited
    assert any("corroborated" in r for r in f.reasons)


def test_contact_mention_and_literal_email_attached_by_name(config: Config, make_transcript: MakeTranscript) -> None:
    body = """
[00:01:40] Dana Whitfield: I will loop in Elena Ruiz, our Procurement Manager, since she owns contracts.
[00:02:02] Dana Whitfield: Her email is elena.ruiz@acmelogistics.com.
"""
    [f] = _facts(config, make_transcript, body, "contact")
    assert f.fields["role"] == "Procurement Manager"
    assert f.fields["email"] == "elena.ruiz@acmelogistics.com"


def test_budget_only_from_customer_and_hedging_lowers_confidence(config: Config, make_transcript: MakeTranscript) -> None:
    body = """
[00:00:41] Sam Ortiz: Our proposal fits your budget at $138,000 per year.
[00:01:02] Elena Ruiz: Our approved budget is $110,000 for year one.
[00:01:44] Dana Whitfield: We have roughly $120k budgeted, maybe a bit more.
"""
    facts = {f.fields["amount"]: f for f in _facts(config, make_transcript, body, "budget")}
    assert set(facts) == {"110000", "120000"}  # the rep's quote is not customer budget
    assert facts["110000"].confidence >= config.threshold_for("budget")
    assert facts["120000"].confidence < config.threshold_for("budget")
    assert any("hedged" in r for r in facts["120000"].reasons)


def test_timeline_needs_go_live_language(config: Config, make_transcript: MakeTranscript) -> None:
    body = """
[00:02:16] Dana Whitfield: We need to go live by Q1, before the spring spike.
[00:02:50] Dana Whitfield: The pilot needs to wrap by end of October so we can decide.
"""
    facts = _facts(config, make_transcript, body, "timeline")
    assert [f.fields["target"] for f in facts] == ["2027-Q1"]


def test_competitors_known_list_and_rep_mentions(config: Config, make_transcript: MakeTranscript) -> None:
    body = """
[00:00:20] Elena Ruiz: We are also evaluating Routewise and talking to Acmeroute.
[00:00:30] Sam Ortiz: How does Optimo compare for you?
"""
    facts = {f.fields["name"]: f for f in _facts(config, make_transcript, body, "competitor")}
    assert facts["Routewise"].confidence == 0.90
    assert facts["Optimo"].confidence < config.default_threshold  # rep asking is weak evidence
    assert facts["Acmeroute"].rule == "competitor_pattern"
    assert facts["Acmeroute"].confidence < config.default_threshold


def test_risks_respect_negation_and_ignore_rep(config: Config, make_transcript: MakeTranscript) -> None:
    body = """
[00:00:10] Sam Ortiz: Our legal team is fast.
[00:00:20] Dana Whitfield: One risk: anything new has to clear a security review.
[00:00:30] Elena Ruiz: There is no legal review needed for renewals.
"""
    facts = {f.fields["category"]: f for f in _facts(config, make_transcript, body, "risk")}
    assert facts["security_review"].confidence == 0.85
    assert facts["legal_review"].citations[0].speaker == "Elena Ruiz"
    assert facts["legal_review"].confidence < config.threshold_for("risk")


def test_next_step_and_most_advanced_stage(config: Config, make_transcript: MakeTranscript) -> None:
    body = """
[00:00:05] Sam Ortiz: Tell me about how routing works today.
[00:01:00] Dana Whitfield: The pilot results were strong. Can we talk about the discount?
[00:03:35] Sam Ortiz: Let's meet on October 16 to finalize terms.
"""
    facts = _facts(config, make_transcript, body)
    [step] = [f for f in facts if f.kind == "next_step"]
    assert step.fields["date"] == "2026-10-16"
    [stage] = [f for f in facts if f.kind == "stage"]
    assert stage.fields["stage"] == "negotiation"


def test_fact_ids_are_deterministic(config: Config, make_transcript: MakeTranscript) -> None:
    body = "[00:03:20] Sam Ortiz: I'll send the deck by Friday."
    a = _facts(config, make_transcript, body)
    b = _facts(config, make_transcript, body)
    assert [f.fact_id for f in a] == [f.fact_id for f in b]
    other = Extractor(config).extract(make_transcript(body, call_id="test-2"))
    assert {f.fact_id for f in other}.isdisjoint({f.fact_id for f in a})


def test_every_fact_has_citation_and_reasons(config: Config) -> None:
    from meeting_crm.ingest import parse_file
    from tests.conftest import REPO

    for path in sorted((REPO / "fixtures" / "transcripts").glob("*.txt")):
        t = parse_file(path, config.default_deal_name, config.internal_speakers).transcript
        utterance_text = {u.timestamp: u.text for u in t.utterances}
        for f in Extractor(config).extract(t):
            assert f.reasons and 0 < f.confidence < 1
            for c in f.citations:
                assert c.quote in utterance_text[c.timestamp]  # verbatim, at that timestamp
