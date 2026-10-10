"""Eval harness: score extraction against hand-labeled fixtures, per fact kind.

A predicted fact matches a label when their match keys are equal (see MATCH_KEYS).
Unmatched predictions are false positives, unmatched labels are false negatives.
For matched pairs, secondary attributes (a contact's email, an action item's
due date) are scored separately as attribute accuracy.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from meeting_crm.config import Config
from meeting_crm.extract import Extractor
from meeting_crm.ingest import parse_file
from meeting_crm.models import FACT_KINDS, Fact

# Fields that identify "the same fact". Timestamp is used for action items because
# two commitments by the same person in one call are different facts.
MATCH_KEYS: dict[str, tuple[str, ...]] = {
    "action_item": ("owner", "timestamp"),
    "contact": ("name",),
    "budget": ("amount",),
    "timeline": ("target",),
    "competitor": ("name",),
    "risk": ("category",),
    "next_step": ("date",),
    "stage": ("stage",),
}
ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "action_item": ("due",),
    "contact": ("role", "email"),
}


@dataclass
class KindScore:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or p + r == 0:
            return None
        return 2 * p * r / (p + r)


@dataclass
class EvalReport:
    kinds: dict[str, KindScore] = field(default_factory=lambda: {k: KindScore() for k in FACT_KINDS})
    attributes: dict[str, list[int]] = field(default_factory=dict)  # "contact.email" -> [correct, total]
    errors: list[str] = field(default_factory=list)  # human-readable FP/FN list
    transcripts: int = 0

    @property
    def overall(self) -> KindScore:
        total = KindScore()
        for s in self.kinds.values():
            total.tp += s.tp
            total.fp += s.fp
            total.fn += s.fn
        return total


def _norm(value: Any) -> str:
    return " ".join(str(value).lower().split())


def _fact_key(fact: Fact) -> tuple[str, ...]:
    values = {**fact.fields, "timestamp": fact.timestamp}
    return tuple(_norm(values.get(k, "")) for k in MATCH_KEYS[fact.kind])


def _label_key(label: dict[str, Any]) -> tuple[str, ...]:
    return tuple(_norm(label.get(k, "")) for k in MATCH_KEYS[str(label["kind"])])


def score_transcript(
    name: str, facts: list[Fact], labels: list[dict[str, Any]], report: EvalReport
) -> None:
    report.transcripts += 1
    for kind in FACT_KINDS:
        predicted = {_fact_key(f): f for f in facts if f.kind == kind}
        expected = {_label_key(lab): lab for lab in labels if lab["kind"] == kind}
        score = report.kinds[kind]
        for key, fact in predicted.items():
            if key in expected:
                score.tp += 1
                for attr in ATTRIBUTES.get(kind, ()):
                    want = _norm(expected[key].get(attr, ""))
                    got = _norm(fact.fields.get(attr, ""))
                    if not want and not got:
                        continue
                    bucket = report.attributes.setdefault(f"{kind}.{attr}", [0, 0])
                    bucket[1] += 1
                    if want == got:
                        bucket[0] += 1
                    else:
                        report.errors.append(
                            f"{name}: {kind} {'/'.join(key)} {attr}: expected {want or '(none)'!r}, got {got or '(none)'!r}"
                        )
            else:
                score.fp += 1
                report.errors.append(f"{name}: false positive {fact.summary()} @ {fact.timestamp}")
        for key in expected:
            if key not in predicted:
                score.fn += 1
                report.errors.append(f"{name}: missed {kind} {'/'.join(key)}")


def run_eval(fixtures: Path, config: Config, min_confidence: float = 0.0) -> EvalReport:
    report = EvalReport()
    extractor = Extractor(config)
    transcripts = sorted((fixtures / "transcripts").glob("*.txt"))
    if not transcripts:
        raise FileNotFoundError(f"no transcripts in {fixtures / 'transcripts'}")
    for path in transcripts:
        label_path = fixtures / "labels" / f"{path.stem}.json"
        if not label_path.exists():
            raise FileNotFoundError(f"missing labels for {path.name}: {label_path}")
        labels: list[dict[str, Any]] = json.loads(label_path.read_text(encoding="utf-8"))["facts"]
        for lab in labels:
            if lab.get("kind") not in MATCH_KEYS:
                raise ValueError(f"{label_path}: unknown kind {lab.get('kind')!r}")
        transcript = parse_file(path, config.default_deal_name, config.internal_speakers).transcript
        facts = [f for f in extractor.extract(transcript) if f.confidence >= min_confidence]
        score_transcript(path.stem, facts, labels, report)
    return report


def _fmt(value: float | None) -> str:
    return "  n/a" if value is None else f"{value:5.2f}"


def render_report(report: EvalReport, verbose: bool = False) -> str:
    lines = [
        f"Extraction eval over {report.transcripts} labeled transcripts",
        "",
        f"{'field':<14}{'tp':>4}{'fp':>4}{'fn':>4}  {'prec':>5}  {'recall':>6}  {'f1':>5}",
    ]
    rows = list(report.kinds.items()) + [("OVERALL", report.overall)]
    for kind, s in rows:
        if kind == "OVERALL":
            lines.append("-" * 46)
        lines.append(
            f"{kind:<14}{s.tp:>4}{s.fp:>4}{s.fn:>4}  {_fmt(s.precision)}  {_fmt(s.recall):>6}  {_fmt(s.f1)}"
        )
    if report.attributes:
        lines += ["", "Attribute accuracy on matched facts:"]
        for attr, (correct, total) in sorted(report.attributes.items()):
            lines.append(f"  {attr:<20}{correct}/{total}")
    if verbose and report.errors:
        lines += ["", "Errors:"] + [f"  {e}" for e in report.errors]
    return "\n".join(lines) + "\n"
