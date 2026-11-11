from pathlib import Path

import pytest

from dropship.config import ConfigError, load_config


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_defaults_match_spec(tmp_path):
    s = load_config(_write(tmp_path, "categories = [1350380031]\n"))
    assert s.keepa_domain == 10
    assert s.min_margin_pct == 0.25
    assert s.llm_top_n == 20
    assert s.daily_spend_cap == 15000.0
    assert s.max_risk_score == 60


def test_empty_categories_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "categories = []\n"))


def test_override(tmp_path):
    s = load_config(_write(tmp_path, "categories = [1]\nmin_rating = 4.5\n"))
    assert s.min_rating == 4.5
