"""Pipeline state on disk: ingested transcripts and extracted facts per call.

Layout under the workdir (default ``.meeting-crm/``):

    transcripts/<transcript_id>.json   canonical utterances, one file per call
    facts/<transcript_id>.jsonl        extracted facts with their gate decision
    review_queue.jsonl                 human-in-the-loop queue (see review.py)
    crm.db                             SQLite reference CRM (see crm/sqlite.py)

Re-ingesting or re-extracting a call overwrites its files, so both are idempotent.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meeting_crm.models import Fact, Transcript


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class GatedFact:
    fact: Fact
    gate: str  # "auto" or "review"
    threshold: float

    def to_dict(self) -> dict[str, Any]:
        return {"gate": self.gate, "threshold": self.threshold, "fact": self.fact.to_dict()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GatedFact:
        return cls(Fact.from_dict(data["fact"]), str(data["gate"]), float(data["threshold"]))


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def queue_path(self) -> Path:
        return self.root / "review_queue.jsonl"

    def save_transcript(self, transcript: Transcript) -> Path:
        path = self.root / "transcripts" / f"{transcript.transcript_id}.json"
        atomic_write(path, json.dumps(transcript.to_dict(), indent=2) + "\n")
        return path

    def load_transcript(self, transcript_id: str) -> Transcript:
        path = self.root / "transcripts" / f"{transcript_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"transcript {transcript_id!r} has not been ingested")
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return Transcript.from_dict(data)

    def transcripts(self) -> list[Transcript]:
        """All ingested transcripts, oldest call first, so sync replays history in order."""
        folder = self.root / "transcripts"
        if not folder.exists():
            return []
        loaded = [self.load_transcript(p.stem) for p in folder.glob("*.json")]
        return sorted(loaded, key=lambda t: (t.call_date, t.transcript_id))

    def save_facts(self, transcript_id: str, facts: list[GatedFact]) -> Path:
        path = self.root / "facts" / f"{transcript_id}.jsonl"
        atomic_write(path, "".join(json.dumps(g.to_dict()) + "\n" for g in facts))
        return path

    def load_facts(self, transcript_id: str) -> list[GatedFact]:
        path = self.root / "facts" / f"{transcript_id}.jsonl"
        if not path.exists():
            return []
        return [
            GatedFact.from_dict(json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
