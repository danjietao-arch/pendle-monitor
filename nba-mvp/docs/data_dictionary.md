# 数据字典

目标是 2026—27 常规赛 MVP。快照只能使用 `game_date <= snapshot_date` 的比赛。赛季末战绩、最终资格和事后报道不能写入更早的快照。缺失值保持空，不用 0 代替。

## Stage 1 核心字段

PRD 的核心字段和当前落点：

| 字段 | 落点 |
| --- | --- |
| `player_id` | `game_logs` / `feature_snapshots`。官方 `personId`。`OTHER` 是其他球员 |
| `season` | 可重放样例从 `2015-16` 到 `2024-25`。`2014-15` 及更早赛程为 HTTP 403，没有补造。目标季仍是 `2026-27` |
| `snapshot_date` | 该球员这条快照的截止日期。赛前是常规赛首场的前一天 |
| `source_timestamp` | 写入这条特征的原始响应抓取时间（UTC）。必须能在 `data/archive/stage1` 里找到 |
| `games_played` | 截止日期前实际上场的比赛数。DNP 不计入，也不写成 0 分钟 |
| `qualifying_games` | 上述比赛里 `minutes > 0` 的场数。分钟缺失时为空 |
| `team_games` | 该球员当时所在球队在截止日期前的比赛数 |
| `player_features` | 仅由 `game_date <= snapshot_date` 的逐场汇总 |
| `team_features` | 仅由该球队 `game_date <= snapshot_date` 的赛果汇总 |
| `injury_status` | 样例里没有逐日官方伤病报告，写 `unknown`，不写成健康 |
| `official_eligible` | 空。样例赛季没有逐人官方资格裁定，不把后来的 65 场规则套回去置 0 |
| `model_version` | 有标签赛季少于 10 个时为空，状态是「待训练」。`stage2-backtest` 不对 2026—27 写概率 |
| `award_winner` | 官方投票唯一最高票为 1，其余为 0。这是赛季末标签，不是赛中特征 |

`stats.nba.com/stats` 在此环境会读超时，不作为 Stage 1 的可用路径。已经走通的官方路径是：

- 逐场：`www.nba.com/game/{game_id}/box-score` 页面里的官方 `game` 对象。存档时去掉转播和图表，保留身份、比分和球员技术统计。
- 球队赛果和赛程日期：`data.nba.com` 的 `00_full_schedule.json`。只收常规赛 `gid` 以 `002` 开头的比赛。赛程状态为 `Final` 时采用赛程比分；否则比分留空，等箱分里 `gameStatus` 为 3 的 Final 补上。
- 投票：NBA Communications 的结果表或选票。2015—16 是 `pr.nba.com` 上的官方 HTML 表。2016—17 到 2018—19、2020—21、2021—22、2022—23、2023—24 是结果 PDF。2019—20、2024—25 和 2025—26 的结果图是图片或扫描件，只说明来源、不做 OCR，总分由选票 PDF 按 10/7/5/3/1 汇总。

原始文件在 `data/archive/stage1/`：`schedules/*.json`、`boxscores/*.jsonl.gz`、`votes/*`、`manifest.json`。重放命令不访问网络。2024—25 的 `0022401084` 箱分被源站拒绝，该场只有赛程里的球队比分，没有球员行。2019—20 有 1059 场、2020—21 有 1080 场，都是缩短赛季，不补到 1230 场。2017—18 赛程里的 `0021700211`（2017-11-15，猛龙客场对鹈鹕）状态停在 `1st Qtr`，赛程比分不采用；该场以官方箱分的 Final 比分写入。箱分也拿不到时，胜负保持为空，不记成输球。2025—26 官方赛程是 1200 场（2025-10-21 至 2026-04-12），`stt` 全是开球时间、比分为空，不把开球时间当成 Final，也不补到 1230 场。`0022500259`（奇才客场对森林狼）、`0022500260`（掘金客场对鹈鹕）、`0022500261`（国王客场对雷霆）都在 2025-11-19：`www.nba.com/game/{id}/box-score` 返回 HTTP 303 到 `/games`，页面没有 game 对象；`cdn.nba.com` 箱分 JSON 为 Access Denied。这三场赛程比分本来就是空的，所以不保留球队比分，也不写球员行。

`player_features.prior_pts_per_game` 和 `prior_games` 只来自上一季、并且不晚于本条快照日期的比赛。`team_features.prior_win_pct` 是该球员上一季最后所在球队、在快照日期之前的胜率。没有上一季时这三项为空，不写成 0。2015—16 是存档的第一季，赛前这三项为空。基线 A 只用这三项。基线 B 才使用本季场均、本季胜率和已赛场次。模型在打分时用训练集的中位数填补特征里的空值，这个填补不写回快照。

