# 产品需求文档：Pendle 聪明钱链上交易监控

| 字段 | 内容 |
| :--- | :--- |
| 产品名 | Pendle Wallet Monitor |
| 文档版本 | V1.1 |
| 状态 | 已落地，本文描述当前行为 + 明确边界 |
| 更新日期 | 2026-10-17 |
| 运行方式 | 本地常驻进程（主路径）/ GitHub Actions 定时单次（备选） |
| 成本约束 | 免费 API + 免费算力 |
| 目标协议 | Pendle Finance（EVM 多链） |

---

## 1. 问题与目标

### 1.1 要解决什么

指定一组 EVM 钱包（巨鲸 / 聪明钱）会在 Pendle 上买卖 PT、YT、提供 LP 或赎回。部分交易会走 1inch、Odos、CowSwap 等聚合器，或从 Safe 多签发出，**不能只看交易 `to` 是否等于 Pendle Router**。

用户需要：命中 Pendle 相关操作后，尽快在 Telegram 收到 **一条结构化提醒**（谁、哪条链、什么动作、进出代币、规模、交易链接），而不是刷屏、漏报或重复推送。

### 1.2 成功标准（产品层）

1. **准**：能识别直连 Router 与经聚合器产生的 PT/YT/SY/LPT 变动。
2. **净**：同一 `txHash` 只发 1 条；重启不重复；首次启动不刷历史。
3. **可用**：免费额度内长期跑；小额噪音可过滤；高优钱包比普通钱包扫得更勤。
4. **可运维**：配置与密钥分离；异常不中断整轮；状态损坏可自愈。

### 1.3 非目标（明确不做）

- 不做全市场扫描、不做未列入名单的地址发现。
- 不做自动跟单、下单、仓位建议。
- 不解析 calldata / 不依赖 `web3.py` 解码事件。
- 不保证毫秒级实时；延迟以轮询间隔为准。
- 不覆盖非 EVM、非 Etherscan V2 支持的链。
- 不把无法定价的长尾代币当成精确 USD 规模。

---

## 2. 用户与使用场景

**主用户：** 自己盯盘的交易者（本地 Windows 常驻运行）。

| 场景 | 期望 |
| :--- | :--- |
| 日常盯盘 | 名单内钱包在 Ethereum / Arbitrum / Monad 上发生 Pendle 相关操作时推送 Telegram |
| 首次接入新钱包 | 只记住当前链上进度，不把几年历史砸到群里 |
| 额度紧张 | 默认只拉 ERC20 转账；Router 兜底可关；高优/普通分级轮询 |
| 导入名单 | 从 CSV 生成 `config.json`；标签含 ✪ 视为高优 |
| 清洗名单 | 按持仓与近 30 日交互额过滤低价值地址（仅主网估算） |
| 改代码后自检 | `python monitor.py --selftest` 不需要任何 Key |

---

## 3. 核心判定：怎样算一笔 Pendle 交易

双重判定，**满足任一即命中**。同一 `txHash` 下该钱包的全部相关 ERC20 转入/转出合并后 **只通知 1 条**。

### 3.1 条件 A（默认主路径，最准）

查该钱包的 ERC20 Token Transfer，代币符号（忽略大小写）符合：

- 前缀：`PT-`、`YT-`、`SY-`（例：`PT-sUSDE-26DEC2026`）
- 包含：`PENDLE-LPT`

前缀列表可在 `config.json` 的 `pendle_token_prefixes` 调整。

### 3.2 条件 B（可选兜底，默认关闭）

查普通交易 `txlist` 的 `to`（小写比对）是否在 `pendle_routers`，且 `isError == "0"`。

当前官方 Router（全链同址）：

- V4：`0x888888888889758f76e7103c6cbf23abbf58f946`
- V3：`0x00000000005bbb0ef59571e58418f9a4357b68a0`

开启后每个 `(链, 钱包)` 多一次 `txlist`，**API 用量约翻倍**。默认 `enable_router_check: false`，靠条件 A 覆盖聚合器路径。

### 3.3 聚合规则（强制）

- 分组键：`txHash`
- 只统计与监控钱包有关的腿：`from == wallet` → Out；`to == wallet` → In
- 数量：`int(value) / 10^tokenDecimal`（小数位非法时按 18）
- 中间 hop（既不是 from 也不是 to 该钱包）忽略
- 禁止同一 `txHash` 拆成多条 Telegram

