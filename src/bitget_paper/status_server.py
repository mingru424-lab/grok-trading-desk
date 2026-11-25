"""Read-only status page bound to 127.0.0.1.

The page shows the snapshot the paper process last published. It does not
place orders, and it does not accept a host other than loopback.

Visual language is copied from the user's k-line-director frontend
(commit 30f6897ccd611c31a84cdfd26ac09523962d5edd):

- frontend/src/styles/theme.css          colors, type, cards, badges
- frontend/src/components/AppHeader.vue  sticky bar, nav, theme button
- frontend/src/stores/theme.ts           light/dark on data-theme
- frontend/src/components/Thermometer.vue metric cards
- frontend/src/components/SignalTable.vue decision table
- frontend/src/components/KlineChart.vue  card around a chart
- frontend/src/components/FooterDisclaimer.vue footer

Candles, AI signals, A-shares, futures, news and backtests are not drawn.
Those screens have no backend here. The chart is the local equity series only.
CSS is embedded, so the page needs no build and no CDN.
"""

from __future__ import annotations

import json
import threading
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

SnapshotFn = Callable[[], dict[str, Any]]

_RUNTIME = {
    "starting": "启动中",
    "running": "运行中",
    "paused": "已暂停",
    "emergency_stop": "紧急停止",
    "stopped": "已停止",
}
_FEED = {
    "ok": "可用",
    "rejected": "已拒绝",
    "unknown": "未采集",
}
_ACTION = {
    "BUY": "买入",
    "SELL": "卖出",
    "HOLD": "持有",
    "NO_TRADE": "不交易",
}

_CSS = """
:root {
  --bg: #ffffff;
  --bg-soft: #f5f7fa;
  --bg-panel: #ffffff;
  --text: #1f2329;
  --text-soft: #646a73;
  --border: #e5e6eb;
  --primary: #2b5cff;
  --primary-soft: #e8efff;
  --up: #e5484d;
  --down: #2ba471;
  --buy: #2b5cff;
  --sell: #f54a45;
  --badge-bg: #eef2ff;
  --shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
}
[data-theme='dark'] {
  --bg: #14161a;
  --bg-soft: #1b1e24;
  --bg-panel: #1b1e24;
  --text: #e6e8eb;
  --text-soft: #9aa0a6;
  --border: #2a2e35;
  --primary: #4d7dff;
  --primary-soft: #1e2a4a;
  --up: #ff6b6b;
  --down: #3ddc97;
  --buy: #4d7dff;
  --sell: #ff6b6b;
  --badge-bg: #232a3d;
  --shadow: 0 1px 3px rgba(0, 0, 0, 0.4);
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; min-height: 100%; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
    'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
  font-size: 14px;
  line-height: 1.5;
}
a { color: inherit; text-decoration: none; }
.container { max-width: 1280px; margin: 0 auto; padding: 0 16px; }
.card {
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: var(--shadow);
}
.up { color: var(--up); }
.down { color: var(--down); }
.badge {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 600;
}
.badge.buy { background: var(--primary-soft); color: var(--buy); }
.badge.sell { background: rgba(245, 74, 69, 0.12); color: var(--sell); }
.badge.none { background: var(--bg-soft); color: var(--text-soft); }
.badge.warn { background: rgba(245, 74, 69, 0.12); color: var(--sell); }
.header {
  position: sticky; top: 0; z-index: 100;
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border);
}
.header-inner { display: flex; align-items: center; gap: 20px; height: 52px; }
.logo { font-size: 17px; font-weight: 700; white-space: nowrap; }
.nav { display: flex; gap: 4px; flex: 1; overflow-x: auto; }
.nav-link {
  padding: 6px 12px; border-radius: 6px; color: var(--text-soft); white-space: nowrap;
}
.nav-link:hover { color: var(--primary); background: var(--bg-soft); }
.theme-btn {
  border: 1px solid var(--border);
  background: var(--bg-panel);
  border-radius: 6px;
  width: 34px; height: 34px;
  cursor: pointer;
  font-size: 16px;
}
.paper {
  margin-top: 12px;
  padding: 10px 14px;
  border-radius: 8px;
  background: rgba(245, 74, 69, 0.1);
  color: var(--sell);
  font-weight: 700;
}
.page-title { font-size: 24px; margin: 20px 0 6px; }
.page-sub { color: var(--text-soft); margin: 0 0 8px; }
.metrics { display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0; }
.tcard { padding: 12px 16px; min-width: 180px; flex: 1; }
.tlabel { color: var(--text-soft); font-size: 12px; }
.tvalue {
  font-size: 28px; font-weight: 700; margin: 4px 0;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.tband { font-size: 13px; font-weight: 600; }
.tsub { color: var(--text-soft); font-size: 12px; margin-top: 4px; overflow-wrap: anywhere; }
.split { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.panel { padding: 12px 16px; }
.panel h2, .chart-head { font-size: 16px; margin: 0 0 8px; }
.kv { display: grid; grid-template-columns: 132px 1fr; gap: 4px 10px; font-size: 13px; }
.kv dt { color: var(--text-soft); }
.kv dd { margin: 0; overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
.chart-card { margin: 16px 0; padding: 10px; }
.chart-head { display: flex; align-items: center; padding: 2px 6px 8px; font-weight: 700; }
.equity { width: 100%; height: 180px; color: var(--primary); display: block; }
.table-wrap { overflow: auto; max-height: 420px; }
table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 13px; }
th, td {
  padding: 7px 10px; border-bottom: 1px solid var(--border);
  text-align: left; white-space: nowrap; max-width: 360px;
  overflow: hidden; text-overflow: ellipsis;
}
th { color: var(--text-soft); font-weight: 600; background: var(--bg-soft); position: sticky; top: 0; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.empty, .hint { color: var(--text-soft); font-size: 12px; padding: 8px; }
.note { margin: 16px 0; padding: 12px 16px; color: var(--text-soft); font-size: 13px; }
.footer { margin-top: 32px; border-top: 1px solid var(--border); padding: 20px 0 32px; }
.disc, .copy { color: var(--text-soft); font-size: 12px; line-height: 1.7; }
.copy { margin-top: 8px; }
@media (max-width: 800px) {
  .header-inner { height: auto; flex-wrap: wrap; padding: 8px 0; }
  .split { grid-template-columns: 1fr; }
  .tvalue { font-size: 22px; }
  .nav { order: 3; width: 100%; }
}
"""

