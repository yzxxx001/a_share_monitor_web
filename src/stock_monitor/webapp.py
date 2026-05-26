from __future__ import annotations

import atexit
import hmac
import json
import logging
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from markupsafe import Markup

from .models import Position
from .providers.hot_sector import AKShareHotSectorProvider
from .runner import MonitorService, build_service
from .services.hot_sector_report import HotSectorReportService, normalize_trade_date
from .signal_engine import evaluate_position


LOGGER = logging.getLogger(__name__)
CODE_PATTERN = re.compile(r"^([0-9]{6})(?:\.(SH|SZ|BJ))?$", re.IGNORECASE)


def normalize_a_share_code(value: str) -> str | None:
    token = value.strip().upper().replace(" ", "")
    match = CODE_PATTERN.match(token)
    if not match:
        return None
    symbol, exchange = match.groups()
    if exchange:
        return f"{symbol}.{exchange.upper()}"
    if symbol.startswith("6"):
        exchange = "SH"
    elif symbol.startswith(("0", "3")):
        exchange = "SZ"
    elif symbol.startswith(("4", "8", "9")):
        exchange = "BJ"
    else:
        return None
    return f"{symbol}.{exchange}"


def _num(name: str, default: float) -> float:
    raw = request.form.get(name, "").strip()
    return default if raw == "" else float(raw)


def _int(name: str, default: int) -> int:
    raw = request.form.get(name, "").strip()
    return default if raw == "" else int(float(raw))


def build_position_from_form(ts_code: str, name: str, existing: Position | None = None) -> Position:
    base = existing or Position(
        enabled=True, ts_code=ts_code, name=name, shares=0, available_to_sell=0, average_cost=0.0,
        account_total_value=100000.0, cash_available=0.0, max_position_pct=0.30, stop_loss_pct=0.05,
        take_profit_pct=0.12, trailing_stop_pct=0.04, max_single_buy_pct=0.10,
    )
    return Position(
        enabled=request.form.get("enabled", "1") == "1",
        ts_code=ts_code,
        name=request.form.get("name", name).strip() or name,
        shares=_int("shares", base.shares),
        available_to_sell=_int("available_to_sell", base.available_to_sell),
        average_cost=_num("average_cost", base.average_cost),
        account_total_value=_num("account_total_value", base.account_total_value),
        cash_available=_num("cash_available", base.cash_available),
        max_position_pct=_num("max_position_pct", base.max_position_pct),
        stop_loss_pct=_num("stop_loss_pct", base.stop_loss_pct),
        take_profit_pct=_num("take_profit_pct", base.take_profit_pct),
        trailing_stop_pct=_num("trailing_stop_pct", base.trailing_stop_pct),
        max_single_buy_pct=_num("max_single_buy_pct", base.max_single_buy_pct),
        peak_price_since_entry=base.peak_price_since_entry,
        note=request.form.get("note", base.note).strip(),
    )


def price_chart_svg(bars) -> Markup:
    if bars.empty:
        return Markup('<div class="empty-chart">尚无分钟行情数据</div>')
    points = bars.tail(60)["close"].astype(float).tolist()
    width, height, pad = 760, 210, 26
    high, low = max(points), min(points)
    span = high - low if high != low else 1.0
    coords = []
    for index, price in enumerate(points):
        x = pad + index * (width - 2 * pad) / max(len(points) - 1, 1)
        y = pad + (high - price) * (height - 2 * pad) / span
        coords.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(coords)
    return Markup(
        f'<svg class="price-chart" viewBox="0 0 {width} {height}" role="img" aria-label="收盘价趋势">'
        f'<text x="8" y="18">{high:.2f}</text><text x="8" y="{height - 8}">{low:.2f}</text>'
        f'<line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" />'
        f'<polyline points="{polyline}" /></svg>'
    )


