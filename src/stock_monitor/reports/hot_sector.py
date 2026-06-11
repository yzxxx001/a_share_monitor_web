from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..services.hot_sector_report import HotSectorReportResult


DISCLAIMER = "该结果为候选观察依据，不构成投资建议，不代表推荐买入、立即建仓或保证收益。"


def _board_label(result: HotSectorReportResult) -> str:
    return "概念" if getattr(result, "board_type", "industry") == "concept" else "行业"


def build_hot_sector_markdown(result: HotSectorReportResult) -> str:
    label = _board_label(result)
    lines = [
        f"# 收盘后热门{label}板块 Top {min(10, len(result.top_sectors))} 报告",
        "",
        f"- 交易日期：{result.trade_date}",
        f"- 生成时间：{result.generated_at:%Y-%m-%d %H:%M:%S}",
        f"- 数据来源：{result.provider_status.get('provider', 'unknown')} / {result.provider_status.get('source', 'unknown')}",
        f"- 数据完整状态：{result.data_status}",
        f"- 是否使用缓存：{'是' if result.provider_status.get('is_cached') else '否'}",
        f"- 是否采用降级评分：{'是' if result.score_mode != 'full' else '否'}",
        f"- 评分模式：{result.score_mode}",
        "",
        "## 评分标准",
        "",
        *_score_rule_lines(result),
        "",
        f"## 热门{label}板块 Top 10",
        "",
    ]
    if not result.top_sectors:
        if result.data_status == "data_unavailable":
            lines += [f"{label}板块数据源当前不可用，本次未生成热门{label}排名。请检查网络、代理规则或稍后重试。", ""]
        else:
            lines += [f"今日未识别到满足条件的明显强势{label}板块，本次不生成候选观察股清单。", ""]
    else:
        lines += [
            "| 排名 | 板块名称 | 综合评分 | 当日涨跌幅 | 上涨覆盖率 | 成交活跃度 | 主力净流入占比 | 近5日相对表现 | 风险或数据状态标签 |",
            "|---:|---|---:|---:|---:|---:|---:|---|---|",
        ]
        for item in result.top_sectors:
            lines.append(
                "| {rank} | {name} | {score:.2f} | {pct} | {up_ratio} | {activity} | {fund} | {relative} | {tags} |".format(
                    rank=item["rank"],
                    name=item["sector_name"],
                    score=item["sector_score"],
                    pct=_pct(item.get("pct_change")),
                    up_ratio=_pct(item.get("up_stock_ratio"), already_ratio=True),
                    activity=_activity_text(item),
                    fund=_pct(item.get("main_net_inflow_ratio"), already_ratio=True),
                    relative=_relative_text(item),
                    tags="、".join(item.get("data_tags", [])),
                )
            )
        lines.append("")
        lines.append("## 评分构成")
        lines.append("")
        for item in result.top_sectors:
            components = "；".join(f"{name}={value:.2f}" for name, value in item.get("score_components", {}).items())
            lines.append(f"- {item['rank']}. {item['sector_name']}：综合评分 {item['sector_score']:.2f}；{components}")
        lines.append("")

    lines += [
        "## Warnings",
        "",
    ]
    if result.warnings:
        lines.extend(f"- {warning}" for warning in result.warnings)
    else:
        lines.append("- 无")
    lines += ["", "## 免责声明", "", DISCLAIMER, ""]
    return "\n".join(lines)


def build_hot_sector_wecom_summary(result: HotSectorReportResult) -> str:
    label = _board_label(result)
    lines = [
        f"### 收盘热门{label}板块摘要",
        f"> 交易日期：{result.trade_date}",
        f"> 评分模式：{result.score_mode}；降级评分：{'是' if result.score_mode != 'full' else '否'}",
        "",
    ]
    for item in result.top_sectors[:3]:
        activity_text = _activity_text(item)
        fund_text = _pct(item.get("main_net_inflow_ratio"), already_ratio=True)
        lines.append(
            f"{item['rank']}. {item['sector_name']}：评分 {item['sector_score']:.1f}，"
            f"涨跌幅 {_pct(item.get('pct_change'))}，上涨覆盖 {_pct(item.get('up_stock_ratio'), already_ratio=True)}，"
            f"活跃度 {activity_text}，资金流 {fund_text}"
        )
    lines += ["", f"候选观察说明：仅用于收盘后{label}热度观察，不构成投资建议。"]
    return "\n".join(lines)


