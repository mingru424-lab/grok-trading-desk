"""Read-only status page bound to 127.0.0.1.

The page shows the snapshot the paper process last published. It does not
place orders, and it does not accept a host other than loopback.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

SnapshotFn = Callable[[], dict[str, Any]]


def _page(snapshot: dict[str, Any]) -> str:
    banner = "PAPER ONLY · 本地纸面 · 不是交易所订单 · 不代表收益"
    runtime = str(snapshot.get("runtime", "unknown"))
    feed = snapshot.get("feed") or {}
    book = snapshot.get("book") or {}
    strategy = snapshot.get("strategy") or {}
    risk = snapshot.get("risk") or {}
    last = snapshot.get("last_decision") or {}
    rows = snapshot.get("recent_decisions") or []
    equity = snapshot.get("equity") or []
    decision_rows = "".join(
        "<tr><td>{ts}</td><td>{action}</td><td>{reason}</td><td>{cid}</td></tr>".format(
            ts=_esc(item.get("ts", "")),
            action=_esc(item.get("action", "")),
            reason=_esc(item.get("reason", "")),
            cid=_esc(item.get("correlation_id", "")),
        )
        for item in rows[-12:]
    )
    equity_rows = "".join(
        "<tr><td>{ts}</td><td>{equity}</td><td>{cash}</td><td>{realized}</td><td>{unrealized}</td></tr>".format(
            ts=_esc(item.get("ts", "")),
            equity=_esc(item.get("equity_usd", "")),
            cash=_esc(item.get("cash_usd", "")),
            realized=_esc(item.get("realized_usd", "")),
            unrealized=_esc(item.get("unrealized_usd", "")),
        )
        for item in equity[-12:]
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Bitget paper · BTCUSDT</title>
<style>
body {{ font-family: ui-sans-serif, sans-serif; margin: 24px; background: #f6f4ef; color: #1c1915; }}
.banner {{ background: #8a3b12; color: white; padding: 12px 16px; font-weight: 700; letter-spacing: 0.02em; }}
section {{ background: white; margin-top: 16px; padding: 16px; border: 1px solid #e4ded2; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ border-bottom: 1px solid #eee; text-align: left; padding: 6px; font-size: 14px; }}
code {{ background: #f3efe6; padding: 0 4px; }}
</style>
</head>
<body>
<div class="banner">{banner}</div>
<section>
<p>运行状态 <code>{_esc(runtime)}</code> · 行情 <code>{_esc(feed.get("status", ""))}</code>
{_esc(feed.get("reason", ""))} · 年龄 {_esc(feed.get("ticker_age_ms", ""))} ms</p>
<p>策略 <code>{_esc(strategy.get("id", ""))}</code>
快均线 {_esc(strategy.get("fast_period", ""))} / 慢均线 {_esc(strategy.get("slow_period", ""))}
· {_esc(strategy.get("interval", ""))}</p>
<p>风控 最大仓位 {_esc(risk.get("max_position_usd", ""))} USD
· 亏损限额 {_esc(risk.get("max_loss_usd", ""))} USD
· 回撤上限 {_esc(risk.get("max_drawdown_pct", ""))}
· 紧急停止平仓 {_esc(risk.get("emergency_flatten", ""))}</p>
<p>紧急停止默认不平仓。只有配置 <code>emergency_flatten: true</code> 时，紧急停止才会用合格行情做一次本地卖出。</p>
</section>
<section>
<h2>账本</h2>
<p>现金 {_esc(book.get("cash_usd", ""))} · 持仓 {_esc(book.get("quantity_btc", ""))} BTC
· 已实现 {_esc(book.get("realized_usd", ""))} · 未实现 {_esc(book.get("unrealized_usd", ""))}
· 权益 {_esc(book.get("equity_usd", ""))}</p>
<p>最近决策 {_esc(last.get("action", ""))} / {_esc(last.get("reason", ""))}
· <code>{_esc(last.get("correlation_id", ""))}</code></p>
</section>
<section>
<h2>最近决策</h2>
<table><tr><th>时间</th><th>动作</th><th>原因</th><th>关联号</th></tr>{decision_rows}</table>
</section>
<section>
<h2>权益</h2>
<table><tr><th>时间</th><th>权益</th><th>现金</th><th>已实现</th><th>未实现</th></tr>{equity_rows}</table>
</section>
</body>
</html>
"""


def _esc(value: Any) -> str:
    text = str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


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
                snapshot = server.snapshot_fn()
                if path in {"/health", "/api/status"}:
                    body = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path in {"/", "/index.html"}:
                    body = _page(snapshot).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

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
