# 钱多多状态页

产品名只用于这个只读页面。程序入口仍是 `python -m src.bitget_paper`。助手名称没有改。

## 设计来源

视觉来自用户量化 2.0，仓库 `mingru424-lab/k-line-director`，提交 `30f6897ccd611c31a84cdfd26ac09523962d5edd`。对照用的是同一份源码导出，不是那个项目的实盘。

| 钱多多 | 来源 | 用法 |
|---|---|---|
| 颜色、字体、卡片、徽章 | `frontend/src/styles/theme.css` | CSS 变量原样放进 `status_server.py`。红涨绿跌保留 |
| 顶栏、导航、明暗按钮 | `frontend/src/components/AppHeader.vue`、`frontend/src/stores/theme.ts` | 顶栏标题改为钱多多。导航只跳到本页已有区块。明暗存在浏览器本地，不写账本 |
| 权益、现金、持仓、运行四张卡 | `frontend/src/components/Thermometer.vue` | 数字来自快照，没有数字时写「未采集」 |
| 决策表 | `frontend/src/components/SignalTable.vue` | 列换成时间、动作、原因、关联号、是否写入本地成交 |
| 权益图外框 | `frontend/src/components/KlineChart.vue` | 只画已有权益点的折线。不画 K 线，不加载 echarts |
| 页脚 | `frontend/src/components/FooterDisclaimer.vue` | 改成纸面和公开行情的声明 |
| 未接入说明 | `frontend/src/router/index.ts` 里的币安、A 股、期货、指数、资讯、策略、回测 | 这些没有后端。页面只写一句「未接入」，没有按钮 |

## 字段从哪来

页面读进程内存里的快照，和 `/health` 是同一份。`loop.py` 在每轮结束后更新它。

| 页面 | 快照字段 | 没有值时 |
|---|---|---|
| 运行 | `runtime` | 未采集。已知值会带上原文：运行中 · running、已暂停 · paused、紧急停止 · emergency_stop、已停止 · stopped |
| 行情状态、原因、年龄 | `feed.status`、`feed.reason`、`feed.ticker_age_ms`、`feed.as_of` | 未采集 |
| 最新价、标记价 | `feed.last_price`、`feed.mark_price` | 未采集。行情拒绝时保留上一笔价格，并标「行情未刷新」 |
| 快慢均线数值 | `strategy.fast_sma`、`strategy.slow_sma` | 未采集。周期根数仍来自配置 |
| 现金、持仓、已实现、未实现、权益 | `book.*` | 未采集。拒绝行情不会把已有账本擦成空白，`book.stale` 为真时写明沿用上一笔 |
| 决策 | `recent_decisions`，最多 20 条 | 尚无决策记录 |
| 权益曲线和表 | `equity`，曲线最多 120 点，表显示末尾 12 点 | 少于 2 个可绘制的点就不画线。不能解析或绝对值过大的点只留在表里，不进曲线 |
| 紧急平仓 | `risk.emergency_flatten` | 未采集。false 时写明默认不平仓 |

最新价、标记价和均线数值是决策当时已经算出来的字符串，多写进摘要供页面显示。买卖、持仓、风控和账本迁移没有改规则。

## 限制

- 只绑定 `127.0.0.1`。没有登录，也不监听公网。
- 页面每 5 秒整页刷新。这不是交易控制。
- 不伪造 K 线、收益、成交或胜率。短权益历史会写明点数。
- 决策和日志文本按 HTML 转义。找不到的路径返回 404，不拿账本页冒充。
- 没有外链 CDN。样式随 Python 文件走，不需要构建。
- 量化 2.0 里的筛选按钮、K 线、AI 表和回测计算都没有搬过来。

## 已有运行目录怎么换页面

1. 停掉正在跑的 `python -m src.bitget_paper --run`。不要删运行目录。
2. 更新代码。固定提交可以用 `https://codeload.github.com/mingru424-lab/grok-trading-desk/tar.gz/<提交号>`。解压到别处，或在原仓库拉取同一分支。
3. 用原来的配置启动，例如 `--config /Users/ming/Documents/qianduoduo-paper-run/` 下现有的 yaml。不要用示例配置覆盖它，也不要覆盖账本、`*.jsonl` 和控制文件。
4. 浏览器打开 `http://127.0.0.1:8765/`。旧进程退出前，浏览器看到的仍是旧页面。

云端提交不会自动替换另一台电脑上已经打开的页面。
