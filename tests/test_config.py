"""YAML subset parser and config validation."""
from __future__ import annotations

from pathlib import Path

import pytest

from meeting_crm import yamlish
from meeting_crm.config import ConfigError, load_config, parse_config


def test_yaml_subset_covers_config_shapes() -> None:
    doc = yamlish.loads(
        """
# comment
customer: "Acme: Inc"   # trailing comment
thresholds:
  default: 0.75
  budget: 0.8
flags: [a, "b, c", 'd']
stages:
  - name: discovery
    signals: [x, y]
  - name: demo
plain:
- one
- 2
empty:
enabled: true
"""
    )
    assert doc == {
        "customer": "Acme: Inc",
        "thresholds": {"default": 0.75, "budget": 0.8},
        "flags": ["a", "b, c", "d"],
        "stages": [{"name": "discovery", "signals": ["x", "y"]}, {"name": "demo"}],
        "plain": ["one", 2],
        "empty": None,
        "enabled": True,
    }


@pytest.mark.parametrize(
    "text",
    ["a: 1\n   b: 2\n", "a: 1\na: 2\n", "a: {x: 1}\n", "a: &anchor 1\n", "a:\n\t- x\n"],
)
def test_yaml_rejects_unsupported_or_invalid(text: str) -> None:
    with pytest.raises(yamlish.YamlError):
        yamlish.loads(text)


def test_repo_config_loads() -> None:
    config = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    assert config.customer == "Northwind Software"
    assert config.threshold_for("budget") == 0.80
    assert config.threshold_for("action_item") == config.default_threshold
    assert config.stage_index("negotiation") > config.stage_index("pilot")
    assert "closed_won" in config.protected_stages
    assert config.field_map["budget"] == "amount"


def _base() -> dict[str, object]:
    return {"customer": "X", "stages": [{"name": "discovery"}]}


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"thresholds": {"default": 1.5}}, "between 0 and 1"),
        ({"thresholds": {"bogus": 0.5}}, "unknown fact kind"),
        ({"field_map": {"bogus": "amount"}}, "unknown fact kind"),
        ({"protected_stages": ["closed_won"]}, "not a defined stage"),
        ({"stages": []}, "non-empty"),
        ({"sync": {"default_deal_name": "no placeholder"}}, "{account}"),
    ],
)
def test_config_validation_errors(patch: dict[str, object], message: str) -> None:
    data = _base()
    data.update(patch)
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")
