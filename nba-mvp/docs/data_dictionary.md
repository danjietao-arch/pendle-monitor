# 数据字典

目标是 2026—27 常规赛 MVP。快照只能使用 `game_date <= snapshot_date` 的比赛。赛季末战绩、最终资格和事后报道不能写入更早的快照。缺失值保持空，不用 0 代替。

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