def load_hot_sector_report(service: MonitorService) -> dict[str, object] | None:
    report_dir = service.settings.hot_sector_monitor.report.directory
    today = normalize_trade_date("today", service.zone)
    candidates = [report_dir / f"{today}_sector_summary.json"]
    if report_dir.exists():
        candidates.extend(sorted(report_dir.glob("*_sector_summary.json"), key=lambda path: path.stat().st_mtime, reverse=True))
    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["json_path"] = str(path)
            payload["markdown_path"] = str(report_dir / f"{str(payload.get('trade_date', '') or path.name[:8])}_sector_summary.md")
            payload["html_path"] = str(report_dir / f"{str(payload.get('trade_date', '') or path.name[:8])}_sector_summary.html")
            return payload
        except Exception as exc:
            LOGGER.warning("读取热门行业板块报告失败：%s；%s", path, exc)
    return None


def build_hot_sector_service(app: Flask, service: MonitorService) -> HotSectorReportService:
    provider = app.config.get("HOT_SECTOR_PROVIDER")
    if provider is None:
        provider = AKShareHotSectorProvider(service.settings.hot_sector_monitor.http, service.settings.hot_sector_monitor.cache)
    wecom = app.config.get("HOT_SECTOR_WECOM")
    return HotSectorReportService(service.settings, provider, wecom=wecom)


def generate_hot_sector_report(app: Flask, service: MonitorService, trade_date: str, *, notify: bool, force_send: bool = False):
    report_service = build_hot_sector_service(app, service)
    return report_service.generate(trade_date, notify=notify, force_send=force_send)


