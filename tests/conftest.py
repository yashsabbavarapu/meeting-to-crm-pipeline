"""Shared fixtures: the repo config and a helper to build transcripts inline."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from meeting_crm.config import Config, load_config
from meeting_crm.ingest import parse_transcript
from meeting_crm.models import Transcript

REPO = Path(__file__).resolve().parent.parent

HEADER = """# call_id: {call_id}
# title: Test call
# date: {date}
# account: Acme Logistics
# deal: Acme Logistics - Route Optimization
# internal: Sam Ortiz
"""


@pytest.fixture
def config() -> Config:
    return load_config(REPO / "config.yaml")


MakeTranscript = Callable[..., Transcript]


@pytest.fixture
def make_transcript(config: Config) -> MakeTranscript:
    def make(body: str, call_id: str = "test-1", date: str = "2026-09-08") -> Transcript:
        text = HEADER.format(call_id=call_id, date=date) + body.strip() + "\n"
        return parse_transcript(text, f"{call_id}.txt", config.default_deal_name,
                                config.internal_speakers).transcript

    return make
