import pytest

from stock_monitor.config import KNOWN_SCORERS
from stock_monitor.services.hot_sector_candidates import score_short_term_resonance
from stock_monitor.services.strategy_registry import available_scorers, get_scorer


def test_get_scorer_returns_short_term_resonance():
    assert get_scorer("short_term_resonance") is score_short_term_resonance


def test_get_scorer_unknown_raises():
    with pytest.raises(ValueError) as exc:
        get_scorer("does_not_exist")
    assert "does_not_exist" in str(exc.value)


def test_registry_matches_config_known_scorers():
    # 两处必须一致：config 用 KNOWN_SCORERS 做加载期校验，registry 做运行期分发。
    assert available_scorers() == KNOWN_SCORERS
