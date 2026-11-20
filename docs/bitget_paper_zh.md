# Bitget BTCUSDT 本地纸面

这是独立入口，不启动原来的 `python -m src.desk`。它只读 Bitget 公开现货行情，在本机模拟成交。没有密钥，没有真实订单，也不是 Bitget 官方模拟盘。

维护记录在 [docs/bitget_paper/](bitget_paper/00-scope.md)。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

不要在 `bitget_paper` 里填写密钥。`execution` 只能是 `simulate`。

## 启动和停止

```bash
python -m src.bitget_paper --config config.yaml --smoke
python -m src.bitget_paper --config config.yaml --once
python -m src.bitget_paper --config config.yaml --run
```

`--run` 按 `poll_seconds` 轮询，直到 Ctrl-C 或 SIGTERM。当前这一轮如果已经开始，会做完再停，不会再开新的一笔。状态页默认只听 `127.0.0.1:8765`。

```bash
python -m src.bitget_paper --config config.yaml --control pause
python -m src.bitget_paper --config config.yaml --control resume
python -m src.bitget_paper --config config.yaml --control emergency-stop
python -m src.bitget_paper --config config.yaml --control clear-emergency
python -m src.bitget_paper --config config.yaml --status
```

`pause` 拒绝新的买单，已有持仓可以按策略卖出。`emergency-stop` 买卖都不做。只有 `emergency_flatten: true` 时，紧急停止才会用合格行情把持仓本地卖平。默认不平仓。

退出码：0 正常，2 配置拒绝，3 行情不可用且没有新成交，4 账本或状态文件有问题，5 已经有一个进程占着同一本账。

## 验收命令

```bash
python3 -m pytest -q -m "not network"
BITGET_PAPER_NETWORK=1 python3 -m pytest -q -m network tests/test_bitget_paper.py
```

夹具 `tests/fixtures/bitget_btcusdt_public_snapshot.json` 标明 `realtime: false`。测试里的 K 线序列也是合成的，不是当时的行情。
