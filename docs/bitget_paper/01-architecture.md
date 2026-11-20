# 模块

```text
cli.py            命令行：smoke / once / run / control / status
loop.py           轮询、停止、把快照交给状态页
engine.py         一轮：行情 → 策略 → 风控 → 模拟成交 → 记账
market.py         无认证 GET：instruments、tickers、candles
strategy.py       baseline_sma_cross_v1，以及可选的旧一次性规则
risk.py           仓位和亏损门闩，复用 shared/risk.py 的亏损判断
broker.py         本地买卖。买在更差的卖一，卖在更差的买一
store.py          版本、迁移、文件锁、控制文件、权益追加
status_server.py  127.0.0.1 上的只读 HTML 和 /health
models.py         严格决策契约和账本
config.py         只接受 simulate，拒绝密钥字段
```

原来的 `src/desk.py` 不导入这个包。这个包也不导入 Alpaca 或 Solana 执行器。

一轮的顺序：

1. 取得账本锁。第二个进程退出码 5。
2. 读合约和 ticker。不合格就记一笔拒绝，不改持仓。
3. 基线策略再读 1 分钟 K 线，只用已经走完的柱。
4. 策略给出 BUY、SELL 或 HOLD，并带关联号。
5. 控制文件可以拦住买单或全部新订单。
6. 风控检查仓位、亏损和回撤。
7. 模拟成交先写入账本，再追加 JSONL。同一根已收盘 K 线最多一笔模拟成交，不论买或卖。紧急平仓是唯一的同根例外。账本和 JSONL 不是同一个事务。

账本文件是持仓的依据。JSONL 是审计。状态页读的是进程刚写下的快照。
