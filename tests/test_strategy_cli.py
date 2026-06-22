from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from stock_monitor.cli import _strategy_export, _strategy_import, _strategy_list
from stock_monitor.config import load_settings


def _make_config(tmp_path: Path) -> Path:
    """在 tmp 下搭出 config/config.yaml + config/strategies/，返回 config.yaml 路径。"""
    source_root = Path(__file__).parents[1]
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    raw = yaml.safe_load((source_root / "config" / "config.yaml").read_text(encoding="utf-8"))
    raw["paths"]["sqlite_file"] = str(tmp_path / "monitor.db")
    raw["paths"]["log_file"] = str(tmp_path / "monitor.log")
    raw["hot_sector_monitor"]["cache"]["directory"] = str(tmp_path / "cache")
    config = config_dir / "config.yaml"
    config.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    shutil.copytree(source_root / "config" / "strategies", config_dir / "strategies")
    return config


def test_strategy_list_runs(tmp_path):
    config = _make_config(tmp_path)
    assert _strategy_list(str(config)) == 0


def test_strategy_export_to_file(tmp_path):
    config = _make_config(tmp_path)
    out = tmp_path / "exported.yaml"
    assert _strategy_export(str(config), "short_term_resonance", str(out)) == 0
    payload = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert payload["id"] == "short_term_resonance"
    assert "weights" in payload


def test_strategy_export_unknown_returns_error(tmp_path):
    config = _make_config(tmp_path)
    assert _strategy_export(str(config), "no_such", None) == 1


def test_strategy_import_roundtrip(tmp_path):
    config = _make_config(tmp_path)
    exported = tmp_path / "exported.yaml"
    _strategy_export(str(config), "short_term_resonance", str(exported))

    # 用新 id 导入，应落盘到 config/strategies/ 并能被重新加载。
    assert _strategy_import(str(config), str(exported), "my_variant") == 0
    dest = config.parent / "strategies" / "my_variant.yaml"
    assert dest.is_file()

    settings = load_settings(config)
    assert "my_variant" in settings.hot_sector_monitor.strategy_configs


def test_strategy_import_rejects_invalid(tmp_path):
    config = _make_config(tmp_path)
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        yaml.safe_dump({"id": "bad", "scorer": "does_not_exist", "weights": {"a": 1.0}}, allow_unicode=True),
        encoding="utf-8",
    )
    assert _strategy_import(str(config), str(bad), None) == 1
    assert not (config.parent / "strategies" / "bad.yaml").exists()