def start_scheduler(app: Flask, service: MonitorService) -> None:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler(timezone=service.settings.app.timezone)
    trigger = CronTrigger(
        minute=f"*/{service.settings.app.schedule_minutes}",
        second=service.settings.app.run_delay_seconds,
        timezone=service.settings.app.timezone,
    )
    scheduler.add_job(service.run_once, trigger=trigger, id="stock_monitor_web", max_instances=1, coalesce=True, replace_existing=True)
    if service.settings.hot_sector_monitor.enabled:
        try:
            hour_text, minute_text = service.settings.hot_sector_monitor.schedule.close_report_time.split(":", maxsplit=1)
            close_trigger = CronTrigger(
                hour=int(hour_text),
                minute=int(minute_text),
                second=0,
                timezone=service.settings.app.timezone,
            )
            scheduler.add_job(
                lambda: generate_hot_sector_report(
                    app,
                    service,
                    "today",
                    notify=service.settings.hot_sector_monitor.notification.send_close_summary,
                ),
                trigger=close_trigger,
                id="hot_sector_close_report",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
        except Exception as exc:
            LOGGER.warning("收盘热门行业板块报告定时任务未启用：%s", exc)
    scheduler.start()
    app.extensions["monitor_scheduler"] = scheduler
    atexit.register(lambda: scheduler.shutdown(wait=False) if scheduler.running else None)


def create_app(config_path: str | Path, scheduler_enabled: bool | None = None) -> Flask:
    service = build_service(config_path)
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SECRET_KEY"] = os.getenv(service.settings.web.secret_key_env, "").strip() or secrets.token_hex(32)
    app.config["MONITOR_SERVICE"] = service
    app.config["MONITOR_SETTINGS"] = service.settings
    access_token = os.getenv(service.settings.web.access_token_env, "").strip()

    @app.context_processor
    def inject_global_values():
        return {"auth_enabled": bool(access_token)}

    @app.before_request
    def require_login():
        if not access_token or request.endpoint in {"login", "static"}:
            return None
        if session.get("authenticated"):
            return None
        return redirect(url_for("login", next=request.full_path))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not access_token:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            entered = request.form.get("token", "")
            if hmac.compare_digest(entered, access_token):
                session["authenticated"] = True
                return redirect(url_for("dashboard"))
            flash("访问口令不正确。", "error")
        return render_template("login.html", title="登录")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    def dashboard():
        query = request.args.get("q", "").strip()
        candidates = service.store.find_stock_candidates(query) if query else []
        return render_template(
            "dashboard.html", title="监控面板", settings=service.settings, snapshots=service.snapshot(),
            runs=service.store.list_runs(8), events=service.store.list_signal_events(12),
            stock_master_count=service.store.stock_master_count(), query=query, candidates=candidates,
            hot_sector_report=load_hot_sector_report(service),
            auth_enabled=bool(access_token),
        )

    @app.post("/actions/import-excel")
    def import_excel():
        try:
            count, warnings = service.import_excel_positions()
            flash(f"已从 Excel 导入或更新 {count} 个启用标的。", "success")
            for warning in warnings[:5]:
                flash(warning, "warning")
        except Exception as exc:
            flash(f"导入 Excel 失败：{exc}", "error")
        return redirect(url_for("dashboard"))

    @app.post("/actions/sync-master")
    def sync_master():
        try:
            count = service.sync_stock_master()
            backfilled = service.store.backfill_watchlist_names()
            extra = f"，已为 {backfilled} 个监控标的补全股票名称" if backfilled else ""
            flash(f"已同步 {count} 条上市 A 股基础信息，可按股票名称搜索添加{extra}。", "success")
        except Exception as exc:
            flash(f"同步股票基础列表失败：{exc}", "error")
        return redirect(url_for("dashboard"))

    @app.post("/actions/run-once")
    def run_once():
        force_session = request.form.get("force_session") == "1"
        summary = service.run_once(force_session=force_session)
        category = "success" if summary.status == "OK" else "warning"
        flash(summary.message, category)
        return redirect(url_for("dashboard"))

    @app.post("/actions/hot-sector-report")
    def hot_sector_report():
        notify = request.form.get("notify") == "1"
        force_send = request.form.get("force_send") == "1"
        try:
            result = generate_hot_sector_report(app, service, "today", notify=notify, force_send=force_send)
            message = f"已生成热门行业板块报告：Top {len(result.top_sectors)}，{result.markdown_path.name}"
            if result.notification:
                message += f"；企业微信：{result.notification.detail}"
            if result.notification_skipped_reason:
                message += f"；{result.notification_skipped_reason}"
            flash(message, "success")
        except Exception as exc:
            LOGGER.exception("生成热门行业板块报告失败。")
            flash(f"生成热门行业板块报告失败：{exc}", "error")
        return redirect(url_for("dashboard"))

    @app.post("/watchlist/add")
    def add_watch_item():
        query = request.form.get("query", "").strip()
        selected_code = request.form.get("selected_ts_code", "").strip().upper()
        selected_name = request.form.get("selected_name", "").strip()
        ts_code = selected_code or normalize_a_share_code(query)
        name = selected_name
        if not ts_code:
            matches = service.store.find_stock_candidates(query)
            exact = [item for item in matches if item["name"] == query]
            if len(exact) == 1:
                ts_code, name = str(exact[0]["ts_code"]), str(exact[0]["name"])
            elif len(matches) == 1:
                ts_code, name = str(matches[0]["ts_code"]), str(matches[0]["name"])
            elif matches:
                flash("找到多个候选，请从下方搜索结果中选择要添加的股票。", "warning")
                return redirect(url_for("dashboard", q=query))
            else:
                flash("无法识别股票。代码可直接填写 600000 或 600000.SH；名称添加前请先同步股票基础列表。", "error")
                return redirect(url_for("dashboard", q=query))
        if not name:
            matches = service.store.find_stock_candidates(ts_code, limit=1)
            if matches and str(matches[0]["ts_code"]) == ts_code:
                name = str(matches[0]["name"])
        try:
            position = build_position_from_form(ts_code, name)
            errors = position.validate()
            if errors:
                raise ValueError("；".join(errors))
            service.store.upsert_watch_item(position)
            flash(f"已添加或更新监控标的：{position.display_name}。", "success")
        except Exception as exc:
            flash(f"添加失败：{exc}", "error")
        return redirect(url_for("dashboard"))

    @app.route("/stocks/<ts_code>", methods=["GET", "POST"])
    def stock_detail(ts_code: str):
        position = service.store.get_watch_item(ts_code.upper())
        if position is None:
            flash("监控标的不存在。", "error")
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            try:
                updated = build_position_from_form(position.ts_code, position.name, position)
                errors = updated.validate()
                if errors:
                    raise ValueError("；".join(errors))
                service.store.upsert_watch_item(updated)
                flash("仓位与风险参数已保存。", "success")
                return redirect(url_for("stock_detail", ts_code=ts_code))
            except Exception as exc:
                flash(f"保存失败：{exc}", "error")
        bars = service.store.load_recent_bars(position.ts_code, service.settings.market.cached_bars)
        evaluation = None if bars.empty else evaluate_position(position, bars, service.settings.rules, service.store.get_peak(position.ts_code))
        return render_template(
            "stock_detail.html", title=position.display_name, settings=service.settings,
            position=position, evaluation=evaluation, bars=bars.tail(30).iloc[::-1].to_dict("records"),
            chart=price_chart_svg(bars), events=service.store.list_signal_events(20, position.ts_code),
            watchlist=service.store.list_watchlist(),
        )

    @app.post("/stocks/<ts_code>/toggle")
    def toggle_stock(ts_code: str):
        position = service.store.get_watch_item(ts_code.upper())
        if position:
            service.store.set_watch_enabled(position.ts_code, not position.enabled)
            flash(f"{position.display_name} 已{'暂停' if position.enabled else '启用'}监控。", "success")
        return redirect(url_for("dashboard"))

    @app.post("/stocks/<ts_code>/delete")
    def delete_stock(ts_code: str):
        position = service.store.get_watch_item(ts_code.upper())
        if position:
            service.store.delete_watch_item(position.ts_code)
            flash(f"已删除监控标的：{position.display_name}。", "success")
        return redirect(url_for("dashboard"))

    @app.post("/stocks/<ts_code>/rename")
    def rename_stock(ts_code: str):
        old_code = ts_code.upper()
        new_code = normalize_a_share_code(request.form.get("new_ts_code", ""))
        if not new_code:
            flash("无法识别新股票代码，请填写如 600000 或 600000.SH 格式。", "error")
            return redirect(url_for("stock_detail", ts_code=old_code))
        if new_code == old_code:
            flash("新股票代码与当前代码相同，未作修改。", "warning")
            return redirect(url_for("stock_detail", ts_code=old_code))
        if service.store.get_watch_item(new_code) is not None:
            flash(f"代码 {new_code} 已在监控列表中，无法重命名为该代码。", "error")
            return redirect(url_for("stock_detail", ts_code=old_code))
        if service.store.get_watch_item(old_code) is None:
            flash("监控标的不存在。", "error")
            return redirect(url_for("dashboard"))
        try:
            service.store.rename_watch_item(old_code, new_code)
            flash(f"股票代码已从 {old_code} 更改为 {new_code}。历史行情与信号记录已同步迁移。", "success")
            return redirect(url_for("stock_detail", ts_code=new_code))
        except Exception as exc:
            flash(f"修改代码失败：{exc}", "error")
            return redirect(url_for("stock_detail", ts_code=old_code))

    @app.get("/api/stocks")
    def api_stocks():
        payload = []
        for item in service.snapshot():
            position, evaluation = item["position"], item["evaluation"]
            payload.append(
                {
                    "ts_code": position.ts_code, "name": position.name, "enabled": position.enabled,
                    "shares": position.shares,
                    "latest_price": None if evaluation is None else evaluation.latest_price,
                    "pnl_pct": None if evaluation is None else evaluation.pnl_pct,
                    "position_pct": None if evaluation is None else evaluation.position_pct,
                    "signals": [] if evaluation is None else [signal.rule_id for signal in evaluation.signals],
                }
            )
        return jsonify(payload)

    @app.get("/api/stocks/<ts_code>/bars")
    def api_bars(ts_code: str):
        bars = service.store.load_recent_bars(ts_code.upper(), service.settings.market.cached_bars)
        records = bars.copy()
        if not records.empty:
            records["time"] = records["time"].dt.strftime("%Y-%m-%d %H:%M:%S")
        return jsonify(records.to_dict("records"))

    enabled = service.settings.web.auto_start_scheduler if scheduler_enabled is None else scheduler_enabled
    if enabled:
        start_scheduler(app, service)
    return app
