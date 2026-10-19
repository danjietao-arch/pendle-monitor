# 2026—27 NBA 常规赛 MVP 概率对照

个人研究用的骨架：采集 NBA 官方统计和历年投票、按当时可知的信息做快照、训练基线，并每小时对照 Polymarket 上**同一奖项**的 YES 订单簿。没有训练产物时页面写「待采集 / 待训练」，不编造球员概率。

这不是最佳球员评分，也不构成投注建议。Polymarket 价格默认不进入模型。

## 命令

```bash
pip install -r requirements.txt
PYTHONPATH=src python -m nba_mvp selftest
PYTHONPATH=src python -m nba_mvp collect --season 2024-25
PYTHONPATH=src python -m nba_mvp snapshot --season 2024-25
PYTHONPATH=src python -m nba_mvp train
PYTHONPATH=src python -m nba_mvp infer --season 2026-27
PYTHONPATH=src python -m nba_mvp hourly
PYTHONPATH=src python -m nba_mvp dashboard
```

`stats.nba.com` 经常拒绝云主机。抓取失败会保留上一份数据并标 `STALE`，不会把缺失填成 0。GitHub Actions 的定时任务可能延迟或跳过，工作流在每小时第 17 分钟触发。

## Stage 1：三季可重放样例

样例赛季是 2022—23、2023—24、2024—25。逐场来自 NBA.com 官方箱分页，赛程来自 data.nba.com，投票来自 NBA Communications。三季样本低于训练门禁，不会输出 2026—27 球员概率，状态保持「待训练」。

```bash
PYTHONPATH=src python3 -m nba_mvp stage1-fetch
PYTHONPATH=src python3 -m nba_mvp stage1-replay
PYTHONPATH=src python3 -m nba_mvp selftest
```

`stage1-fetch` 把原始响应写到 `data/archive/stage1/`。箱分失败会留下错误记录，不补造球员数据。`stage1-replay` 只读这些存档，重建比赛、投票和赛前 / 球队 10、20、40、60 场 / 赛季结束快照，并写 `data/reports/stage1_summary.json`。自检会拒绝中期快照里的未来比赛，并核对 `source_timestamp` 能追溯到存档。字段说明在 `docs/data_dictionary.md`。

已知缺口：2024—25 的 `0022401084`（2025-03-30，活塞客场对森林狼）在 `www.nba.com` 上持续返回 Access Denied。该场球队比分仍在赛程存档里，球员逐场缺失，没有填 0。

## Stage 2：历史赛季与两条基线

可拉取的有标签赛季是 2015—16 到 2024—25。`data.nba.com` 对 2014 及更早的赛程返回 HTTP 403，那些赛季没有入库，也没有补造比赛或选票。2019—20 的官方结果 PDF 是扫描件，总分来自选票 PDF 的 10/7/5/3/1，不做 OCR。缩短赛季保持实际场次，不补到 1230 场。

基线 A 只用赛前可知的上场均、上场次和上季球队胜率。基线 B 是 L2 逻辑回归（`C=0.1`，`random_state=0`），按快照键只用更早赛季的同一快照拟合，再在该快照内 softmax。Walk-forward 比较 log loss 和 Brier；Top-1 / Top-3 是辅助指标。可靠性图是 `data/reports/stage2_reliability.svg`。

```bash
PYTHONPATH=src python3 -m nba_mvp stage1-fetch --season 2015-16 --season 2016-17 --season 2017-18 --season 2018-19 --season 2019-20 --season 2020-21 --season 2021-22
PYTHONPATH=src python3 -m nba_mvp stage2-backtest
PYTHONPATH=src python3 -m nba_mvp selftest
```

`stage2-backtest` 只读存档，不访问网络，也不对 2026—27 调用 `infer`。有标签赛季少于 10 个时，状态保持「待训练」。这次存档里 10 个赛季都有官方选票，门禁达到后会写出训练产物，但本命令仍不推断 2026—27。赔率和 Polymarket 不进入这条回测。

2016—17 不进入 A/B 的配对均值：2015—16 没有更早赛季，赛前特征全空，基线 A 无法拟合。从 2017—18 起的配对折上，基线 B 的平均 log loss 和 Brier 都不高于基线 A。`tg_10` 和 `tg_40` 的 Brier 略高，写在报告的 `snapshot_shortfalls` 里，没有为了抹平这个差距去调 `C`。Top-1 / Top-3 只作辅助。可靠性图的正样本就是这些留出快照，样本很小。

## Stage 3：每日增量

`stage3-daily` 只吃 `as_of` 当天及之前已经结束的比赛。同一天再跑一次不会重复写入比赛、周快照或日志。抓取失败时保留已有行，日志和页面标 `STALE`，页面继续显示上一份周快照，缺比赛写「待采集」，不会把球员数据填成 0。未证实伤病不入库，也不把获奖概率置 0。Polymarket 不在这条路径上。

2026—27 赛程在 `data.nba.com` 上目前是 HTTP 403。非存档日更会再请求一次官方伤病 PDF / 伤病页；拿不到就记「待采集」，拿到文件但没有已验证的解析器就记「未解析」，两种情况都不写入球员状态、不把概率置 0。已存档的赛季可以用 `--source archive` 按天重放，这条命令不访问伤病站。

```bash
PYTHONPATH=src python3 -m nba_mvp stage3-daily
PYTHONPATH=src python3 -m nba_mvp stage3-daily --source archive --season 2024-25 --as-of 2024-10-24
```

日志和输出：

- `data/reports/daily/run_log.jsonl`：每次日更一条，同一状态不重复追加
- `data/reports/daily/<season>/<as_of>.json`：当天新比赛、缺失场次、推断状态。推断成功时 `curve` 带上当天概率行，用来保留每日曲线
- `data/reports/weekly/<season>/<as_of>.json` 和同名 CSV：球员、概率、相对上周 Δpp、资格概率、预计胜场区间、`as_of`、`model_version`、理由
- `data/reports/stage3_latest.json` 和 `data/reports/stage3_board.html`：最近一次日更。抓取失败时页面标 `STALE` 并留下上一份周快照；没有可展示的概率时写「待采集」

周快照只在基线 B 已过门禁并且当天推断成功时写出。预计胜场是收缩后胜率的简单区间，不是蒙特卡洛。

## 市场

`data/registry/market_registry.json` 一开始是空的。程序只会把 Gamma 搜索结果放进待审核队列。总决赛 MVP、错误赛季、未人工确认的市场不会进入对照。审核通过后手工写入注册表，每名球员只保留一个主市场。

订单簿中间价只在双边报价都存在且未过期时计算。最近成交价和 Gamma 展示价不会拿来冒充中间价。

## 模型

基线 A 是赛前先验。基线 B 是带 L2 的逻辑回归，再在每个快照内做 softmax。有标签的历史赛季少于 `min_historical_seasons`（默认 10），或模型来自合成样本时，目标赛季不输出概率。蒙特卡洛默认关闭。收缩权重只在回测拟合出结果后才使用。Stage 2 的对照见上一节，不使用 Polymarket 价格。

## 目录

- `src/nba_mvp/`：采集、快照、模型、市场对照、报表、每小时任务
- `data/rules/eligibility_rules.json`：分赛季资格规则。2026—27 仍是 `unverified`
- `data/fixtures/`：离线自检样本，标明为合成数据
- `docs/data_dictionary.md`：字段和时间口径
- `data/reports/`：每小时 JSON、CSV 和 `dashboard.html`

原始响应、SQLite 和模型文件不入库。
