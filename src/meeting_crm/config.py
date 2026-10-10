"""Per-customer configuration: thresholds, stages, field mappings, competitors.

Nothing customer-specific is hardcoded in the pipeline. A new deployment is a
new YAML file, not a code change.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meeting_crm import yamlish
from meeting_crm.models import FACT_KINDS


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class StageDef:
    name: str
    signals: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    customer: str
    internal_speakers: frozenset[str]
    default_threshold: float
    thresholds: dict[str, float]
    stages: tuple[StageDef, ...]
    protected_stages: frozenset[str]
    field_map: dict[str, str]
    competitors: tuple[str, ...]
    review_overwrites: bool
    freely_updated_fields: frozenset[str]
    default_deal_name: str

    def threshold_for(self, kind: str) -> float:
        return self.thresholds.get(kind, self.default_threshold)

    def stage_index(self, stage: str | None) -> int:
        for i, s in enumerate(self.stages):
            if s.name == stage:
                return i
        return -1

    @property
    def initial_stage(self) -> str:
        return self.stages[0].name


def _str_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{key} must be a list of strings")
    return list(value)


def _threshold(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be a number between 0 and 1")
    if not 0.0 <= float(value) <= 1.0:
        raise ConfigError(f"{key} must be between 0 and 1, got {value}")
    return float(value)


def parse_config(data: Any) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")

    customer = data.get("customer")
    if not isinstance(customer, str) or not customer:
        raise ConfigError("customer is required")

    raw_thresholds = data.get("thresholds") or {}
    if not isinstance(raw_thresholds, dict):
        raise ConfigError("thresholds must be a mapping")
    default_threshold = _threshold(raw_thresholds.get("default", 0.75), "thresholds.default")
    thresholds: dict[str, float] = {}
    for kind, value in raw_thresholds.items():
        if kind == "default":
            continue
        if kind not in FACT_KINDS:
            raise ConfigError(f"thresholds.{kind}: unknown fact kind")
        thresholds[kind] = _threshold(value, f"thresholds.{kind}")

    raw_stages = data.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise ConfigError("stages must be a non-empty list")
    stages: list[StageDef] = []
    for i, entry in enumerate(raw_stages):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise ConfigError(f"stages[{i}] needs a name")
        signals = _str_list(entry.get("signals"), f"stages[{i}].signals")
        stages.append(StageDef(entry["name"], tuple(s.lower() for s in signals)))
    stage_names = {s.name for s in stages}

    protected = _str_list(data.get("protected_stages"), "protected_stages")
    for name in protected:
        if name not in stage_names:
            raise ConfigError(f"protected_stages: {name!r} is not a defined stage")

    raw_map = data.get("field_map") or {}
    if not isinstance(raw_map, dict):
        raise ConfigError("field_map must be a mapping")
    field_map: dict[str, str] = {}
    for kind, target in raw_map.items():
        if kind not in FACT_KINDS:
            raise ConfigError(f"field_map.{kind}: unknown fact kind")
        if not isinstance(target, str):
            raise ConfigError(f"field_map.{kind} must be a field name")
        field_map[kind] = target

    sync = data.get("sync") or {}
    if not isinstance(sync, dict):
        raise ConfigError("sync must be a mapping")
    review_overwrites = sync.get("review_overwrites", True)
    if not isinstance(review_overwrites, bool):
        raise ConfigError("sync.review_overwrites must be true or false")
    deal_name = sync.get("default_deal_name", "{account} - New Business")
    if not isinstance(deal_name, str) or "{account}" not in deal_name:
        raise ConfigError("sync.default_deal_name must contain {account}")

    return Config(
        customer=customer,
        internal_speakers=frozenset(_str_list(data.get("internal_speakers"), "internal_speakers")),
        default_threshold=default_threshold,
        thresholds=thresholds,
        stages=tuple(stages),
        protected_stages=frozenset(protected),
        field_map=field_map,
        competitors=tuple(_str_list(data.get("competitors"), "competitors")),
        review_overwrites=review_overwrites,
        freely_updated_fields=frozenset(
            _str_list(sync.get("freely_updated_fields"), "sync.freely_updated_fields")
        ),
        default_deal_name=deal_name,
    )


def load_config(path: Path) -> Config:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    try:
        return parse_config(yamlish.loads(text))
    except yamlish.YamlError as exc:
        raise ConfigError(f"{path}: {exc}") from None
