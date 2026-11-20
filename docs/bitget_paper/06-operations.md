# 操作、备份和排错

## 干净环境

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python -m src.bitget_paper --config config.yaml --smoke
```

`--smoke` 成功时输出里有 `"authenticated": false` 和 `"symbol": "BTCUSDT"`。它不建仓。

## 运行和检查

```bash
python -m src.bitget_paper --config config.yaml --run
```

另开一个终端：

```bash
python -m src.bitget_paper --config config.yaml --status
curl -s http://127.0.0.1:8765/health
```

停止：在运行的终端按 Ctrl-C，或对进程发 SIGTERM。

有限轮次，便于复查：

```bash
python -m src.bitget_paper --config config.yaml --run --max-cycles 3
```

## 备份和恢复

停掉进程后再复制，避免抄到写了一半的文件。

```bash
cp logs/bitget_paper_state.json logs/bitget_paper_state.json.bak
cp logs/bitget_paper.jsonl logs/bitget_paper.jsonl.bak
cp logs/bitget_paper_equity.jsonl logs/bitget_paper_equity.jsonl.bak
```

恢复就是把这三份拷回去。控制文件可以删掉，下次按运行中处理。不要同时开两个进程用同一本账。

## 回退

代码回退用 git。账本不会跟着 git 走，`logs/` 已被忽略。回退代码之后，如果账本版本比程序新，程序会拒绝读取，原文件保持不动。版本 1 的旧账本可以被当前程序读入并写成版本 2。

## 排错

| 现象 | 含义 |
|---|---|
| 退出码 2 | 配置不合法，没有下单路径被调用 |
| 退出码 3 | 这一轮行情不可用，持仓不变 |
| 退出码 4 | 账本损坏、版本不认识，或控制文件损坏 |
| 退出码 5 | 已经有进程占着这本账 |
| 状态页打不开 | 进程没在跑，或端口不是 8765。先看 `--status` |
| 一直持有不成交 | 均线没有给出反向信号，或行情被拒绝。看日志里的 `reason` |

日志里的 `not_exchange_order: true` 表示这是本地成交。不要把测试夹具或合成 K 线里的成交当成当时的市场成交。
