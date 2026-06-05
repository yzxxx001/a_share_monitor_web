from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..services.hot_sector_candidates import HotSectorCandidateReportResult


DISCLAIMER = "本报告仅用于短线候选观察与复盘，不构成投资建议，不输出推荐买入、建议建仓或确定性收益结论。"


def build_candidate_markdown(result: HotSectorCandidateReportResult) -> str:
    lines = [
        f"# {result.sector_name} 短线候选观察股报告",
        "",
        f"- 板块名称：{result.sector_name}",
        f"- 策略名称：{result.strategy_name}",
        f"- 交易日期：{result.trade_date}",
        f"- 生成时间：{result.generated_at.isoformat(timespec='seconds')}",
        f"- 板块热度评分：{_fmt(result.sector_heat_score)}",
        f"- 板块当日涨跌幅：{_pct(result.sector_change_pct)}",
        f"- 数据状态：{result.data_status}",
        f"- 免责声明：{DISCLAIMER}",
        "",
        "## 候选观察股 Top 10",
        "",
    ]
    if result.candidates:
        lines.extend(
            [
                "| 排名 | 股票代码 | 股票名称 | 总评分 | 当日涨跌幅 | 相对板块表现 | 成交额倍数 | 趋势状态 | RSI状态 | 风险标签 | 数据完整状态 |",
                "|---:|---|---|---:|---:|---:|---:|---|---|---|---|",
            ]
        )
        for item in result.candidates:
            lines.append(
                "| {rank} | {code} | {name} | {score} | {pct} | {relative} | {amount_ratio} | {trend} | {rsi} | {tags} | {status} |".format(
                    rank=item.get("rank", ""),
                    code=item.get("stock_code", ""),
                    name=item.get("stock_name", ""),
                    score=_fmt(item.get("short_term_score")),
                    pct=_pct(item.get("pct_change")),
                    relative=_pct(item.get("relative_return")),
                    amount_ratio=_ratio(item.get("amount_ratio")),
                    trend=item.get("trend_state", ""),
                    rsi=item.get("rsi_state", ""),
                    tags=_join(item.get("risk_tags")),
                    status=item.get("data_status", ""),
                )
            )
        lines.append("")
        lines.append("### 入选理由")
        for item in result.candidates:
            lines.append(f"- {item.get('rank')}. {item.get('stock_name')}（{item.get('stock_code')}）：{item.get('selection_reason')}")
    else:
        lines.append("本次未生成正常候选观察股。")

    lines.extend(["", "## 仅观察标的", ""])
    if result.observe_only:
        lines.extend(["| 股票代码 | 股票名称 | 原因 | 风险标签 | 数据状态 |", "|---|---|---|---|---|"])
        for item in result.observe_only:
            lines.append(
                f"| {item.get('stock_code', '')} | {item.get('stock_name', '')} | {_join(item.get('reasons'))} | {_join(item.get('risk_tags'))} | {item.get('data_status', '')} |"
            )
    else:
        lines.append("无。")

    lines.extend(["", "## 被排除标的", ""])
    if result.excluded:
        lines.extend(["| 股票代码 | 股票名称 | 排除原因 | 风险标签 | 数据状态 |", "|---|---|---|---|---|"])
        for item in result.excluded:
            lines.append(
                f"| {item.get('stock_code', '')} | {item.get('stock_name', '')} | {_join(item.get('reasons'))} | {_join(item.get('risk_tags'))} | {item.get('data_status', '')} |"
            )
    else:
        lines.append("无。")

    lines.extend(["", "## 评分配置", ""])
    for key, value in result.score_weights.items():
        lines.append(f"- {key}: {float(value):.2%}")

    lines.extend(["", "## 数据 Warnings", ""])
    if result.warnings:
        for warning in result.warnings:
            lines.append(f"- {warning}")
    else:
        lines.append("无。")
    lines.extend(["", f"> {DISCLAIMER}", ""])
    return "\n".join(lines)


