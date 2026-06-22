from pathlib import Path

import yaml

from stock_monitor.config import load_settings, validate_strategy_config


def _write_base_config(tmp_path: Path) -> Path:
    source = Path(__file__).parents[1] / "config" / "config.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["paths"]["sqlite_file"] = str(tmp_path / "monitor.db")
    raw["paths"]["log_file"] = str(tmp_path / "monitor.log")
    raw["hot_sector_monitor"]["cache"]["directory"] = str(tmp_path / "cache")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return config


def _valid_strategy(strategy_id: str) -> dict:
    return {
        "id": strategy_id,
        "display_name": f"测试-{strategy_id}",
        "scorer": "short_term_resonance",
        "schema_version": 1,
        "weights": {"relative_strength": 0.5, "fund_flow": 0.2, "risk_control": 0.3},
        "degraded_weights_no_fund_flow": {"relative_strength": 0.6, "risk_control": 0.4},
        "thresholds": {"rsi_hot": 70.0},
        "risk_penalties": {"lhb": 5.0},
    }


def test_hot_sector_config_loads_from_yaml(tmp_path: Path):
    config = _write_base_config(tmp_path)

    settings = load_settings(config)

    assert settings.hot_sector_monitor.enabled is True
    assert settings.hot_sector_monitor.sector_scope.types == ("industry", "concept")
    assert settings.hot_sector_monitor.sector_scope.top_n_display == 10
    assert settings.hot_sector_monitor.http.timeout_seconds == 10
    assert settings.hot_sector_monitor.http.retry_backoff_seconds == (1.0, 3.0, 8.0)
    assert settings.hot_sector_monitor.cache.directory == tmp_path / "cache"


def test_real_config_loads_strategies_from_directory():
    config = Path(__file__).parents[1] / "config" / "config.yaml"
    settings = load_settings(config)
    strategies = settings.hot_sector_monitor.strategy_configs
    assert "short_term_resonance" in strategies
    assert strategies["short_term_resonance"]["display_name"] == "短线热点共振"
    assert strategies["short_term_resonance"]["scorer"] == "short_term_resonance"


def test_strategy_directory_takes_priority_and_skips_invalid(tmp_path: Path):
    config = _write_base_config(tmp_path)
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "good.yaml").write_text(
        yaml.safe_dump(_valid_strategy("good"), allow_unicode=True), encoding="utf-8"
    )
    # 坏文件：id 与文件名不一致 + scorer 未注册，应被跳过且不影响其它策略。
    bad = _valid_strategy("mismatch")
    bad["scorer"] = "does_not_exist"
    (strategies_dir / "bad.yaml").write_text(
        yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8"
    )

    settings = load_settings(config)
    strategies = settings.hot_sector_monitor.strategy_configs
    assert "good" in strategies
    assert "bad" not in strategies


def test_validate_strategy_config_catches_errors():
    assert validate_strategy_config(_valid_strategy("ok")) == []

    bad_weight = _valid_strategy("x")
    bad_weight["weights"] = {"a": "not-a-number"}
    assert any("weights.a" in err for err in validate_strategy_config(bad_weight))

    bad_scorer = _valid_strategy("x")
    bad_scorer["scorer"] = "nope"
    assert any("scorer" in err for err in validate_strategy_config(bad_scorer))

    bad_degraded = _valid_strategy("x")
    bad_degraded["degraded_weights_no_fund_flow"] = {"unknown_factor": 1.0}
    assert any("degraded_weights_no_fund_flow" in err for err in validate_strategy_config(bad_degraded))

    mismatch = _valid_strategy("real-id")
    assert any("id" in err for err in validate_strategy_config(mismatch, expected_id="other-id"))