### 3.4 动作标签（优先级从上到下）

| 条件 | 标签 |
| :--- | :--- |
| 任意腿符号包含 `PENDLE-LPT` | `💧 LP 流动性操作` |
| 转入侧有 `PT-` | `🟢 买入 PT (锁定固收)` |
| 转入侧有 `YT-` | `🔥 买入 YT (做多收益/积分)` |
| 转出侧有 Pendle 符号 | `🔴 卖出/赎回 PT/YT` |
| 其余命中 | `🔄 Pendle 协议交互` |

说明：同时转入 PT 与 YT 时，按上表 **PT 优先于 YT**。这是确定性规则，不是完整策略分类。

---

## 4. 功能需求

### 4.1 监控与轮询

| ID | 需求 | 优先级 |
| :--- | :--- | :--- |
| F1 | 按 `config.json` 的 `chains × wallets` 扫描 | 必须 |
| F2 | 钱包 `priority: true`（标签含 ✪ 导入时自动）走高优间隔，其余走普通间隔 | 必须 |
| F3 | 默认高优 10 分钟、普通 30 分钟，可配置 | 必须 |
| F4 | `RUN_MODE=loop` 常驻；`once` 扫完全部钱包后退出 | 必须 |
| F5 | CLI 可覆盖：`--loop` / `--once` / `--selftest` | 必须 |

当前默认链：Ethereum (`1`)、Monad (`143`)、Arbitrum (`42161`)。增链只需加 `chains` 项（需 Etherscan V2 支持该 `chainid`）。

### 4.2 首次启动与去重

| ID | 需求 | 优先级 |
| :--- | :--- | :--- |
| F6 | `(chain_id, wallet)` 无状态时只记起始块，**不发历史通知** | 必须 |
| F7 | 有代币记录：起始块 = 最新一笔 tokentx 的 `blockNumber`；无记录：起始块 = 链头 | 必须 |
| F8 | 新交易定义：`blockNumber >= last_block` **且** hash 不在 `recent_txs` | 必须 |
| F9 | `recent_txs` 环形保留最近 N 条（默认 50），覆盖同区块多笔 | 必须 |
| F10 | Telegram 发送失败的 hash **不写入 recent、不推进 last_block**，下轮可重发 | 必须 |

### 4.3 USD 规模过滤

| ID | 需求 | 优先级 |
| :--- | :--- | :--- |
| F11 | `min_tx_usd > 0` 时，规模 `< 阈值` 的命中交易记日志、不通知 | 必须 |
| F12 | 规模 = `max(可定价 In 合计 USD, 可定价 Out 合计 USD)` | 必须 |
| F13 | 仅在已命中 Pendle 的交易上拉价格，避免给噪音交易计价 | 必须 |
| F14 | 消息展示 `≈N USD`；存在未定价腿时展示 `≥N USD`（下界） | 必须 |

计价优先级：

1. 已知稳定币符号（及常见 USD 前缀）按 $1
2. WETH / 原生币：CoinGecko，失败则 Etherscan `ethprice`（ETH/Arb）
3. Ethereum / Arbitrum：Pendle 官方 `assets/prices`（批量 20）
4. 其余或失败：该腿不计价（计未定价腿）

Monad 上 Pendle 资产可能无法定价；有稳定币腿时仍可用稳定币侧作为下界。

### 4.4 通知

| ID | 需求 | 优先级 |
| :--- | :--- | :--- |
| F15 | Telegram HTML；关网页预览 | 必须 |
| F16 | 卡片字段：钱包标签+短地址、链、动作、可选规模、Out、In、UTC 时间、浏览器 tx 链接、DeBank | 必须 |
| F17 | `DRY_RUN=1` 只打日志不发送 | 必须 |
| F18 | 429 / 5xx 重试；同会话发送间隔约 1.1s | 必须 |

消息骨架：

```
🚨 Pendle 聪明钱交易提醒

👤 钱包：{label} ({0x1234...abcd})
⛓ 网络：{chain}
🎯 动作：{action}
💰 规模：≈ / ≥ {usd} USD          ← min_tx_usd>0 时

📤 支出 (Out)：
• {amount} {symbol}

📥 获得 (In)：
• {amount} {symbol}

🕒 时间：YYYY-MM-DD HH:MM:SS UTC
🔗 查看交易详情 | DeBank 主页
```