def build_candidate_html(result: HotSectorCandidateReportResult) -> str:
    candidate_rows = "".join(
        "<tr>"
        f"<td>{escape(str(item.get('rank', '')))}</td>"
        f"<td><strong>{escape(str(item.get('stock_name', '')))}</strong><br><small>{escape(str(item.get('stock_code', '')))}</small></td>"
        f"<td>{escape(_fmt(item.get('short_term_score')))}</td>"
        f"<td class=\"{_pos_neg(item.get('pct_change'))}\">{escape(_pct(item.get('pct_change')))}</td>"
        f"<td>{escape(_pct(item.get('relative_return')))}</td>"
        f"<td>{escape(_ratio(item.get('amount_ratio')))}</td>"
        f"<td>{escape(str(item.get('trend_state', '')))}</td>"
        f"<td>{escape(str(item.get('rsi_state', '')))}</td>"
        f"<td>{escape(_join(item.get('risk_tags')))}</td>"
        f"<td>{escape(str(item.get('data_status', '')))}</td>"
        "</tr>"
        for item in result.candidates
    )
    if not candidate_rows:
        candidate_rows = '<tr><td colspan="10" class="empty">本次未生成正常候选观察股。</td></tr>'

    observe_rows = _simple_rows(result.observe_only, empty="无仅观察标的。")
    excluded_rows = _simple_rows(result.excluded, empty="无被排除标的。")
    reasons = "".join(
        f"<li>{escape(str(item.get('rank', '')))}. {escape(str(item.get('stock_name', '')))}"
        f"（{escape(str(item.get('stock_code', '')))}）：{escape(str(item.get('selection_reason', '')))}</li>"
        for item in result.candidates
    ) or "<li>无。</li>"
    weights = "".join(f"<li>{escape(str(key))}: {float(value):.2%}</li>" for key, value in result.score_weights.items())
    warnings = "".join(f"<li>{escape(str(warning))}</li>" for warning in result.warnings) or "<li>无。</li>"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(result.sector_name)} 短线热点共振候选报告</title>
  <style>
    body{{margin:0;background:#f5f7fb;color:#1c2534;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Arial,sans-serif}}
    main{{max-width:1220px;margin:0 auto;padding:24px}}
    section{{background:#fff;border:1px solid #e4e8f0;border-radius:10px;padding:18px;margin-bottom:16px;box-shadow:0 3px 12px rgba(24,39,66,.06)}}
    h1{{font-size:24px;margin:0 0 10px}} h2{{font-size:17px;margin:0 0 12px}} small,.muted{{color:#66758a}}
    .meta{{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}} .meta span{{background:#f1f5fb;border:1px solid #e4e8f0;border-radius:6px;padding:5px 9px}}
    table{{width:100%;border-collapse:collapse}} th{{background:#f7f9fc;color:#5a697c;text-align:left}} th,td{{border-top:1px solid #e4e8f0;padding:9px;vertical-align:top}} .positive{{color:#0f8a5f}} .negative{{color:#c43d4d}} .empty{{text-align:center;color:#66758a;padding:20px}}
    .notice{{color:#66758a}}
  </style>
</head>
<body>
<main>
  <section>
    <h1>{escape(result.sector_name)} 短线热点共振候选报告</h1>
    <div class="meta">
      <span>策略：{escape(result.strategy_name)}</span>
      <span>交易日期：{escape(result.trade_date)}</span>
      <span>板块热度：{escape(_fmt(result.sector_heat_score))}</span>
      <span>板块涨跌幅：{escape(_pct(result.sector_change_pct))}</span>
      <span>数据状态：{escape(result.data_status)}</span>
    </div>
    <p class="notice">{escape(DISCLAIMER)}</p>
  </section>
  <section>
    <h2>候选观察股 Top 10</h2>
    <table>
      <thead><tr><th>排名</th><th>股票</th><th>总评分</th><th>当日涨跌幅</th><th>相对板块</th><th>成交额倍数</th><th>趋势状态</th><th>RSI状态</th><th>风险标签</th><th>数据状态</th></tr></thead>
      <tbody>{candidate_rows}</tbody>
    </table>
  </section>
  <section><h2>入选理由</h2><ul>{reasons}</ul></section>
  <section>
    <h2>仅观察标的</h2>
    <table><thead><tr><th>股票</th><th>原因</th><th>风险标签</th><th>数据状态</th></tr></thead><tbody>{observe_rows}</tbody></table>
  </section>
  <section>
    <h2>被排除标的</h2>
    <table><thead><tr><th>股票</th><th>原因</th><th>风险标签</th><th>数据状态</th></tr></thead><tbody>{excluded_rows}</tbody></table>
  </section>
  <section><h2>评分配置</h2><ul>{weights}</ul></section>
  <section><h2>数据 Warnings</h2><ul>{warnings}</ul></section>
</main>
</body>
</html>"""


def build_candidate_wecom_summary(result: HotSectorCandidateReportResult) -> str:
    lines = [
        f"### {result.sector_name} 短线候选观察",
        f"> 策略：{result.strategy_name}；日期：{result.trade_date}",
        f"> {DISCLAIMER}",
    ]
    for item in result.candidates[:3]:
        lines.append(
            f"{item.get('rank')}. {item.get('stock_name')}：评分 {_fmt(item.get('short_term_score'))}，"
            f"相对板块 {_pct(item.get('relative_return'))}，{item.get('trend_state', '')}"
        )
    if not result.candidates:
        lines.append("本次未生成正常候选观察股。")
    return "\n".join(lines)


def _simple_rows(items: list[dict[str, Any]], *, empty: str) -> str:
    if not items:
        return f'<tr><td colspan="4" class="empty">{escape(empty)}</td></tr>'
    return "".join(
        "<tr>"
        f"<td><strong>{escape(str(item.get('stock_name', '')))}</strong><br><small>{escape(str(item.get('stock_code', '')))}</small></td>"
        f"<td>{escape(_join(item.get('reasons')))}</td>"
        f"<td>{escape(_join(item.get('risk_tags')))}</td>"
        f"<td>{escape(str(item.get('data_status', '')))}</td>"
        "</tr>"
        for item in items
    )


def _pos_neg(value: Any) -> str:
    try:
        return "positive" if float(value) > 0 else "negative"
    except (TypeError, ValueError):
        return ""


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _pct(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return str(value)


def _ratio(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}x"
    except (TypeError, ValueError):
        return str(value)


def _join(value: Any) -> str:
    if not value:
        return "-"
    if isinstance(value, list | tuple):
        return "、".join(str(item) for item in value if item)
    return str(value)
