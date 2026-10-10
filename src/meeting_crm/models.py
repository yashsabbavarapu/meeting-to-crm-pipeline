"""Core data types: transcripts, utterances, citations, and extracted facts."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

# Every fact kind the extractor can emit. The CRM mapping for each lives in config.
FACT_KINDS: tuple[str, ...] = (
    "action_item",
    "contact",
    "budget",
    "timeline",
    "competitor",
    "risk",
    "next_step",
    "stage",
)


@dataclass(frozen=True)
class Utterance:
    timestamp: str  # "HH:MM:SS" offset into the call
    speaker: str
    text: str
    line_no: int


@dataclass(frozen=True)
class Transcript:
    transcript_id: str
    title: str
    call_date: date
    account: str
    deal: str
    internal_speakers: frozenset[str]
    utterances: tuple[Utterance, ...]
    source: str

    @property
    def speakers(self) -> list[str]:
        seen: list[str] = []
        for u in self.utterances:
            if u.speaker not in seen:
                seen.append(u.speaker)
        return seen

    def is_internal(self, speaker: str) -> bool:
        return speaker in self.internal_speakers

    def to_dict(self) -> dict[str, Any]:
        return {
            "transcript_id": self.transcript_id,
            "title": self.title,
            "call_date": self.call_date.isoformat(),
            "account": self.account,
            "deal": self.deal,
            "internal_speakers": sorted(self.internal_speakers),
            "source": self.source,
            "utterances": [
                {"timestamp": u.timestamp, "speaker": u.speaker, "text": u.text, "line_no": u.line_no}
                for u in self.utterances
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transcript:
        return cls(
            transcript_id=str(data["transcript_id"]),
            title=str(data["title"]),
            call_date=date.fromisoformat(str(data["call_date"])),
            account=str(data["account"]),
            deal=str(data["deal"]),
            internal_speakers=frozenset(str(s) for s in data["internal_speakers"]),
            source=str(data["source"]),
            utterances=tuple(
                Utterance(str(u["timestamp"]), str(u["speaker"]), str(u["text"]), int(u["line_no"]))
                for u in data["utterances"]
            ),
        )


@dataclass(frozen=True)
class Citation:
    """Points an extracted fact back to the exact transcript moment it came from."""

    timestamp: str
    speaker: str
    quote: str

    def render(self) -> str:
        return f'[{self.timestamp}] {self.speaker}: "{self.quote}"'


@dataclass
class Fact:
    fact_id: str
    transcript_id: str
    kind: str
    fields: dict[str, str]
    confidence: float
    citations: list[Citation]
    rule: str
    reasons: list[str] = field(default_factory=list)

    @property
    def timestamp(self) -> str:
        return self.citations[0].timestamp

    @property
    def quote(self) -> str:
        return self.citations[0].quote

    def summary(self) -> str:
        parts = [f"{k}={v}" for k, v in self.fields.items() if v]
        return f"{self.kind}: " + ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "transcript_id": self.transcript_id,
            "kind": self.kind,
            "fields": dict(self.fields),
            "confidence": self.confidence,
            "citations": [
                {"timestamp": c.timestamp, "speaker": c.speaker, "quote": c.quote}
                for c in self.citations
            ],
            "rule": self.rule,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fact:
        return cls(
            fact_id=str(data["fact_id"]),
            transcript_id=str(data["transcript_id"]),
            kind=str(data["kind"]),
            fields={str(k): str(v) for k, v in data["fields"].items()},
            confidence=float(data["confidence"]),
            citations=[
                Citation(str(c["timestamp"]), str(c["speaker"]), str(c["quote"]))
                for c in data["citations"]
            ],
            rule=str(data["rule"]),
            reasons=[str(r) for r in data.get("reasons", [])],
        )