`stage2-backtest` 写 `data/reports/stage2_backtest.json` 和 `data/reports/stage2_reliability.svg`。可靠性图按基线 B 的预测概率分档，空档保持为空。

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

## Stage 3 日更

`stage3-daily` 的作业键是 `daily:{season}:{as_of}`。成功之后同一天再跑直接返回，不追加比赛行、周快照或日志。

只写入 `game_date <= as_of` 且赛程或箱分已结束的新比赛。箱分失败时保留赛程里的球队比分，不写球员行，当天 `data_status` 为 `partial`。整份赛程拿不到时不删除已有比赛，`data_status` 为 `STALE`，采集状态为「待采集」，页面继续显示该赛季最近一份周快照。

伤病只接受带来源且状态属于 `out` / `doubtful` / `questionable` / `probable` / `available` 的官方记录。传闻被拒绝。官方伤病会改 `injury_status`，不会把 `p_award` 置 0。当天没有新的官方记录时，沿用该日之前最近一次已入库的官方状态。非存档日更会检查官方伤病地址：失败记「待采集」，文件未解析记「未解析」，都不新增伤病行。2026—27 资格规则仍是 `unverified`，`p_eligibility` 保持空。

推断成功的每一天把概率行写进当天 JSON 的 `curve`。周快照表仍只在周一或该赛季第一份快照时追加。

## Stage 4 蒙特卡洛

`stage4-simulate` 的收缩形式是 `w = games / (games + k)`。`k` 来自历史快照对最终场均的误差，样本少于 10 个就不给出权重。剩余场次只数赛程里 `game_date` 晚于该球员快照日的比赛。出勤概率是已赛场次比上球队已赛场次；官方伤病状态只会按比例下调，未知伤病不会变成 0。

模拟达标概率 `p_games_threshold` 只在资格规则 `verified` 且写了 `min_games` 时有值。统计榜的 58 场不参与。规则 `unverified` 时它和 `p_eligibility` 都为空。官方认定无资格的球员获奖概率为 0。

门禁比较同一批候选上的基线 B 和蒙特卡洛。模拟次数少于 200，或两个随机种子没有同时优于基线 B 时，不升格。未升格时 CSV 的 `primary` 是 `baseline_b`，看板上的真实预测用基线 B，蒙特卡洛和「再缺席 10 场」放在假设情景区块。

`stage4-board --season 2025-26` 只写该赛季的看板，不改 `stage4_board.html`。默认快照是 `end`（没有则用已有快照里最晚的一个）。真实预测只用更早赛季拟合的基线 B。页面是内联 CSS/SVG，同一份 HTML 写到 `data/reports/stage4_board_2025-26.html` 和 `data/reports/boards/2025-26.html`。数值以 `data/reports/boards/2025-26.json` 和同名 CSV 为准。官方选票已入库时，页眉写获奖者，并说明表内概率不是选票份额。

周快照字段：`player_id`、`player_name`、`p_award`、`delta_pp_vs_last_week`、`p_eligibility`、`projected_wins_low`、`projected_wins_high`、`as_of`、`model_version`、`reasons`。没有上周概率时 Δpp 为空，不写成 0。没有胜场或剩余赛程时预计胜场区间为空。

## 市场与每小时对照

`market_registry` 只来自人工审核过的 JSON。队列状态 `unmapped` 或 `rejected` 不参与对照。

`market_quotes.market_mid` 仅在最优 bid 和 ask 都存在、时间戳距离比较时刻不超过 90 分钟时等于 `(bid + ask) / 2`。否则为空，状态是 `N/A` 或 `STALE`。`last_trade` 和 `gamma_price` 只是辅助列。

`delta_pp = 100 * (p_model - p_market_mid)`，单位是百分点。报价过期、单边缺失，或应纳入模型的完场比赛还没处理时，不写这个差值。

`p_market_normalized` 只在注册表声明事件互斥且穷尽、并且每个主市场都有合格中间价时填写。否则 `coverage_status` 为 `coverage_incomplete`。

幂等键是 `(as_of_hour, model_version, market_id)` 和 `(as_of_hour, model_version, player_id)`。补跑时，报价时间晚于该小时结束的记录会被拒绝，不能把后来的价格写成当时的价格。

分歧提醒要求绝对差至少 5 个百分点、买卖价差不超过 3 个百分点，并且连续两个小时成立。文案是「模型与市场分歧」。同一连续区间只提醒一次。

## 任务

`job_status` 记录每小时任务。成功后同一小时再次运行不会重写报价，也不会再次提醒。失败会留下审计日志，已有快照保持原样。
