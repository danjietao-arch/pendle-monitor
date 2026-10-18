# 📡 Pendle 聪明钱链上交易监控机器人

监控一组指定钱包（巨鲸/聪明钱）在 **Pendle Finance** 上的链上操作（买/卖 PT、YT、LP、赎回，含经聚合器的交易），命中后通过 **Telegram Bot** 推送结构化提醒。

由 GitHub Actions 每 15 分钟云端自动运行，无需本地开机。状态（`state.json`）每轮自动 commit 回仓库实现断点续扫：**掉单不漏单，只延迟**。

## 🎯 核心逻辑

- **判定条件 A（主）**：ERC20 代币符号特征 —— `PT-` / `YT-` / `SY-` 前缀或包含 `PENDLE-LPT`（无论是否经 1inch/Odos 等聚合器都能捕获）
- **判定条件 B（兜底，默认关）**：交易 `to` 地址命中 Pendle Router V4/V3（`config.json` 的 `enable_router_check`）
- **聚合**：同一 `txHash` 的所有代币转入/转出合并为 1 条通知
- **规模过滤**：单笔交互估值 `min_tx_usd`（默认 100 USD）以下不通知
- **通知内容**：钱包/链/动作标签/**市场隐含APY**/USD规模/收支明细/交易与 DeBank 链接

## ⚙️ 配置

| 文件 | 说明 |
|---|---|
| `config.json` | 监控链、钱包列表（`priority:true` 为高优）、路由、代币前缀、阈值 |
| `.env`（本地）/ Secrets（Actions） | `ETHERSCAN_API_KEY` / `TG_BOT_TOKEN` / `TG_CHAT_ID` |
| `.github/workflows/monitor.yml` | 云端定时任务（cron，UTC 时区） |

**修改钱包列表或阈值**：直接在 GitHub 网页编辑 `config.json` 提交即可，下一轮自动生效。

## 💰 免费额度核算

- Etherscan V2 统一 API（一个 Key 查所有链）：~2 万次/天（免费 10 万）
- GitHub Actions 公开仓库：免费无上限

## 🖥️ 本地运行（备用）

```bash
pip install -r requirements.txt
cp .env.example .env   # 填入三个密钥
python monitor.py --selftest   # 离线自检
python monitor.py              # 常驻轮询模式
python monitor.py --once       # 单次执行
```

> ⚠️ 从云端切回本地前，务必先 `git pull` 拉取云端最新 `state.json`，否则会从旧断点重扫导致重复通知；云端与本地不可同时运行（双发通知）。

## 📁 目录结构

```
├── monitor.py           # 主程序（拉取/聚合/防重/通知/规模过滤/APY）
├── config.json          # 监控配置（链/钱包/阈值）
├── state.json           # 扫描断点（由 Actions 自动维护）
├── import_wallets.py    # CSV 钱包列表导入工具
├── filter_wallets.py    # 钱包过滤工具（按持仓/30日交互额）
├── requirements.txt     # requests / python-dotenv
└── .github/workflows/monitor.yml
```
