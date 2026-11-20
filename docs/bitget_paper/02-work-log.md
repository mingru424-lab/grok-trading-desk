# 工作记录

时间用 Asia/Shanghai。只追加，不改旧行。

## 2026-11-19

- 第一版：BTCUSDT 公开行情，本地模拟，默认策略是空仓买一次然后持有。
- 提交 `d314f3d`，PR https://github.com/mingru424-lab/grok-trading-desk/pull/1
- 当时 PR 没有评审，仓库也没有 CI 检查。

## 2026-11-20

- 默认策略改为已收盘 1 分钟 K 线的快慢均线，可以买、持有和平仓。
- 加上轮询、停止、有限重试、暂停、紧急停止、回撤限额、账本版本 2、单写者锁和本机状态页。
- 旧的一次性规则仍可在配置里选，但不再是默认值。
- 维护说明放到 `docs/bitget_paper/`。
- 验证命令和结果写在同日的 PR 说明里。这一轮开始时的基线是 `d314f3d`。
- 离线测试 `python3 -m pytest -q -m "not network"`：291 passed, 2 deselected。
- 公开行情 `BITGET_PAPER_NETWORK=1 python3 -m pytest -q -m network tests/test_bitget_paper.py`：2 passed, 49 deselected。
- 开发机 `--run --max-cycles 3` 退出码 0。随后同一本账 `--once` 在均线转向后本地平仓。样本见 `examples/runtime-2026-11-20.json`。
- 状态页在 `127.0.0.1:8765` 返回 `PAPER ONLY`，并用本机 Chrome 无头模式截过运行中的页面。
- 上述产品改动在提交 `69c2dd0`。

## 2026-11-20 审查

对照当时的 HEAD `53a2221` 查了五条意见。验证数字记在下一条，不写进产生这些数字的提交里。

- `fills_count` 不存在。`PaperState.fill_count` 是账本字段。broker、engine、decision、strategy 读的都是 `fill_count`。审查点名的行号也是 `fill_count`。这不能解释 `--once` / `--run` 会 AttributeError。291 个离线测试和当时的公开行情运行与这个结论一致。
- 行情拒绝的摘要里没有现金和持仓。轮询把缺失字段写成空字符串，状态页上的账本会被擦掉。这是真问题，已改：拒绝时带上上一本账，并标 `book_stale` 和时间。
- 仓库当时没有 GitHub Actions。已加 `.github/workflows/tests.yml`，只跑离线测试。文件进仓库不等于那次检查已经成功。
- 重复保护原来只挡同一根 K 线的同一个方向。现改为一根已收盘 K 线最多一笔模拟成交。紧急平仓除外。
- 账本先于 JSONL 落盘，不是事务。缺口用 `bitget_paper_audit_gap` 承认，不补造成交明细。

## 2026-11-20 16:59 CST 验证

验证的代码是 `6d57ea6d4414d1d5a706e294823d8e7ca9eca653`。当时 `git status --short` 为空。Python 3.12.3。命令 `python3 -m pytest -q -m "not network"`，UTC 2026-11-20 08:59:19，上海时间 16:59:19。结果 295 passed, 2 deselected，退出码 0。原文在 `examples/pytest-offline-6d57ea6.txt`。

同一提交的 GitHub Actions：

- push 成功：https://github.com/mingru424-lab/grok-trading-desk/actions/runs/44706952020
- pull_request 成功：https://github.com/mingru424-lab/grok-trading-desk/actions/runs/44706952122

这两次都跑在 `6d57ea6`。本条记录本身是之后的文档提交，会再触发一次检查；写下这条时还不能把那次新运行说成已经成功。

公开行情没有被要求成交。`--smoke` 退出码 0，`authenticated` 为 false。`--run --max-cycles 2` 退出码 0。同一本账再 `--once` 是 `baseline_stay_flat`，`fill_count` 仍是 0。买卖回合只在标明为合成 K 线的离线测试里强制出现。
