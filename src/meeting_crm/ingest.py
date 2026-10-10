"""Parse timestamped speaker-line transcripts into structured utterances.

Format (what Gong, Chorus, and Fireflies exports reduce to after cleanup):

    # call_id: gong-58211
    # title: Discovery call
    # date: 2026-09-08
    # account: Acme Logistics
    # deal: Acme Logistics - Route Optimization   (optional)
    # internal: Sam Ortiz, Priya Nair             (optional, seller-side speakers)
    [00:00:04] Sam Ortiz: Thanks for making the time today.
    continuation lines without a timestamp are appended to the previous utterance

Malformed lines produce warnings, never a crash: real exports are messy.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from meeting_crm.models import Transcript, Utterance

_HEADER = re.compile(r"^#\s*([a-z_]+)\s*:\s*(.*)$")
_LINE = re.compile(r"^\[(\d{1,2}):(\d{2}):(\d{2})\]\s*([^:\]]{1,60}?)\s*:\s*(.+)$")


class IngestError(ValueError):
    pass


@dataclass
class IngestResult:
    transcript: Transcript
    warnings: list[str] = field(default_factory=list)


def parse_transcript(
    text: str,
    source: str,
    default_deal_name: str = "{account} - New Business",
    internal_speakers: frozenset[str] = frozenset(),
) -> IngestResult:
    headers: dict[str, str] = {}
    utterances: list[Utterance] = []
    warnings: list[str] = []

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        header = _HEADER.match(line)
        if header and not utterances:
            headers[header.group(1)] = header.group(2).strip()
            continue
        match = _LINE.match(line)
        if match:
            hh, mm, ss, speaker, body = match.groups()
            if int(mm) > 59 or int(ss) > 59:
                warnings.append(f"line {line_no}: invalid timestamp, skipped")
                continue
            utterances.append(
                Utterance(f"{int(hh):02d}:{mm}:{ss}", speaker.strip(), body.strip(), line_no)
            )
        elif utterances and not line.startswith(("[", "#")):
            prev = utterances[-1]
            utterances[-1] = Utterance(prev.timestamp, prev.speaker, f"{prev.text} {line}", prev.line_no)
        else:
            warnings.append(f"line {line_no}: unrecognized line, skipped: {line[:60]!r}")

    if not utterances:
        raise IngestError(f"{source}: no utterances found")
    for required in ("date", "account"):
        if not headers.get(required):
            raise IngestError(f"{source}: missing required header '# {required}:'")
    try:
        call_date = date.fromisoformat(headers["date"])
    except ValueError:
        raise IngestError(f"{source}: '# date:' must be YYYY-MM-DD") from None

    timestamps = [u.timestamp for u in utterances]
    if timestamps != sorted(timestamps):
        warnings.append("timestamps are not in order; kept file order")

    account = headers["account"]
    transcript_id = headers.get("call_id") or (
        "t-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", transcript_id):
        raise IngestError(f"{source}: call_id may only contain letters, digits, '.', '_', '-'")
    internal = internal_speakers | frozenset(
        s.strip() for s in headers.get("internal", "").split(",") if s.strip()
    )

    transcript = Transcript(
        transcript_id=transcript_id,
        title=headers.get("title", Path(source).stem),
        call_date=call_date,
        account=account,
        deal=headers.get("deal") or default_deal_name.format(account=account),
        internal_speakers=internal,
        utterances=tuple(utterances),
        source=source,
    )
    return IngestResult(transcript, warnings)


def parse_file(
    path: Path,
    default_deal_name: str = "{account} - New Business",
    internal_speakers: frozenset[str] = frozenset(),
) -> IngestResult:
    return parse_transcript(
        path.read_text(encoding="utf-8"), str(path), default_deal_name, internal_speakers
    )
