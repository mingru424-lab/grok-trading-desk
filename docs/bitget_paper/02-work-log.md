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
