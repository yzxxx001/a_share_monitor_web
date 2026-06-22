"""打分引擎（scorer）注册表。

策略配置文件里的 `scorer` 字段决定用哪套打分逻辑。当前所有策略共用
`short_term_resonance` 引擎——它是配置驱动的（权重/阈值/扣分全来自策略文件），
因此换配方即可产生不同选股结果，无需新增代码。

将来要做真正的新因子策略（例如基本面/价值）时：在 services 里写新的打分函数，
在 _build_scorers() 注册，并把名字加入 config.KNOWN_SCORERS（两者须保持一致，
test_strategy_registry 会断言这一点）。
"""
from __future__ import annotations

from typing import Any, Callable

# 打分函数签名：(rows, config) -> (scored_rows, warnings, weights)
Scorer = Callable[[list[dict[str, Any]], dict[str, Any]], tuple[list[dict[str, Any]], list[str], dict[str, float]]]


def _build_scorers() -> dict[str, Scorer]:
    # 延迟导入，避免与 hot_sector_candidates 形成模块级循环依赖。
    from .hot_sector_candidates import score_short_term_resonance

    return {
        "short_term_resonance": score_short_term_resonance,
    }


def available_scorers() -> frozenset[str]:
    """已注册的 scorer 名称集合。"""
    return frozenset(_build_scorers())


def get_scorer(name: str) -> Scorer:
    """按名称取打分引擎；未注册时抛出清晰错误。"""
    scorers = _build_scorers()
    scorer = scorers.get(name)
    if scorer is None:
        raise ValueError(f"未注册的 scorer：{name}（可用：{', '.join(sorted(scorers))}）")
    return scorer
