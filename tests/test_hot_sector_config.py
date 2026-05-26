from pathlib import Path

import yaml

from stock_monitor.config import load_settings


def test_hot_sector_config_loads_from_yaml(tmp_path: Path):
    source = Path(__file__).parents[1] / "config" / "config.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["paths"]["sqlite_file"] = str(tmp_path / "monitor.db")
    raw["paths"]["log_file"] = str(tmp_path / "monitor.log")
    raw["hot_sector_monitor"]["cache"]["directory"] = str(tmp_path / "cache")
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")

    settings = load_settings(config)

    assert settings.hot_sector_monitor.enabled is True
    assert settings.hot_sector_monitor.sector_scope.types == ("industry",)
    assert settings.hot_sector_monitor.sector_scope.top_n_display == 10
    assert settings.hot_sector_monitor.http.timeout_seconds == 10
    assert settings.hot_sector_monitor.http.retry_backoff_seconds == (1.0, 3.0, 8.0)
    assert settings.hot_sector_monitor.cache.directory == tmp_path / "cache"
