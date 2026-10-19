# 数据字典

目标是 2026—27 常规赛 MVP。快照只能使用 `game_date <= snapshot_date` 的比赛。赛季末战绩、最终资格和事后报道不能写入更早的快照。缺失值保持空，不用 0 代替。

## Stage 1 核心字段

PRD 的核心字段和当前落点：

| 字段 | 落点 |
| --- | --- |
| `player_id` | `game_logs` / `feature_snapshots`。官方 `personId`。`OTHER` 是其他球员 |
| `season` | `2022-23`、`2023-24`、`2024-25` 为可重放样例。目标季仍是 `2026-27` |
| `snapshot_date` | 该球员这条快照的截止日期。赛前是常规赛首场的前一天 |
| `source_timestamp` | 写入这条特征的原始响应抓取时间（UTC）。必须能在 `data/archive/stage1` 里找到 |
| `games_played` | 截止日期前实际上场的比赛数。DNP 不计入，也不写成 0 分钟 |
| `qualifying_games` | 上述比赛里 `minutes > 0` 的场数。分钟缺失时为空 |
| `team_games` | 该球员当时所在球队在截止日期前的比赛数 |
| `player_features` | 仅由 `game_date <= snapshot_date` 的逐场汇总 |
| `team_features` | 仅由该球队 `game_date <= snapshot_date` 的赛果汇总 |
| `injury_status` | 样例里没有逐日官方伤病报告，写 `unknown`，不写成健康 |
| `official_eligible` | 空。样例赛季没有逐人官方资格裁定，不把后来的 65 场规则套回去置 0 |
| `model_version` | 空。三季样本不够训练门禁，状态是「待训练」 |
| `award_winner` | 官方投票唯一最高票为 1，其余为 0。这是赛季末标签，不是赛中特征 |

`stats.nba.com/stats` 在此环境会读超时，不作为 Stage 1 的可用路径。已经走通的官方路径是：

- 逐场：`www.nba.com/game/{game_id}/box-score` 页面里的官方 `game` 对象。存档时去掉转播和图表，保留身份、比分和球员技术统计。
- 球队赛果和赛程日期：`data.nba.com` 的 `00_full_schedule.json`。只收常规赛 `gid` 以 `002` 开头且状态为 `Final` 的比赛。
- 投票：`pr.nba.com` 奖项页链出的 NBA Communications PDF。2022—23 与 2023—24 用投票结果 PDF。2024—25 的结果图只存档、不做 OCR，总分由选票 PDF 按 10/7/5/3/1 汇总。

原始文件在 `data/archive/stage1/`：`schedules/*.json`、`boxscores/*.jsonl.gz`、`votes/*`、`manifest.json`。重放命令不访问网络。2024—25 的 `0022401084` 箱分被源站拒绝，该场只有赛程里的球队比分，没有球员行。

2022—23 的赛前快照没有上一季逐场，上一季场均为空，不是 0。2023—24 和 2024—25 的赛前快照只用更早赛季、并且不晚于赛季开始日前一天的比赛。

## 球员与比赛

| 字段 | 含义 |
| --- | --- |
| `player_id` | NBA 官方球员 ID。候选人改名也不更换。`OTHER` 表示其他球员 |
| `season` | 例如 `2026-27` |
| `game_id`, `game_date` | 比赛 ID 与 ISO 日期 |
| `minutes`, `pts`, `reb`, `ast`, `stl`, `blk`, `tov`, `fgm`, `fga`, `ftm`, `fta` | 当场数据。解析失败或源里没有则为空 |
| `source_url`, `source_timestamp` | 原始响应位置与抓取时间 |

`team_results.win` 为 1/0。没有胜负字段时为空，不记成输球。

## 快照

快照键：`preseason`、`tg_10`、`tg_20`、`tg_40`、`tg_60`、`end`。

`tg_N` 的截止日期是该球员当时所在球队打完第 N 场的日期。球队还没打到第 N 场时，这名球员不进入该快照。

`player_features` / `team_features` 是 JSON。场均只在该组数据完整时计算。`award_winner` 只有在投票表能唯一确定获奖者时才填写。

`official_eligible`：1 官方认定有资格，0 官方认定无资格，空表示未知。未知和未核实规则都不能推出获奖概率 0。2025—26 的 65 场门槛已记录；统计榜 58 场门槛不是 MVP 门槛。2026—27 规则状态是 `unverified`。

## 模型

`predictions.p_award` 为空表示还没有完整分布。状态 `pending_collection` 或 `pending_training`。分布输出后，同一快照的概率（含其他球员）合计为 1。官方认定无资格的球员在这个分布里为 0，其余人重新归一。

`model_runs.shrinkage_w` 为空表示收缩权重还没拟合。不会默认成「打满 20 场就完全相信当季」。

Polymarket 价格不是特征。`use_in_training` 为 true 时训练直接拒绝。

## 市场与每小时对照

`market_registry` 只来自人工审核过的 JSON。队列状态 `unmapped` 或 `rejected` 不参与对照。

`market_quotes.market_mid` 仅在最优 bid 和 ask 都存在、时间戳距离比较时刻不超过 90 分钟时等于 `(bid + ask) / 2`。否则为空，状态是 `N/A` 或 `STALE`。`last_trade` 和 `gamma_price` 只是辅助列。

`delta_pp = 100 * (p_model - p_market_mid)`，单位是百分点。报价过期、单边缺失，或应纳入模型的完场比赛还没处理时，不写这个差值。

`p_market_normalized` 只在注册表声明事件互斥且穷尽、并且每个主市场都有合格中间价时填写。否则 `coverage_status` 为 `coverage_incomplete`。

幂等键是 `(as_of_hour, model_version, market_id)` 和 `(as_of_hour, model_version, player_id)`。补跑时，报价时间晚于该小时结束的记录会被拒绝，不能把后来的价格写成当时的价格。

分歧提醒要求绝对差至少 5 个百分点、买卖价差不超过 3 个百分点，并且连续两个小时成立。文案是「模型与市场分歧」。同一连续区间只提醒一次。

## 任务

`job_status` 记录每小时任务。成功后同一小时再次运行不会重写报价，也不会再次提醒。失败会留下审计日志，已有快照保持原样。
