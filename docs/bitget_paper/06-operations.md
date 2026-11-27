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

账本比 JSONL 先落盘。如果进程在两者之间停掉，持仓以账本为准，日志里可能少一行成交。下次启动会追加 `bitget_paper_audit_gap`，里面没有被补造的成交明细。不要把 JSONL 当成和账本同一事务的完整凭证。

## 只读看一段公开行情

不写账本、不模拟成交。时间限制在 10 到 120 秒。把配置换成你自己的绝对路径，不要用示例配置覆盖它：

```bash
python -m src.bitget_paper --config /Users/ming/Documents/qianduoduo-paper-run/runtime/config.yaml --ws-diagnose --seconds 90
```

这只读，不写账本。输出里的 `fill_written` 应为 false。`arrival_minus_server_ms` 不是单程延迟。当前环境如果没有 `websockets`，这条命令会直接失败并提示安装，或者把 `public_ws_enabled` 设为 false 继续只用 REST。它不会在后台反复重连。

## 更新已经在跑的目录

先停掉正在跑的进程。不要用 `config.example.yaml` 覆盖正在用的配置，回滚代码时也不要回滚账本。

正在用的配置在本机是 `/Users/ming/Documents/qianduoduo-paper-run/runtime/config.yaml`。账本路径以这份文件里的字段为准，不要猜成仓库根目录或 `runtime/logs` 以外的位置。相对路径按启动进程时的工作目录解析，不是按 yaml 文件所在目录。

```bash
python - << 'PY'
from pathlib import Path
import yaml
cfg = Path("/Users/ming/Documents/qianduoduo-paper-run/runtime/config.yaml")
section = yaml.safe_load(cfg.read_text(encoding="utf-8"))["bitget_paper"]
for key in ("state_path", "log_path", "equity_path", "control_path"):
    print(key, section.get(key))
PY
```

对打印出来的每一个路径，若是相对路径，就拼上当时启动时的工作目录，再复制成 `.bak`。不要覆盖这些原文件。确认旧进程已经退出后，用同一份 yaml 再启动：

```bash
python -m src.bitget_paper --config /Users/ming/Documents/qianduoduo-paper-run/runtime/config.yaml --run
```

旧配置没有 `public_ws_enabled` 时默认会连公开行情，但不会改写现金、风险限额、策略和紧急平仓开关。若要维持纯 REST，只在原来那份 yaml 里加上 `public_ws_enabled: false`。启动前这个环境必须能 `import websockets`，否则进程会明确失败，而不是带着断线错误继续跑。

浏览器重新打开 `http://127.0.0.1:8765/`。旧进程在退出前仍是旧页面。页面上的时间按 Asia/Shanghai 显示。

状态页的 CSS 在 `status_server.py` 里，没有单独的静态资源，也不需要 npm 或前端构建。固定提交的源码包是 GitHub 的 codeload archive，解压后同样直接运行，不覆盖上述运行目录。

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