def build_hot_sector_html(result: HotSectorReportResult) -> str:
    label = _board_label(result)
    rows = []
    for item in result.top_sectors:
        rows.append(
            "<tr>"
            f"<td>{item['rank']}</td>"
            f"<td><strong>{escape(str(item['sector_name']))}</strong><br><small>{escape(str(item.get('sector_code', '')))}</small></td>"
            f"<td>{float(item['sector_score']):.2f}</td>"
            f"<td>{escape(_pct(item.get('pct_change')))}</td>"
            f"<td>{escape(_pct(item.get('up_stock_ratio'), already_ratio=True))}</td>"
            f"<td>{escape(_activity_text(item))}</td>"
            f"<td>{escape(_pct(item.get('main_net_inflow_ratio'), already_ratio=True))}</td>"
            f"<td>{escape(_relative_text(item))}</td>"
            f"<td>{escape('、'.join(item.get('data_tags', [])))}</td>"
            "</tr>"
        )
    if not rows:
        empty_text = (
            f"{label}板块数据源当前不可用，本次未生成热门{label}排名。请检查网络、代理规则或稍后重试。"
            if result.data_status == "data_unavailable"
            else f"今日未识别到满足条件的明显强势{label}板块，本次不生成候选观察股清单。"
        )
        rows.append(f'<tr><td colspan="9" class="empty">{escape(empty_text)}</td></tr>')

    components = []
    for item in result.top_sectors:
        detail = "；".join(f"{name}={value:.2f}" for name, value in item.get("score_components", {}).items())
        components.append(f"<li>{item['rank']}. {escape(str(item['sector_name']))}：综合评分 {float(item['sector_score']):.2f}；{escape(detail)}</li>")
    if not components:
        components.append("<li>无评分构成。</li>")

    warnings = [f"<li>{escape(warning)}</li>" for warning in result.warnings] or ["<li>无</li>"]
    cache_text = "是" if result.provider_status.get("is_cached") else "否"
    degraded_text = "是" if result.score_mode != "full" else "否"
    provider = f"{result.provider_status.get('provider', 'unknown')} / {result.provider_status.get('source', 'unknown')}"
    title = f"收盘后热门{label}板块 Top {min(10, len(result.top_sectors))} 报告"
    score_rules = "".join(f"<li>{escape(line[2:])}</li>" for line in _score_rule_lines(result))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body{{margin:0;background:#f6f8fb;color:#1f2937;font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Arial,sans-serif}}
    main{{max-width:1080px;margin:0 auto;padding:32px 22px 56px}}
    h1{{font-size:30px;margin:0 0 14px}} h2{{font-size:21px;margin:28px 0 12px;border-bottom:1px solid #d9e2ef;padding-bottom:6px}}
    .meta{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;margin:14px 0 22px}}
    .meta div,.note{{background:#fff;border:1px solid #d9e2ef;border-radius:8px;padding:10px 12px}}
    .rules{{background:#fff;border:1px solid #d9e2ef;border-radius:8px;padding:12px 14px;margin:0 0 22px}}
    .rules ul{{margin:6px 0 0 18px;padding:0}}
    table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d9e2ef}} th,td{{border:1px solid #d9e2ef;padding:8px 10px;vertical-align:top}} th{{background:#f1f5f9;text-align:left;white-space:nowrap}}
    small{{color:#64748b}} .empty{{text-align:center;color:#64748b;padding:24px}} .warning{{background:#fff7ed;border-color:#fed7aa}} .disclaimer{{background:#ecfdf5;border-color:#bbf7d0}}
  </style>
</head>
<body>
<main>
  <h1>{escape(title)}</h1>
  <section class="meta">
    <div>交易日期：<strong>{escape(result.trade_date)}</strong></div>
    <div>生成时间：<strong>{escape(result.generated_at.strftime("%Y-%m-%d %H:%M:%S"))}</strong></div>
    <div>数据来源：<strong>{escape(provider)}</strong></div>
    <div>数据完整状态：<strong>{escape(result.data_status)}</strong></div>
    <div>是否使用缓存：<strong>{cache_text}</strong></div>
    <div>是否采用降级评分：<strong>{degraded_text}</strong></div>
    <div>评分模式：<strong>{escape(result.score_mode)}</strong></div>
  </section>
  <section class="rules">
    <strong>评分标准</strong>
    <ul>{score_rules}</ul>
  </section>
  <h2>热门{escape(label)}板块 Top 10</h2>
  <table>
    <thead><tr><th>排名</th><th>板块名称</th><th>综合评分</th><th>当日涨跌幅</th><th>上涨覆盖率</th><th>成交活跃度</th><th>主力净流入占比</th><th>近5日相对表现</th><th>风险或数据状态标签</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <h2>评分构成</h2>
  <ul>{''.join(components)}</ul>
  <h2>Warnings</h2>
  <div class="note warning"><ul>{''.join(warnings)}</ul></div>
  <h2>免责声明</h2>
  <div class="note disclaimer">{escape(DISCLAIMER)}</div>
</main>
</body>
</html>
"""


def _pct(value: Any, already_ratio: bool = False) -> str:
    if value is None:
        return "-"
    number = float(value)
    if already_ratio:
        number *= 100
    return f"{number:.2f}%"


def _num(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}"


def _activity_text(item: dict[str, Any]) -> str:
    if item.get("activity_ratio") is not None:
        return _num(item.get("activity_ratio"))
    label = str(item.get("activity_label") or "")
    value = item.get("activity_value")
    if value is not None and label and label != "缺失":
        return f"{label} {_num(value)}"
    if item.get("turnover_rate") is not None:
        return f"换手率 {_pct(item.get('turnover_rate'))}"
    if item.get("amount") is not None:
        return f"成交额 {_num(item.get('amount'))}"
    return "-"


def _relative_text(item: dict[str, Any]) -> str:
    if item.get("relative_return_neutral"):
        return "缺失-中性分"
    value = item.get("relative_return_vs_benchmark")
    if item.get("relative_return_label") == "近5日表现替代":
        return f"近5日表现 {_pct(value)}"
    return _pct(value)


def _score_rule_lines(result: HotSectorReportResult) -> list[str]:
    labels = {
        "pct_change": "当日涨跌幅百分位",
        "up_stock_ratio": "上涨覆盖率百分位",
        "activity": "成交活跃度百分位",
        "fund_flow": "主力净流入占比百分位",
        "relative_return": "近5日相对表现百分位",
    }
    lines = [
        "- 排名不是按当日涨幅单项排序，而是按综合评分排序；因此高涨幅板块仍可能因成交活跃度、资金流或持续性较弱而排在后面。"
    ]
    for key, weight in result.score_weights.items():
        lines.append(f"- {labels.get(key, key)}：{weight * 100:.0f}%")
    lines.append("- 成交活跃度优先使用“当日成交额 / 近20日平均成交额”；缺失时降级为换手率或当日成交额百分位，并在数据状态标签标注。")
    lines.append("- 近5日相对表现缺失时不再显示为 0.00%，评分采用中性分 50，并在数据状态标签标注。")
    return lines
