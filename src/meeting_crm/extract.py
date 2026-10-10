"""Deterministic, explainable fact extraction. Regex plus rules, no ML, no LLM.

Every fact carries a confidence score built from named, additive rule
components (listed in ``Fact.reasons``) and citations back to the transcript.
A reviewer can always answer "why did the system think this?".
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator
from datetime import date

from meeting_crm.config import Config
from meeting_crm.dates import find_date, find_period
from meeting_crm.models import Citation, Fact, Transcript, Utterance

NAME = r"[A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?\s[A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?"
ROLE_WORDS = re.compile(
    r"\b(vp|vice president|director|head|manager|lead|chief|officer|cfo|cto|ceo|coo|cio|ciso|"
    r"engineer|architect|analyst|counsel|admin|administrator|procurement|buyer|president|owner)\b",
    re.I,
)
HEDGES = re.compile(
    r"\b(maybe|might|probably|possibly|roughly|ballpark|approximately|"
    r"i think|hopefully|try to|a bit more|or so)\b",
    re.I,
)
NEGATION = re.compile(r"\b(no|not|don't|doesn't|won't|never|without)\b[^.]{0,25}$", re.I)
ACTION_VERBS = {
    "send", "share", "set", "schedule", "loop", "get", "put", "draft", "book", "follow",
    "run", "start", "review", "prepare", "introduce", "check", "confirm", "update",
    "create", "reach", "email", "circle", "provide", "forward", "kick", "pull", "write",
}

RISK_LEXICON: dict[str, tuple[str, ...]] = {
    "security_review": ("security review", "security questionnaire", "infosec", "pen test",
                        "penetration test", "security assessment"),
    "legal_review": ("legal review", "legal team", "redlines", "data processing agreement",
                     "msa review"),
    "budget_constraint": ("budget freeze", "over budget", "too expensive", "cut costs",
                          "cost cutting", "no budget", "can't afford", "cannot afford"),
    "champion_risk": ("leaving the company", "new role", "reorg", "reorganization", "stepping down"),
    "timing_risk": ("not a priority", "on hold", "push this to next", "next fiscal year", "delayed"),
    "integration_risk": ("do not integrate", "does not integrate", "doesn't integrate",
                         "don't integrate", "integration concern", "not compatible"),
}

GO_LIVE = re.compile(r"\b(go live|live|launch|roll ?out|in place|deployed|up and running)\b", re.I)
BUDGET_WORDS = re.compile(r"\b(budget|budgeted|approved|allocated|funding|spend)\b", re.I)
MONEY = re.compile(
    r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(k|m|thousand|million)?\b"
    r"|\b(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|million)?\s+dollars\b",
    re.I,
)
NEXT_STEP = re.compile(
    r"\b(let's|let us)\s+(meet|schedule|reconvene|regroup|book|set up|plan|sync)\b"
    r"|\bnext steps?\s+(is|are|will be)\b"
    r"|\b(follow-up|kickoff)\s+(call|meeting)\b",
    re.I,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'])")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def _clamp(value: float) -> float:
    return round(max(0.05, min(0.99, value)), 2)


def make_fact_id(transcript_id: str, kind: str, key: str) -> str:
    digest = hashlib.sha256(f"{transcript_id}|{kind}|{key.lower()}".encode("utf-8"))
    return digest.hexdigest()[:12]


class _Builder:
    """Accumulates confidence components so every score explains itself."""

    def __init__(self, base: float, reason: str) -> None:
        self.score = base
        self.reasons = [f"{reason} ({base:+.2f})"]

    def add(self, delta: float, reason: str) -> None:
        self.score += delta
        self.reasons.append(f"{reason} ({delta:+.2f})")

    def hedge_check(self, sentence: str) -> None:
        hedge = HEDGES.search(sentence)
        if hedge:
            self.add(-0.20, f"hedged language: '{hedge.group(0)}'")


class Extractor:
    def __init__(self, config: Config) -> None:
        self.config = config

    # Public API ---------------------------------------------------------

    def extract(self, transcript: Transcript) -> list[Fact]:
        rules: list[Callable[[Transcript], Iterator[Fact]]] = [
            self._action_items,
            self._contacts,
            self._budget,
            self._timeline,
            self._competitors,
            self._risks,
            self._next_steps,
            self._stage,
        ]
        facts: list[Fact] = []
        for rule in rules:
            facts.extend(rule(transcript))
        return _merge(facts)

    # Helpers ------------------------------------------------------------

    def _fact(
        self,
        t: Transcript,
        kind: str,
        key: str,
        fields: dict[str, str],
        u: Utterance,
        quote: str,
        rule: str,
        builder: _Builder,
    ) -> Fact:
        return Fact(
            fact_id=make_fact_id(t.transcript_id, kind, key),
            transcript_id=t.transcript_id,
            kind=kind,
            fields=fields,
            confidence=_clamp(builder.score),
            citations=[Citation(u.timestamp, u.speaker, quote)],
            rule=rule,
            reasons=builder.reasons,
        )

    def _external(self, t: Transcript) -> Iterator[Utterance]:
        for u in t.utterances:
            if not t.is_internal(u.speaker):
                yield u

    def _resolve_person(self, t: Transcript, first_name: str) -> str | None:
        for speaker in t.speakers:
            if speaker.split()[0] == first_name:
                return speaker
        return None

    def _due(self, sentence: str, ref: date, builder: _Builder) -> str:
        found = find_date(sentence, ref)
        if not found:
            return ""
        resolved, phrase = found
        builder.add(0.05, f"due date stated: '{phrase}' -> {resolved.isoformat()}")
        return resolved.isoformat()

    # Rules --------------------------------------------------------------

    def _action_items(self, t: Transcript) -> Iterator[Fact]:
        first_person = re.compile(r"\bI(?:'ll| will)\s+(?:also\s+)?(?P<task>[a-z][^.?!]*)")
        request = re.compile(r"^(?P<name>[A-Z][a-z]+),\s+(?:can|could|would) you\s+(?P<task>[^?]+)")
        collective = re.compile(r"\b[Ww]e(?:'ll| will)\s+(?P<task>[a-z][^.?!]*)")

        for u in t.utterances:
            for s in sentences(u.text):
                owner: str | None = None
                task = ""
                if m := first_person.search(s):
                    owner, task = u.speaker, m.group("task")
                    verb = task.split()[0]
                    if verb in ACTION_VERBS:
                        b = _Builder(0.80, "first-person commitment with action verb")
                    else:
                        b = _Builder(0.60, f"first-person statement, '{verb}' is not an action verb")
                    rule = "first_person_commitment"
                elif m := request.search(s):
                    owner = self._resolve_person(t, m.group("name"))
                    if owner is None:
                        continue
                    task = m.group("task")
                    b = _Builder(0.70, "direct request to a named participant")
                    rule = "named_request"
                elif m := collective.search(s):
                    owner, task = u.speaker, m.group("task")
                    b = _Builder(0.55, "collective 'we' commitment, owner is ambiguous")
                    rule = "collective_commitment"
                else:
                    continue
                if s.endswith("?") and rule == "first_person_commitment":
                    b.add(-0.25, "phrased as a question")
                b.hedge_check(s)
                due = self._due(s, t.call_date, b)
                task = task.strip().rstrip(".")
                yield self._fact(
                    t, "action_item", f"{owner}|{u.timestamp}",
                    {"owner": owner, "task": task, "due": due}, u, s, rule, b,
                )

    def _contacts(self, t: Transcript) -> Iterator[Fact]:
        role = r"(?P<role>[A-Za-z][A-Za-z &/-]{2,40}?)"
        role_end = r"(?=\s+(?:at|here|for|from|on)\b|[,.;]|$)"
        patterns: list[tuple[re.Pattern[str], float, str, bool]] = [
            (re.compile(rf"\b(?:I'm|I am|my name is|this is)\s+(?P<name>{NAME}),\s+(?:the\s+|our\s+)?{role}{role_end}"),
             0.85, "self-introduction with role", True),
            (re.compile(rf"\b(?:I'm|I am|my name is)\s+(?P<name>{NAME})\b"),
             0.65, "self-introduction without role", True),
            (re.compile(rf"\b(?P<name>{NAME}),\s+(?:is\s+)?our\s+{role}{role_end}"),
             0.80, "named with role", False),
            (re.compile(rf"\b(?P<name>{NAME})\s+is\s+our\s+{role}{role_end}"),
             0.80, "named with role", False),
            (re.compile(rf"\b(?:loop in|introduce you to|cc|copy in|bring in)\s+(?P<name>{NAME})\b"),
             0.60, "named as a stakeholder to involve", False),
        ]
        excluded = set(t.internal_speakers) | {t.account}
        found: list[Fact] = []
        for u in t.utterances:
            for s in sentences(u.text):
                matched_names: set[str] = set()
                for pattern, base, label, self_intro in patterns:
                    for m in pattern.finditer(s):
                        name = m.group("name")
                        if name in excluded or name in matched_names:
                            continue
                        if self_intro and t.is_internal(u.speaker):
                            continue
                        fields = {"name": name, "role": "", "email": ""}
                        b = _Builder(base, label)
                        if "role" in m.groupdict() and m.group("role"):
                            role_text = m.group("role").strip()
                            if not ROLE_WORDS.search(role_text):
                                continue
                            fields["role"] = role_text
                        matched_names.add(name)
                        found.append(self._fact(
                            t, "contact", name, fields, u, s, "contact_" + label.replace(" ", "_"), b,
                        ))
        # Emails: literal or spoken ("marcus dot lee at acme dot com"), attached by name match.
        for u in t.utterances:
            for s in sentences(u.text):
                for email in _emails(s):
                    local = re.split(r"[._-]", email.split("@")[0].lower())
                    owner = None
                    for f in found:
                        parts = f.fields["name"].lower().split()
                        if parts[0] in local or parts[-1] in local:
                            owner = f.fields["name"]
                            break
                    if owner is None and not t.is_internal(u.speaker) and re.search(r"\b(my|me)\b", s, re.I):
                        owner = u.speaker
                    if owner is None or owner in excluded:
                        continue
                    b = _Builder(0.75, "email address stated on the call")
                    if any(p in local for p in owner.lower().split()):
                        b.add(0.10, "email local part matches the contact's name")
                    found.append(self._fact(
                        t, "contact", owner, {"name": owner, "role": "", "email": email},
                        u, s, "contact_email", b,
                    ))
        yield from found

    def _budget(self, t: Transcript) -> Iterator[Fact]:
        for u in self._external(t):
            for s in sentences(u.text):
                money = MONEY.search(s)
                keyword = BUDGET_WORDS.search(s)
                if not money or not keyword:
                    continue
                amount = _money_value(money)
                if amount is None:
                    continue
                b = _Builder(0.85, f"customer states an amount next to '{keyword.group(0)}'")
                if re.search(r"\bapproved budget\b|\bbudget is\b", s, re.I):
                    b.add(0.05, "explicit budget statement")
                b.hedge_check(s)
                if re.search(r"\b(about|around)\s+\$", s, re.I):
                    b.add(-0.15, "approximate amount")
                yield self._fact(
                    t, "budget", str(amount), {"amount": str(amount)}, u, s, "budget_amount", b,
                )

    def _timeline(self, t: Transcript) -> Iterator[Fact]:
        for u in self._external(t):
            for s in sentences(u.text):
                live = GO_LIVE.search(s)
                if not live:
                    continue
                period = find_period(s, t.call_date)
                if not period:
                    continue
                target, phrase = period
                b = _Builder(0.85, f"go-live language '{live.group(0)}' with target '{phrase}'")
                b.hedge_check(s)
                yield self._fact(
                    t, "timeline", target, {"target": target}, u, s, "go_live_target", b,
                )

    def _competitors(self, t: Transcript) -> Iterator[Fact]:
        known = {c.lower(): c for c in self.config.competitors}
        evaluating = re.compile(
            r"\b(?:evaluating|looking at|talking to|considering|trialing|piloting)\s+([A-Z][A-Za-z0-9]+)"
        )
        people = {w for s in t.speakers for w in s.split()}
        for u in t.utterances:
            internal = t.is_internal(u.speaker)
            for s in sentences(u.text):
                seen: set[str] = set()
                for lower, name in known.items():
                    if re.search(rf"\b{re.escape(lower)}\b", s, re.I):
                        seen.add(name)
                        if internal:
                            b = _Builder(0.65, "configured competitor named by our own rep")
                        else:
                            b = _Builder(0.90, "configured competitor named by the customer")
                        yield self._fact(
                            t, "competitor", name, {"name": name}, u, s, "competitor_known", b,
                        )
                for m in evaluating.finditer(s):
                    name = m.group(1)
                    if name in seen or name.lower() in known or name in people or internal:
                        continue
                    b = _Builder(0.55, "unrecognized vendor after evaluation language")
                    yield self._fact(
                        t, "competitor", name, {"name": name}, u, s, "competitor_pattern", b,
                    )

    def _risks(self, t: Transcript) -> Iterator[Fact]:
        for u in self._external(t):
            for s in sentences(u.text):
                lower = s.lower()
                for category, phrases in RISK_LEXICON.items():
                    for phrase in phrases:
                        idx = lower.find(phrase)
                        if idx < 0:
                            continue
                        b = _Builder(0.80, f"customer raised '{phrase}'")
                        if NEGATION.search(s[:idx]):
                            b.add(-0.40, "negated nearby")
                        if re.search(r"\b(risk|concern|worried|flag)\b", lower):
                            b.add(0.05, "explicitly framed as a concern")
                        yield self._fact(
                            t, "risk", category, {"category": category}, u, s, "risk_lexicon", b,
                        )
                        break

    def _next_steps(self, t: Transcript) -> Iterator[Fact]:
        for u in t.utterances:
            for s in sentences(u.text):
                m = NEXT_STEP.search(s)
                if not m:
                    continue
                found = find_date(s, t.call_date)
                if found:
                    resolved, phrase = found
                    b = _Builder(0.85, f"next-step language with date '{phrase}'")
                    when = resolved.isoformat()
                else:
                    b = _Builder(0.55, "next-step language without a date")
                    when = ""
                b.hedge_check(s)
                yield self._fact(
                    t, "next_step", when or u.timestamp,
                    {"description": s.rstrip("."), "date": when}, u, s, "next_step", b,
                )

    def _stage(self, t: Transcript) -> Iterator[Fact]:
        best: tuple[int, str, Utterance, str] | None = None
        hits = 0
        for u in t.utterances:
            lower = u.text.lower()
            for i, stage in enumerate(self.config.stages):
                for signal in stage.signals:
                    if signal in lower:
                        hits += 1
                        if best is None or i > best[0]:
                            sentence = next((s for s in sentences(u.text) if signal in s.lower()), u.text)
                            best = (i, signal, u, sentence)
        if best is None:
            return
        i, signal, u, sentence = best
        stage_name = self.config.stages[i].name
        b = _Builder(0.80, f"stage signal '{signal}'")
        if hits > 1:
            b.add(min(0.10, 0.03 * (hits - 1)), f"{hits} stage signals in the call")
        yield self._fact(t, "stage", stage_name, {"stage": stage_name}, u, sentence, "stage_signal", b)


def _money_value(m: re.Match[str]) -> int | None:
    digits = m.group(1) or m.group(3)
    unit = (m.group(2) or m.group(4) or "").lower()
    try:
        value = float(digits.replace(",", ""))
    except ValueError:
        return None
    if unit in ("k", "thousand"):
        value *= 1_000
    elif unit in ("m", "million"):
        value *= 1_000_000
    return int(round(value))


_SPOKEN_EMAIL = re.compile(
    r"\b([a-z0-9]+(?:\s+(?:dot|underscore|dash)\s+[a-z0-9]+)*)\s+at\s+"
    r"([a-z0-9-]+(?:\s+dot\s+[a-z0-9-]+)+)\b",
    re.I,
)
_LITERAL_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")


def _emails(sentence: str) -> list[str]:
    out = [m.group(0).lower().rstrip(".") for m in _LITERAL_EMAIL.finditer(sentence)]
    for m in _SPOKEN_EMAIL.finditer(sentence):
        local = re.sub(r"\s+dot\s+", ".", m.group(1), flags=re.I)
        local = re.sub(r"\s+underscore\s+", "_", local, flags=re.I)
        local = re.sub(r"\s+dash\s+", "-", local, flags=re.I)
        local = local.split()[-1]  # "send it to marcus.lee" -> "marcus.lee"
        domain = re.sub(r"\s+dot\s+", ".", m.group(2), flags=re.I)
        out.append(f"{local}@{domain}".lower())
    return out


def _merge(facts: list[Fact]) -> list[Fact]:
    """Collapse repeated mentions of the same fact, keeping every citation.

    Repetition is evidence: each extra independent mention adds a small,
    capped corroboration bonus. Field values fill in from later mentions only
    where the first one was blank (a contact's email said later in the call).
    """
    merged: dict[str, Fact] = {}
    order: list[str] = []
    for f in facts:
        existing = merged.get(f.fact_id)
        if existing is None:
            merged[f.fact_id] = f
            order.append(f.fact_id)
            continue
        if f.confidence > existing.confidence:
            primary, other = f, existing
        else:
            primary, other = existing, f
        fields = dict(primary.fields)
        for k, v in other.fields.items():
            if v and not fields.get(k):
                fields[k] = v
        citations = list(primary.citations)
        for c in other.citations:
            if c not in citations:
                citations.append(c)
        bonus = 0.05 if len(citations) > len(primary.citations) else 0.0
        reasons = list(primary.reasons)
        if bonus:
            reasons.append(f"corroborated by {len(citations)} mentions (+{bonus:.2f})")
        merged[f.fact_id] = Fact(
            fact_id=primary.fact_id,
            transcript_id=primary.transcript_id,
            kind=primary.kind,
            fields=fields,
            confidence=_clamp(primary.confidence + bonus),
            citations=sorted(citations, key=lambda c: c.timestamp),
            rule=primary.rule,
            reasons=reasons,
        )
    return [merged[i] for i in order]
