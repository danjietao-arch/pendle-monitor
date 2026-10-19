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

## 市场

`data/registry/market_registry.json` 一开始是空的。程序只会把 Gamma 搜索结果放进待审核队列。总决赛 MVP、错误赛季、未人工确认的市场不会进入对照。审核通过后手工写入注册表，每名球员只保留一个主市场。

订单簿中间价只在双边报价都存在且未过期时计算。最近成交价和 Gamma 展示价不会拿来冒充中间价。

## 模型

基线 B 是带 L2 的逻辑回归，再在每个快照内做 softmax。有标签的历史赛季少于 `min_historical_seasons`（默认 10），或模型来自合成样本时，目标赛季不输出概率。蒙特卡洛默认关闭。收缩权重只在回测拟合出结果后才使用。

## 目录

- `src/nba_mvp/`：采集、快照、模型、市场对照、报表、每小时任务
- `data/rules/eligibility_rules.json`：分赛季资格规则。2026—27 仍是 `unverified`
- `data/fixtures/`：离线自检样本，标明为合成数据
- `docs/data_dictionary.md`：字段和时间口径
- `data/reports/`：每小时 JSON、CSV 和 `dashboard.html`

原始响应、SQLite 和模型文件不入库。
