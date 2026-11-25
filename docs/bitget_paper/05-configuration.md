# 配置

只读 `config.yaml` 里的 `bitget_paper`。示例在 `config.example.yaml`。这一节出现密钥字段会拒绝启动。

| 字段 | 默认 | 含义 |
|---|---|---|
| `execution` | `simulate` | 唯一合法值 |
| `strategy` | `baseline_sma_cross_v1` | 另可选 `deterministic_demo_flat_once_v1` |
| `fast_period` / `slow_period` | 5 / 20 | 已收盘 K 线根数，快的必须更短 |
| `candle_interval` | `1m` | 目前只接受这一档 |
| `demo_notional_usd` | 25.00 | 每次买入的名义本金 |
| `demo_cash_usd` | 100.00 | 新账本的演示现金 |
| `fee_bps` / `slippage_bps` | 10 / 5 | 假设，不是交易所费率 |
| `max_position_usd` | 100.00 | 敞口上限，超过拒单 |
| `max_loss_usd` | 100.00 | 已实现加未实现亏损上限 |
| `max_drawdown_pct` | 0.25 | 相对峰值权益的回撤，超过拒绝新买单 |
| `poll_seconds` | 20 | 轮询间隔，允许 5 到 300 |
| `max_cycle_retries` | 2 | 只重试传输失败和 429/5xx |
| `timeout_seconds` | 10 | 单次 HTTP 超时 |
| `max_quote_age_seconds` | 15 | ticker 最大年龄，单位秒。WebSocket 和 REST 都用它 |
| `clock_skew_seconds` | 2 | 允许的未来时间，单位秒。超过对时不确定度后再超出这个值，会停止这一轮买卖 |
| `public_ws_enabled` | true | 旧文件没写这项也是 true。false 才关闭实时流。不改其它风险和策略字段 |
| `ws_ping_seconds` | 30 | 文本 ping 间隔，单位秒，必须大于 pong 超时 |
| `ws_pong_timeout_seconds` | 10 | 发出 ping 后多久没收到 pong 就重连，单位秒 |
| `ws_reconnect_max_seconds` | 30 | 重连退避上限，单位秒，实际等待会再乘一个 0.5 到 1 的抖动 |
| `clock_sync_seconds` | 60 | 多久向 `/api/v3/market/time` 对一次时，单位秒 |
| `emergency_flatten` | false | 紧急停止是否平仓 |
| `status_host` / `status_port` | 127.0.0.1 / 8765 | 状态页 |
| `state_path` | `logs/bitget_paper_state.json` | 账本 |
| `log_path` | `logs/bitget_paper.jsonl` | 审计日志 |
| `equity_path` | `logs/bitget_paper_equity.jsonl` | 权益追加 |
| `control_path` | `logs/bitget_paper_control.json` | 暂停和紧急停止 |
| `status_path` | `logs/bitget_paper_status.json` | 最近一次快照 |

父配置里的 `mode: live` 不会打开 Bitget 下单，也不会被这个入口调用。
