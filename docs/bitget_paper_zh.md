# Bitget BTCUSDT 现货：公开行情与本地纸面交易

这是一条**演示管线**。它用 Bitget 的公开行情接口读取 `BTCUSDT` 现货，再在本机账本里模拟一笔成交。它不连接你的 Bitget 账户，不读取交易密钥，也不向交易所下单。

默认策略 `deterministic_demo_flat_once_v1` 只做一件事：账本是空的、而且之前没有成交时，买入配置里的演示名义本金一次；否则持有。价格不参与决策。这不是预测，也不代表任何预期收益。成交后按更差的买一价估值，手续费和滑点都算进去，所以刚成交时未实现盈亏通常是负的。

没有 LLM 密钥也可以跑。这条路径不调用模型，**不是**完整的多 Agent 验证。

## 这不是什么

- 不是 Bitget 实盘，也不是 Bitget 的模拟盘。官方模拟盘要 Demo API Key，并在请求头里带 `paptrading: 1`。本程序不实现这条路径。
- 不是股票代币（rToken）交易。`isReality` 不是 `no` 的品种会直接拒绝。
- 不会启动原来的 `python -m src.desk`。桌面的 Alpaca / Solana 执行器也不会被这个入口调用。
- `config.yaml` 里的 `mode: live` 只影响原来的桌面。它不会把这里变成实盘，这里也没有实盘分支。

`execution` 只接受 `simulate`。写成别的值、缺了 `bitget_paper`、或在这一节里出现密钥字段，进程直接退出，不会改去跑桌面。

## 依赖

Python 3.11+。与仓库其余测试相同：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

纸面路径实际用到的是 `httpx`、`pydantic`、`pyyaml`。`pytest` 用来跑下面的验收。不需要安装 Bitget SDK，也不需要 `BITGET_API_KEY` 之类的环境变量。

## 配置

```bash
cp config.example.yaml config.yaml
```

不要在 `bitget_paper` 里填写任何密钥、口令或签名。示例里的数字都是演示参数：

| 字段 | 示例 | 含义 |
|---|---|---|
| `execution` | `simulate` | 唯一合法值 |
| `symbol` / `category` | `BTCUSDT` / `SPOT` | 本轮只做这一对 |
| `demo_notional_usd` | `25.00` | 空仓时模拟买入的名义本金 |
| `demo_cash_usd` | `100.00` | 本地账本里的演示现金，不是真实资金 |
| `fee_bps` | `10` | 手续费假设（10 个基点）。不是 Bitget 费率表 |
| `slippage_bps` | `5` | 滑点假设，买在更差的卖一、卖在更差的买一 |
| `max_position_usd` | `100.00` | 最大敞口，超过就拒单 |
| `max_loss_usd` | `100.00` | 亏损限额，超过就拒单 |
| `max_quote_age_seconds` | `15` | 行情超过这个年龄就拒绝，并且不成交 |

风控按「名义本金 + 假设手续费 + 0.01 美元缓冲」估计新增敞口。盈利不会把亏损限额抬高。限额用的是本地账本里的已实现加未实现盈亏，**不会因为过了一天就自动恢复**。拒单只拒绝，不会把单缩小后再成交。

## 启动

只看公开行情，不建仓：

```bash
python -m src.bitget_paper --config config.yaml --smoke
```

跑一轮演示决策。空账本且行情合格时，会写一笔本地模拟成交：

```bash
python -m src.bitget_paper --config config.yaml --once
```

成功时标准输出是一行 JSON，其中 `authenticated` 为 `false`，`exchange_order` 为 `false`，`strategy_expects_profit` 为 `false`，`observation_not_a_signal` 为 `true`。`--smoke` 不会创建 `state_path` 里的持仓文件。

退出码：

| 码 | 含义 |
|---|---|
| 0 | 行情可用。可能成交、持有，或被风控拒单 |
| 2 | 配置不合法，没有启动 |
| 3 | 行情断开、过期或异常，没有新的模拟成交 |
| 4 | 本地账本损坏或无法写入，没有用坏账本继续交易 |
| 1 | 没有预料到的错误 |

账本在 `state_path`（默认 `logs/bitget_paper_state.json`）。审计日志在 `log_path`（默认 `logs/bitget_paper.jsonl`），只追加，不改旧行。

## 日志里有什么

每一行都有本地时间 `ts`（UTC）。和成交相关的行还会带上：

- 来源：交易所、未认证的公开 REST、两个路径、请求时间和行情 `ts`
- 决策：`DETERMINISTIC_DEMO`，以及 `price_used_as_signal: false`
- 假设：`fee_bps`、`slippage_bps`，并写明它们不是交易所费率、也不是真实成交
- 仓位和盈亏：数量、成本、现金、已实现、未实现

行情不合格时写 `bitget_paper_quote_rejected`，`new_fill` 为 `false`，账本文件保持原样。

## 验收

下面的测试默认不访问网络。夹具文件 `tests/fixtures/bitget_btcusdt_public_snapshot.json` 标明了 `realtime: false`，不能当作当前价格。

```bash
pytest -v
```

公开行情冒烟（需要能访问 `api.bitget.com`，仍然不带密钥）：

```bash
BITGET_PAPER_NETWORK=1 pytest -v -m network tests/test_bitget_paper.py
python -m src.bitget_paper --config config.yaml --smoke
```

`--smoke` 成功时，输出里的 `status` 是 `smoke_ok`，`symbol` 是 `BTCUSDT`，`authenticated` 是 `false`。这只说明当时拿到了一份合格的公开行情。

再跑一次 `--once` 时，若账本里已经有持仓，决策是持有，不会再加一笔。

## 限制与未验证

- 没有调用下单、撤单、划转、资产或私有 WebSocket。这些接口需要签名，本程序里没有对应代码。
- 没有验证 Bitget 模拟盘，也没有验证 rToken 下单。
- 现货公开合约里没有手续费字段。10 bps 和 5 bps 是写死的演示假设。
- 冒烟只证明当时一次公开 GET 能通过校验，不是持续行情，也不是成交质量。
- 本地时钟和交易所差超过 `clock_skew_seconds`（默认 2 秒）时，行情会被拒绝。
- 演示策略不会自动卖出。已实现盈亏的计算公式有单元测试，默认运行路径只记录未实现盈亏。
- 持仓超过限额时只拒绝新的买单，不会自动减仓。
- 原来的十二个机器人、Alpaca 执行器和 Solana 占位执行器没有改行为。`python -m src.desk` 不会去读 Bitget。
