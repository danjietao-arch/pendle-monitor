# -*- coding: utf-8 -*-
"""
一次性导入脚本：把「监控地址 (2).csv」转换为 config.json 的 wallets 配置。

CSV 格式（无表头，每行一条）：
    标签----0x地址
已知格式坑（均已处理）：
    - 文件带 UTF-8 BOM 头
    - 个别行因含逗号被 Excel 用双引号包裹（csv.reader 会自动还原）
    - 末尾存在空行
    - 标签中的 ✪ / ★ 标记 => priority=true（高优分级轮询）
    - 可能存在重复地址（保留首个标签，优先级取并集）

用法：
    python import_wallets.py            # 默认读取「监控地址 (2).csv」
    python import_wallets.py 我的.csv   # 指定其他文件
"""

import csv
import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CSV = BASE_DIR / "监控地址 (2).csv"
CONFIG_FILE = BASE_DIR / "config.json"

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# 监控链配置（Etherscan V2 统一 API 均支持，免费层可用）
DEFAULT_CHAINS = [
    {"name": "Ethereum", "chain_id": 1, "explorer": "https://etherscan.io"},
    {"name": "Monad", "chain_id": 143, "explorer": "https://monadscan.com"},
    {"name": "Arbitrum", "chain_id": 42161, "explorer": "https://arbiscan.io"},
]

# Pendle 官方路由（全链同址部署），条件 B 兜底用（默认关闭）
DEFAULT_ROUTERS = [
    "0x888888888889758f76e7103c6cbf23abbf58f946",  # Pendle Router V4
    "0x00000000005bbb0ef59571e58418f9a4357b68a0",  # Pendle Router V3（旧版兼容）
]

# 默认业务配置（生成后可手动修改 config.json）
DEFAULT_SETTINGS = {
    "pendle_token_prefixes": ["PT-", "YT-", "SY-", "PENDLE-LPT"],
    "enable_router_check": False,   # 条件 B（txlist 路由匹配），开启后 API 用量翻倍
    "tier1_interval_min": 10,       # ✪ 高优钱包轮询间隔（分钟）
    "tier2_interval_min": 30,       # 普通钱包轮询间隔（分钟）
    "api_sleep_sec": 0.25,          # 每次 API 请求间隔，<=5 req/s 免费限制
    "recent_txs_cap": 50,           # 每钱包已通知哈希保留数量（二次去重）
    "max_pages": 3,                 # 单钱包单轮最多翻页数（防高频钱包漏扫）
    "daily_call_budget": 90000,     # 每日 API 调用保护阈值（免费额度 10 万）
}


def parse_csv(csv_file: Path):
    """解析 CSV，返回 (wallets 列表, 重复地址报告)"""
    wallets = []
    seen = {}          # 地址小写 -> (label, priority, 行号)
    dup_report = []
    bad_lines = []

    with open(csv_file, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for lineno, row in enumerate(reader, start=1):
            # 引号包裹的逗号会被 csv 模块拆列，拼回来再按 ---- 切
            raw = ",".join(cell for cell in row if cell is not None).strip()
            if not raw:
                continue
            if "----" not in raw:
                bad_lines.append((lineno, raw[:40]))
                continue

            label, _, addr = raw.rpartition("----")
            label = label.strip().strip('"').strip("'").strip()
            addr = addr.strip().strip('"').strip("'").strip()

            if not ADDR_RE.match(addr):
                bad_lines.append((lineno, raw[:40]))
                continue

            addr_lw = addr.lower()
            priority = ("✪" in label) or ("★" in label)

            if addr_lw in seen:
                old_label, old_priority, old_line = seen[addr_lw]
                dup_report.append(
                    f"  行{lineno} 与 行{old_line} 重复: {addr_lw} "
                    f"(保留标签「{old_label}」，优先级 {'✪' if old_priority else '普通'})"
                )
                # 优先级取并集：重复地址任一标签带 ✪ 即视为高优
                if priority and not old_priority:
                    for w in wallets:
                        if w["address"] == addr_lw:
                            w["priority"] = True
                    seen[addr_lw] = (old_label, True, old_line)
                continue

            seen[addr_lw] = (label, priority, lineno)
            wallets.append({"address": addr_lw, "label": label, "priority": priority})

    return wallets, dup_report, bad_lines


def main():
    csv_file = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV
    if not csv_file.exists():
        print(f"❌ 找不到 CSV 文件: {csv_file}")
        sys.exit(1)

    wallets, dup_report, bad_lines = parse_csv(csv_file)

    # 已有 config.json 时只更新 wallets，保留其他手工配置
    if CONFIG_FILE.exists():
        try:
            config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            print(f"ℹ️ 检测到已有 {CONFIG_FILE.name}，仅更新 wallets 字段，其余配置保留")
        except Exception as e:
            print(f"⚠️ 现有 config.json 解析失败({e})，将重新生成完整配置")
            config = {}
    else:
        config = {}

    config.setdefault("chains", DEFAULT_CHAINS)
    config.setdefault("pendle_routers", DEFAULT_ROUTERS)
    for k, v in DEFAULT_SETTINGS.items():
        config.setdefault(k, v)
    config["wallets"] = wallets

    CONFIG_FILE.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    n_pri = sum(1 for w in wallets if w["priority"])
    print("=" * 60)
    print(f"✅ 导入完成: {CONFIG_FILE}")
    print(f"   钱包总数 : {len(wallets)}")
    print(f"   ✪ 高优先: {n_pri}  (每 {DEFAULT_SETTINGS['tier1_interval_min']} 分钟轮询)")
    print(f"   普通优先: {len(wallets) - n_pri}  (每 {DEFAULT_SETTINGS['tier2_interval_min']} 分钟轮询)")
    print(f"   监控链  : {' / '.join(c['name'] for c in config['chains'])}")
    if dup_report:
        print(f"⚠️ 发现 {len(dup_report)} 个重复地址（已自动去重）:")
        print("\n".join(dup_report))
    if bad_lines:
        print(f"⚠️ 跳过 {len(bad_lines)} 行无法解析的数据:")
        for lineno, text in bad_lines:
            print(f"  行{lineno}: {text}")
    print("=" * 60)


if __name__ == "__main__":
    main()