### 4.5 配置与密钥

敏感信息只放 `.env`；业务名单放 `config.json`。

**.env**

| 变量 | 说明 |
| :--- | :--- |
| `ETHERSCAN_API_KEY` | Etherscan V2，一个 Key 查多链 |
| `TG_BOT_TOKEN` | BotFather |
| `TG_CHAT_ID` | 个人或群（群为负数） |
| `RUN_MODE` | `loop` / `once` |
| `DRY_RUN` | `1` 试运行 |

**config.json（结构，勿把真实名单写进 PRD）**

```json
{
  "chains": [
    {"name": "Ethereum", "chain_id": 1, "explorer": "https://etherscan.io"},
    {"name": "Monad", "chain_id": 143, "explorer": "https://monadscan.com"},
    {"name": "Arbitrum", "chain_id": 42161, "explorer": "https://arbiscan.io"}
  ],
  "pendle_routers": [
    "0x888888888889758f76e7103c6cbf23abbf58f946",
    "0x00000000005bbb0ef59571e58418f9a4357b68a0"
  ],
  "pendle_token_prefixes": ["PT-", "YT-", "SY-", "PENDLE-LPT"],
  "enable_router_check": false,
  "tier1_interval_min": 10,
  "tier2_interval_min": 30,
  "api_sleep_sec": 0.25,
  "recent_txs_cap": 50,
  "max_pages": 3,
  "daily_call_budget": 90000,
  "min_tx_usd": 500,
  "wallets": [
    {"address": "0x...", "label": "示例巨鲸✪", "priority": true}
  ]
}
```

### 4.6 配套工具（与主监控分离）

| 脚本 | 职责 |
| :--- | :--- |
| `import_wallets.py` | CSV（`标签----0x地址`）→ `config.json`；✪/★ → `priority`；地址去重 |
| `filter_wallets.py` | 仅 Ethereum：持仓估值 & 近 30 日主流资产交互额过滤；默认只出报告，`--apply` 才改配置 |

过滤默认阈值（脚本常量，改脚本即改规则）：持仓 < 1000 USD **或** 近 30 日交互 < 2000 USD 则剔除。已知近似：超 30 日未动的代币、无法定价长尾按 $0。

---

## 5. 技术约束与架构

保持 **Python 3.10+ 单文件主程序**（`monitor.py`），依赖仅 `requests`、`python-dotenv`。

```
CSV / 手工编辑
      ↓
config.json  +  .env
      ↓
monitor.py
  fetcher   → Etherscan V2 tokentx（可选 txlist）
  analyzer  → 条件 A/B、聚合、动作、USD 过滤
  notifier  → Telegram
  state     → state.json（last_block + recent_txs）
      ↓
Telegram / monitor.log
```

**数据源**

| 用途 | 接口 |
| :--- | :--- |
| ERC20 转账 | `https://api.etherscan.io/v2/api` `action=tokentx` |
| Router 兜底 | 同上 `action=txlist` |
| 链头 | `proxy/eth_blockNumber`（5 分钟缓存） |
| PT/YT/LPT 价格 | `https://api-v2.pendle.finance/core/v1/{chainId}/assets/prices` |
| ETH/MON 价格 | CoinGecko `simple/price` |

**额度保护**

- 请求间隔默认 0.25s（≤ 5 req/s）
- tokentx 最多翻 `max_pages` 页（默认 3 × 100），页未满则停
- 日调用达到 `daily_call_budget`（默认 90000）本轮中止，loop 休眠 30 分钟
- 粗算：`(高优×144 + 普通×48) × 链数` 次/天（条件 B 关闭、每钱包每轮 1 次 tokentx）

**状态 key：** `{chain_id}_{checksum或配置中的地址原文}`  
当前实现使用配置里的 address 字符串，导入时应统一小写，避免同一钱包两套状态。

---

## 6. 异常与边界