_THEME_JS = """
<script>
(function () {
  var key = 'qianduoduo-theme';
  var theme = 'light';
  try {
    var saved = localStorage.getItem(key);
    if (saved === 'dark' || saved === 'light') theme = saved;
  } catch (e) {}
  document.documentElement.setAttribute('data-theme', theme);
  function paint() {
    var button = document.getElementById('theme-toggle');
    if (!button) return;
    var dark = document.documentElement.getAttribute('data-theme') === 'dark';
    button.textContent = dark ? '\\u2600\\ufe0f' : '\\uD83C\\uDF19';
    button.title = dark ? '切换明亮' : '切换暗黑';
  }
  document.addEventListener('DOMContentLoaded', paint);
  document.addEventListener('click', function (event) {
    var button = event.target && event.target.closest ? event.target.closest('#theme-toggle') : null;
    if (!button) return;
    var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem(key, next); } catch (e) {}
    paint();
  });
  paint();
})();
</script>
"""


def _esc(value: Any) -> str:
    text = str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _shown(value: Any, fallback: str = "未采集") -> str:
    text = "" if value is None else str(value).strip()
    return _esc(text) if text else fallback


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > 40:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or abs(number) > Decimal("1000000000000"):
        return None
    return number


def _pnl_class(value: Any) -> str:
    number = _decimal(value)
    if number is None or number == 0:
        return ""
    return "up" if number > 0 else "down"


def _labeled(mapping: dict[str, str], raw: Any) -> str:
    text = "" if raw is None else str(raw).strip()
    if not text:
        return "未采集"
    label = mapping.get(text, "未识别")
    return f"{label} · {_esc(text)}"


def _badge(action: Any) -> str:
    text = "" if action is None else str(action).strip()
    if text == "BUY":
        kind = "buy"
    elif text == "SELL":
        kind = "sell"
    else:
        kind = "none"
    label = _ACTION.get(text, "未识别" if text else "未采集")
    shown = _esc(text) if text else "—"
    return f'<span class="badge {kind}">{_esc(label)} · {shown}</span>'


