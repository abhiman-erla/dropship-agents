from pathlib import Path

import pytest

from dropship.config import ConfigError, load_config

KEEPA = 'data_source = "keepa"\n'


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_defaults_match_spec(tmp_path):
    s = load_config(_write(tmp_path, KEEPA + "categories = [1350380031]\n"))
    assert s.keepa_domain == 1
    assert s.min_margin_pct == 0.25
    assert s.llm_top_n == 20
    assert s.daily_spend_cap == 1000.0
    assert s.max_risk_score == 60


def test_empty_categories_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "categories = []\n"))


def test_override(tmp_path):
    s = load_config(_write(tmp_path, KEEPA + "categories = [1]\nmin_rating = 4.5\n"))
    assert s.min_rating == 4.5


def test_unknown_key_rejected(tmp_path):
    with pytest.raises(ConfigError, match="min_ratng"):
        load_config(_write(tmp_path, KEEPA + "categories = [1]\nmin_ratng = 4.5\n"))


def test_zero_max_offer_count_rejected(tmp_path):
    with pytest.raises(ConfigError, match="max_offer_count"):
        load_config(_write(tmp_path, KEEPA + "categories = [1]\nmax_offer_count = 0\n"))


@pytest.mark.parametrize("line", [
    "fee_pct = 1.0", "est_cost_ratio = -0.1", "min_margin_pct = 1.5",
    "max_risk_score = 101", "min_rating = 5.5", "price_min = -1",
    "max_asins_per_category = 0", "llm_top_n = 0",
])
def test_out_of_range_values_rejected(tmp_path, line):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, f"{KEEPA}categories = [1]\n{line}\n"))


def test_malformed_toml_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, KEEPA + "categories = [1\n"))


def test_max_asins_per_category_default(tmp_path):
    assert load_config(_write(tmp_path, KEEPA + "categories = [1]\n")).max_asins_per_category == 50


@pytest.mark.parametrize("text, bad", [
    ('categories = [1]\n', "1"),  # default data_source is canopy
    ('categories = ["bestsellers_kitchen", 12]\n', "12"),
    ('data_source = "keepa"\ncategories = ["bestsellers_kitchen"]\n', "bestsellers_kitchen"),
    ('data_source = "keepa"\ncategories = [1, "2"]\n', "2"),
])
def test_category_type_must_match_data_source(tmp_path, text, bad):
    with pytest.raises(ConfigError, match="categories") as ei:
        load_config(_write(tmp_path, text))
    assert bad in str(ei.value)


def test_category_types_matching_data_source_accepted(tmp_path):
    s = load_config(_write(tmp_path, 'categories = ["bestsellers_kitchen", "b"]\n'))
    assert s.categories == ["bestsellers_kitchen", "b"]
    s = load_config(_write(tmp_path, 'data_source = "keepa"\ncategories = [1, 2]\n'))
    assert s.categories == [1, 2]


def test_blocked_brands_loaded_and_stripped(tmp_path):
    from dropship.config import load_config
    p = tmp_path / "c.toml"
    p.write_text('categories = ["bestsellers_kitchen"]\nblocked_brands = [" Owala ", "", "YETI"]\n',
                 encoding="utf-8")
    assert load_config(p).blocked_brands == ["Owala", "YETI"]