| 场景 | 行为 |
| :--- | :--- |
| Etherscan 超时 / 5xx / 限流 | 单请求最多 3 次；失败抛错，**跳过该钱包本轮**，不中断整批 |
| 返回 “No transactions found” | 视为空列表 |
| Telegram 失败 | 不推进该钱包块高，失败 hash 可重发 |
| `state.json` 损坏 | 备份为 `.json.bak`，空状态重建（等价首次启动，不刷历史） |
| 写入中途杀进程 | 先写 `.json.tmp` 再 `os.replace` |
| GitHub Actions | `once` 跑完 commit/push `state.json`；cron 建议 ≥ 30 分钟，并设 concurrency 防重叠 |
| 日预算打满 | 打错误日志，停止本轮扫描 |

---

## 7. 已知限制（产品债，不是 bug）

1. **符号误伤：** 非 Pendle 但符号碰巧以 `PT-`/`YT-`/`SY-` 开头会被当成命中。
2. **条件 B 关闭时：** 若某笔 Router 交互完全不产生监控钱包的 PT/YT/SY/LPT 转账，不会报警（例如纯内部记账）。这是额度换召回的取舍。
3. **高频钱包翻页上限：** 一轮最多约 300 条 tokentx；极端刷量可能漏中间页。
4. **动作标签粗糙：** 轧差、多市场一笔、PT+YT 组合仓会落进优先规则或「协议交互」。
5. **规模是近似值：** 未定价腿只给下界；稳定币列表需随市场手工补。
6. **原生币转账：** 条件 A 只看 ERC20；纯 ETH 进出 Pendle 且无 PT/YT 代币变动时可能漏（开条件 B 可补 Router 直连）。

---

## 8. 目录结构

```text
pendle-wallet-monitor/
├── .env.example
├── .env                      # 本地，不入库
├── config.json               # 名单与策略参数
├── state.json                # 运行生成
├── monitor.py                # 主程序
├── import_wallets.py         # CSV 导入
├── filter_wallets.py         # 名单清洗
├── requirements.txt
├── 启动监控.bat / 停止监控.bat
└── .github/workflows/monitor.yml
```

---

## 9. 验收标准

可离线覆盖的项用 `python monitor.py --selftest`。

- [ ] **冷启动：** 新钱包首次扫描只写 `state.json`，Telegram 0 条历史。
- [ ] **直连 + 聚合器：** 收到 `PT-`/`YT-`/`SY-`/`PENDLE-LPT` 后，在该钱包所属档位的一个轮询周期内发出通知。
- [ ] **合并：** 同一 `txHash` 多腿进出 → 1 条消息，In/Out 金额正确。
- [ ] **噪音：** 无 Pendle 符号的稳定币互换不通知。
- [ ] **去重：** 重启或第二轮相同数据 0 条重复；同区块尚未通知的后续 hash 仍能发出。
- [ ] **动作：** 入 YT → 买入 YT；含 LPT → LP（优先于买入标签）。
- [ ] **小额：** `min_tx_usd=500` 时，可定价规模 300u 的命中不通知；25000u 通知且带规模行。
- [ ] **失败重试：** 模拟发送失败后，该 hash 下轮仍会尝试（不在 recent 中且块高未前进）。
- [ ] **DRY_RUN：** 不调用 Telegram send。
- [ ] **自检：** `--selftest` 退出码 0。

---

## 10. 实现备注（给后续改代码用）

1. 判定、聚合、动作、消息渲染保持纯函数，便于 selftest。
2. 不要引入 `web3.py` 或付费节点，除非产品明确放弃 $0 约束。
3. 改轮询间隔或开关条件 B 前，先按「钱包数 × 链数 × 每轮请求数 × 每天轮次」重算是否打穿免费额度。
4. 增删钱包用 `import_wallets.py` / 编辑 `config.json`，不要把地址写进 `monitor.py`。
5. 修改本 PRD 的判定规则时，同步改 `selftest()` 用例。

---

## 附：按本文生成 / 对齐代码的 Prompt

> 请阅读 `pendle_wallet_monitor_prd.md`。以现有 `monitor.py` 为准做增量，不要推倒重写。严格遵守：条件 A 默认、条件 B 可关、按 txHash 聚合只发 1 条、冷启动不刷历史、发送失败不推进状态、分级轮询、`min_tx_usd` 规模过滤、免费 API 限速与日预算。改判定逻辑必须同时更新 `--selftest`。