def _rows(items: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    chosen = [item for item in items if isinstance(item, dict)]
    return chosen[-limit:]


def _equity_svg(points: list[dict[str, Any]]) -> str:
    parsed: list[Decimal] = []
    for item in points:
        number = _decimal(item.get("equity_usd"))
        if number is not None:
            parsed.append(number)
    if len(parsed) < 2:
        return '<p class="empty">尚无足够权益点，不绘制曲线。</p>'
    width, height, pad = 640, 180, 16
    low, high = min(parsed), max(parsed)
    span = high - low
    if span == 0:
        span = Decimal("1")
    coords = []
    last = Decimal(max(len(parsed) - 1, 1))
    for index, number in enumerate(parsed):
        x = Decimal(pad) + (Decimal(index) * Decimal(width - 2 * pad) / last)
        y = Decimal(pad) + (high - number) * Decimal(height - 2 * pad) / span
        coords.append(f"{x:.2f},{y:.2f}")
    note = ""
    if len(parsed) < 8:
        note = f'<p class="hint">只有 {len(parsed)} 个已记录的权益点，历史很短。</p>'
    return (
        note
        + f'<svg class="equity" viewBox="0 0 {width} {height}" role="img" aria-label="本地权益曲线">'
        + f'<polyline fill="none" stroke="currentColor" stroke-width="2" points="{" ".join(coords)}"/>'
        + "</svg>"
        + f'<p class="hint">共 {len(parsed)} 个点，来自本机权益记录，不是行情 K 线。</p>'
    )


def _decision_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<tr><td colspan="5" class="empty">尚无决策记录。</td></tr>'
    body = []
    for item in rows:
        reason = "" if item.get("reason") is None else str(item.get("reason"))
        body.append(
            "<tr>"
            f"<td title=\"{_esc(item.get('ts', ''))}\">{_shown(item.get('ts'), '—')}</td>"
            f"<td>{_badge(item.get('action'))}</td>"
            f"<td title=\"{_esc(reason)}\">{_shown(reason, '—')}</td>"
            f"<td title=\"{_esc(item.get('correlation_id', ''))}\">{_shown(item.get('correlation_id'), '—')}</td>"
            f"<td>{_fill_label(item.get('fill_written'))}</td>"
            "</tr>"
        )
    return "".join(body)


def _fill_label(value: Any) -> str:
    if value is True:
        return "已写入本地成交"
    if value is False:
        return "未写入成交"
    return "未采集"


def _equity_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<tr><td colspan="5" class="empty">尚无权益记录。</td></tr>'
    body = []
    for item in rows[-12:]:
        body.append(
            "<tr>"
            f"<td>{_shown(item.get('ts'), '—')}</td>"
            f"<td class=\"num\">{_shown(item.get('equity_usd'), '—')}</td>"
            f"<td class=\"num\">{_shown(item.get('cash_usd'), '—')}</td>"
            f"<td class=\"num {_pnl_class(item.get('realized_usd'))}\">{_shown(item.get('realized_usd'), '—')}</td>"
            f"<td class=\"num {_pnl_class(item.get('unrealized_usd'))}\">{_shown(item.get('unrealized_usd'), '—')}</td>"
            "</tr>"
        )
    return "".join(body)


_CONN = {
    "disconnected": "未连接",
    "connecting": "连接中",
    "connected": "已连接",
    "stopping": "正在停止",
    "disabled": "未启用",
    "not_started": "未启动",
}
_SUB = {
    "none": "尚未订阅",
    "pending": "订阅中",
    "subscribed": "已订阅",
    "error": "订阅失败",
}
_BEAT = {
    "idle": "尚未心跳",
    "waiting": "等待 pong",
    "ok": "心跳正常",
    "late": "心跳超时",
}
_CLOCK = {
    "uncalibrated": "未校准",
    "ok": "已对时",
    "within_noise": "偏差落在往返不确定度内",
    "shifted": "时钟偏差过大",
    "failed": "对时失败",
}
_SOURCE = {
    "bitget_public_ws_v3": "公开 WebSocket",
    "bitget_public_rest_v3": "公开 REST",
    "none": "没有可用行情",
}


def _ms_text(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        return "未采集"
    return str(value)


def _monitor(snapshot: dict[str, Any]) -> str:
    market = snapshot.get("market") if isinstance(snapshot.get("market"), dict) else {}
    if not market:
        return '<section class="card panel" id="live"><h2>实时行情</h2><p class="empty">未采集</p></section>'
    served = snapshot.get("served_at_ms")
    received = market.get("received_at_ms")
    cache_age = "未采集"
    if isinstance(served, int) and isinstance(received, int) and not isinstance(served, bool):
        cache_age = str(served - received)
    samples = market.get("interval_samples")
    sample_line = "样本不足，不计算分位数。"
    if isinstance(samples, int) and samples > 0:
        sample_line = (
            f"相邻推送间隔样本 {samples} / 窗口 {_esc(market.get('interval_window'))}。"
            f"最近 {_ms_text(market.get('interval_last_ms'))} ms，"
            f"最小 {_ms_text(market.get('interval_min_ms'))}，最大 {_ms_text(market.get('interval_max_ms'))}。"
            "这是两次有效快照的服务端时间差，不是延迟保证，也不是 p95。"
        )
    fresh = "尚无有效快照"
    max_age = market.get("max_quote_age_seconds")
    if cache_age != "未采集" and isinstance(max_age, int):
        fresh = "未超过配置的行情年龄" if int(cache_age) <= max_age * 1000 else "已超过配置的行情年龄，不能当作成交价"
    arrival = market.get("arrival_minus_server_ms")
    arrival_text = _ms_text(arrival) if arrival is not None else "未采集"
    return f"""<section class="card panel" id="live">
      <h2>实时行情与延迟</h2>
      <dl class="kv">
        <dt>连接</dt><dd>{_labeled(_CONN, market.get("connection"))}</dd>
        <dt>订阅</dt><dd>{_labeled(_SUB, market.get("subscription"))}</dd>
        <dt>心跳</dt><dd>{_labeled(_BEAT, market.get("heartbeat"))}</dd>
        <dt>决策所用</dt><dd>{_labeled(_SOURCE, market.get("trade_source"))}</dd>
        <dt>回退原因</dt><dd>{_shown(market.get("trade_note"))}</dd>
        <dt>最新价</dt><dd>{_shown(market.get("last_price"))}</dd>
        <dt>买一 / 卖一</dt><dd>{_shown(market.get("bid"))} / {_shown(market.get("ask"))}</dd>
        <dt>服务端消息时间</dt><dd>{_ms_text(market.get("server_ts_ms"))}</dd>
        <dt>本机接收时间</dt><dd>{_ms_text(market.get("received_at_ms"))}</dd>
        <dt>缓存年龄</dt><dd>{_esc(cache_age)} ms</dd>
        <dt>接收减服务端</dt><dd>{_esc(arrival_text)} ms</dd>
        <dt>有效消息</dt><dd>{_ms_text(market.get("accepted"))}</dd>
        <dt>拒绝 / 乱序 / 未来</dt><dd>{_ms_text(market.get("rejected"))} / {_ms_text(market.get("disorder"))} / {_ms_text(market.get("future"))}</dd>
        <dt>重连</dt><dd>{_ms_text(market.get("reconnects"))}</dd>
        <dt>时钟</dt><dd>{_labeled(_CLOCK, market.get("clock_status"))}</dd>
        <dt>时钟偏差估计</dt><dd>{_ms_text(market.get("clock_offset_ms"))} ms，往返 {_ms_text(market.get("clock_rtt_ms"))}，不确定度 ±{_ms_text(market.get("clock_uncertainty_ms"))}</dd>
        <dt>新鲜度</dt><dd>{_esc(fresh)}</dd>
      </dl>
      <p class="hint">{sample_line}</p>
      <p class="hint">接收减服务端消息时间包含时钟差、网络和排队，不是单程网络延迟。负值会原样显示。心跳正常只说明 ping/pong 还在，不代表行情仍然新鲜。页面约 5 秒刷新一次；推送只在行情变化时发生，官方写明大约 200ms，那不是延迟保证。买卖仍按已收盘 1 分钟 K 线和配置的轮询间隔，不会每个跳动都成交。</p>
      <p class="hint">{_esc(market.get("subscribe_error") or "")}</p>
    </section>"""


def _page(snapshot: dict[str, Any]) -> str:
    feed = snapshot.get("feed") if isinstance(snapshot.get("feed"), dict) else {}
    book = snapshot.get("book") if isinstance(snapshot.get("book"), dict) else {}
    strategy = snapshot.get("strategy") if isinstance(snapshot.get("strategy"), dict) else {}
    risk = snapshot.get("risk") if isinstance(snapshot.get("risk"), dict) else {}
    last = snapshot.get("last_decision") if isinstance(snapshot.get("last_decision"), dict) else {}
    decisions = _rows(snapshot.get("recent_decisions"), 20)
    equity = _rows(snapshot.get("equity"), 120)
    stale = bool(book.get("stale"))
    price_stale = bool(feed.get("price_stale"))
    feed_status = str(feed.get("status") or "")
    book_note = "账本沿用上一笔，这轮行情没有刷新。" if stale else "账本与这一轮摘要一致。"
    if not book:
        book_note = "还没有账本数字。"
    price_note = "沿用上一次价格，当前行情不可用。" if price_stale else ""
    flatten = risk.get("emergency_flatten")
    if flatten is True:
        flatten_text = "是 · true。紧急停止会用合格行情做一次本地卖出。"
    elif flatten is False:
        flatten_text = "否 · false。紧急停止默认不平仓。"
    else:
        flatten_text = "未采集"
    unrealized_class = _pnl_class(book.get("unrealized_usd"))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>钱多多 · BTCUSDT</title>
<style>{_CSS}</style>
{_THEME_JS}
</head>
<body>
<header class="header">
  <div class="container header-inner">
    <a class="logo" href="#overview">钱多多</a>
    <nav class="nav">
      <a class="nav-link" href="#overview">总览</a>
      <a class="nav-link" href="#market">行情</a>
      <a class="nav-link" href="#book">账本</a>
      <a class="nav-link" href="#strategy">策略</a>
      <a class="nav-link" href="#decisions">决策</a>
      <a class="nav-link" href="#equity">权益</a>
    </nav>
    <button class="theme-btn" id="theme-toggle" type="button" title="切换暗黑">🌙</button>
  </div>
</header>
<main class="container">
  <div class="paper">PAPER ONLY · 本地纸面 · 不是交易所订单 · 不代表收益</div>
  <h1 class="page-title" id="overview">钱多多 · BTCUSDT 现货</h1>
  <p class="page-sub">只读本机模拟账本和 Bitget 公开行情。红涨绿跌沿用量化 2.0 的颜色。页面每 5 秒刷新，没有下单按钮。</p>
  <section class="metrics" id="book">
    <div class="card tcard">
      <div class="tlabel">权益 USD</div>
      <div class="tvalue">{_shown(book.get("equity_usd"))}</div>
      <div class="tband">{_esc(book_note)}</div>
      <div class="tsub">截至 {_shown(book.get("as_of"))}</div>
    </div>
    <div class="card tcard">
      <div class="tlabel">现金 USD</div>
      <div class="tvalue">{_shown(book.get("cash_usd"))}</div>
      <div class="tsub">持仓 {_shown(book.get("quantity_btc"))} BTC</div>
    </div>
    <div class="card tcard">
      <div class="tlabel">未实现 USD</div>
      <div class="tvalue {unrealized_class}">{_shown(book.get("unrealized_usd"))}</div>
      <div class="tsub">已实现 <span class="{_pnl_class(book.get("realized_usd"))}">{_shown(book.get("realized_usd"))}</span></div>
    </div>
    <div class="card tcard">
      <div class="tlabel">运行</div>
      <div class="tvalue" style="font-size:22px">{_labeled(_RUNTIME, snapshot.get("runtime"))}</div>
      <div class="tsub">最近决策 {_badge(last.get("action"))}</div>
    </div>
  </section>
  <div class="split">
    <section class="card panel" id="market">
      <h2>行情</h2>
      <dl class="kv">
        <dt>品种</dt><dd>BTCUSDT · SPOT</dd>
        <dt>状态</dt><dd>{_labeled(_FEED, feed_status)}{_stale_badge(feed_status, price_stale)}</dd>
        <dt>最新价</dt><dd>{_shown(feed.get("last_price"))}</dd>
        <dt>标记价</dt><dd>{_shown(feed.get("mark_price"))}</dd>
        <dt>年龄</dt><dd>{_shown(feed.get("ticker_age_ms"), "未采集")} ms</dd>
        <dt>原因</dt><dd title="{_esc(feed.get("reason", ""))}">{_shown(feed.get("reason"))}</dd>
        <dt>时间</dt><dd>{_shown(feed.get("as_of"))}</dd>
      </dl>
      <p class="hint">{_esc(price_note)}</p>
    </section>
    <section class="card panel" id="strategy">
      <h2>策略与风控</h2>
      <dl class="kv">
        <dt>策略</dt><dd>{_shown(strategy.get("id"))}</dd>
        <dt>周期</dt><dd>快 {_shown(strategy.get("fast_period"))} / 慢 {_shown(strategy.get("slow_period"))} · {_shown(strategy.get("interval"))}</dd>
        <dt>快均线</dt><dd>{_shown(strategy.get("fast_sma"))}</dd>
        <dt>慢均线</dt><dd>{_shown(strategy.get("slow_sma"))}</dd>
        <dt>最大仓位</dt><dd>{_shown(risk.get("max_position_usd"))} USD</dd>
        <dt>亏损限额</dt><dd>{_shown(risk.get("max_loss_usd"))} USD</dd>
        <dt>回撤上限</dt><dd>{_shown(risk.get("max_drawdown_pct"))}</dd>
        <dt>费用假设</dt><dd>{_shown(risk.get("fee_bps"))} / {_shown(risk.get("slippage_bps"))} bps</dd>
        <dt>紧急平仓</dt><dd>{flatten_text}</dd>
      </dl>
      <p class="hint">紧急停止默认不平仓。只有配置 emergency_flatten: true 时，紧急停止才会用合格行情做一次本地卖出。</p>
    </section>
  </div>
  {_monitor(snapshot)}
  <section class="card chart-card" id="equity">
    <div class="chart-head">权益 · 本机记账</div>
    {_equity_svg(equity)}
    <div class="table-wrap">
      <table>
        <thead><tr><th>时间</th><th class="num">权益</th><th class="num">现金</th><th class="num">已实现</th><th class="num">未实现</th></tr></thead>
        <tbody>{_equity_table(equity)}</tbody>
      </table>
    </div>
  </section>
  <section class="card table-wrap" id="decisions">
    <table>
      <thead><tr><th>时间</th><th>动作</th><th>原因</th><th>关联号</th><th>成交</th></tr></thead>
      <tbody>{_decision_table(decisions)}</tbody>
    </table>
  </section>
  <section class="card note">
    <strong>未接入。</strong>
    量化 2.0 的币安合约信号、A 股、期货、指数、资讯、GBM、AI 信号和 K 线没有接到这本账。这里不显示那些页面，也不放不能用的按钮。
  </section>
</main>
<footer class="footer">
  <div class="container">
    <p class="disc">免责声明：钱多多只展示本机纸面账本和公开行情，不构成任何投资建议，也不代表收益。模拟成交不是交易所订单。行情可能延迟或被拒绝。红涨绿跌只是颜色习惯。</p>
    <p class="copy">钱多多 · 本地纸面 · 127.0.0.1</p>
  </div>
</footer>
</body>
</html>
"""


def _stale_badge(status: str, price_stale: bool) -> str:
    if status == "rejected" or price_stale:
        return ' <span class="badge warn">行情未刷新</span>'
    return ""


def _not_found() -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>钱多多 · 没有这个路径</title>
<style>{_CSS}</style>
</head>
<body>
<main class="container">
  <h1 class="page-title">没有这个路径</h1>
  <p class="page-sub">状态页只提供 / 和 /health。这一页不是账本，也没有隐藏数据。</p>
  <p><a href="/">返回钱多多</a></p>
</main>
</body>
</html>
"""


class StatusServer:
    def __init__(self, host: str, port: int, snapshot_fn: SnapshotFn):
        if host != "127.0.0.1":
            raise ValueError("status_host")
        self.host = host
        self.port = port
        self.snapshot_fn = snapshot_fn
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def bound_port(self) -> int:
        if self._httpd is None:
            return self.port
        return int(self._httpd.server_address[1])

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path in {"/health", "/api/status"}:
                    snapshot = server.snapshot_fn()
                    body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
                    self._send(200, "application/json; charset=utf-8", body)
                    return
                if path in {"/", "/index.html"}:
                    snapshot = server.snapshot_fn()
                    body = _page(snapshot).encode("utf-8")
                    self._send(200, "text/html; charset=utf-8", body)
                    return
                self._send(404, "text/html; charset=utf-8", _not_found().encode("utf-8"))

            def _send(self, status: int, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="bitget-paper-status", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
