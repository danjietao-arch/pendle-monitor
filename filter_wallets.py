# -*- coding: utf-8 -*-
"""
钱包过滤脚本（仅统计 Ethereum 主网数据）

过滤规则（移除满足任一条件的钱包）:
    1) 当前持仓估值 < 1000 USD（ETH 原生 + 近30日触达代币的当前余额，按最新价格估算）
    2) 近 30 日交互额度 < 2000 USD（30日内 ERC20 转账金额 + ETH 转账金额，双向累计）

数据源（全部免费）:
    - Etherscan V2 (chainid=1): tokentx / txlist / balancemulti / tokenbalance / getblocknobytime
    - Pendle 官方 API:  /core/v1/1/assets/prices  （PT/YT/LPT/SY/主流资产 USD 价格）
    - CoinGecko 免费接口: token_price/ethereum （其余 ERC20）
    - 稳定币兜底: 已知稳定币符号按 $1 计

用法:
    python filter_wallets.py                # 试运行: 只生成 filter_report.json，不改任何配置
    python filter_wallets.py --apply        # 实际应用: 备份并更新 config.json / state.json
    python filter_wallets.py --reset        # 忽略缓存重新采集（约10分钟）

说明:
    - 断点续跑: 中间结果存 filter_cache.json，中断后重跑自动续
    - 已知近似: 超过30日未动过的代币持仓不可见（免费接口限制）; 无法定价的长尾代币按 $0 计并在报告中标注
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import requests
    from dotenv import load_dotenv
except ImportError:
    print("缺少依赖: pip install -r requirements.txt")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
CACHE_FILE = BASE_DIR / "filter_cache.json"
REPORT_FILE = BASE_DIR / "filter_report.json"

CHAIN_ID = 1  # 仅以太坊主网
ES_URL = "https://api.etherscan.io/v2/api"
PENDLE_PRICE_URL = "https://api-v2.pendle.finance/core/v1/1/assets/prices"
CG_TOKEN_URL = "https://api.coingecko.com/api/v3/simple/token_price/ethereum"
CG_SIMPLE_URL = "https://api.coingecko.com/api/v3/simple/price"

MIN_HOLD_USD = 1000.0
MIN_VOL30D_USD = 2000.0
MAX_TOKENS_PER_WALLET = 25   # 每钱包最多查询余额的代币数（按30日成交额排序取前N）
PAGES_TOKENTX = 2            # 30日代币转账最多翻2页(200条)
PAGE_SIZE_TXLIST = 100

# 已知稳定币（符号大写后匹配，按 $1 计价）
STABLE_SYMBOLS = {
    "USDC", "USDT", "DAI", "USDE", "SUSDE", "USDS", "SUSDS", "FDUSD", "TUSD", "BUSD",
    "PYUSD", "USDD", "USD0", "USD1", "AUSDC", "GUSDC", "SRUSDE", "SRUSDT", "SUSDT",
    "CrvUSD", "SDAI", "EUSD", "DEUSD", "USDC.E", "USDT.E", "LUSD", "FRAX", "WUSD",
    "GHO", "SLVUSD", "SPLUSD", "SYRUPUSDC", "USDAI", "USDF", "USDY", "SUSY",
}

# ===== 主流代币白名单（持仓与30日交互额只统计这些; 长尾/meme币一律忽略） =====
MAINSTREAM_PREFIXES = ("PT-", "YT-", "SY-")           # Pendle 生态代币全部计入
MAINSTREAM_EXACT = set(STABLE_SYMBOLS) | {
    # LST / LRT
    "WSTETH", "STETH", "WETH", "WEETH", "EETH", "RETH", "CBETH", "EZETH", "RSETH",
    "PUFETH", "SWETH", "METH", "OSETH", "STETHX",
    # BTC 系
    "WBTC", "CBBTC", "SOLVBTC", "SBTC", "TBTC", "EBTC", "LBTC", "MBTC",
    # 主流蓝筹
    "LINK", "AAVE", "UNI", "CRV", "CVX", "LDO", "PENDLE", "ENA", "ARB", "OP",
    "MKR", "SKY", "COMP", "SUSHI", "YFI", "FXS", "MORPHO", "ETHFI", "JTO",
    "PENDLE-LPT",
}
# 上面这些主流币若 Pendle API 没覆盖, 用 CoinGecko 按符号补一次价格（一次请求搞定）
MAINSTREAM_CG_IDS = {
    "WBTC": "wrapped-bitcoin", "LINK": "chainlink", "UNI": "uniswap", "AAVE": "aave",
    "CRV": "curve-dao-token", "CVX": "convex-finance", "LDO": "lido-dao",
    "PENDLE": "pendle", "ENA": "ethena", "ARB": "arbitrum", "OP": "optimism",
    "MKR": "maker", "COMP": "compound-governance-token", "SUSHI": "sushi-swap",
    "YFI": "yearn-finance", "FXS": "frax-share", "MORPHO": "morpho", "GHO": "gho",
    "ETHFI": "ether-fi", "JTO": "jito-governance-token", "SOLVBTC": "solv-btc",
    "CBBTC": "cbBTC", "EZETH": "ez-eth", "PUFETH": "puffer-eth",
    "RSETH": "kelp-dao-restaked-eth", "RETH": "rocket-pool-eth", "STETH": "staked-ether",
}


def is_mainstream(symbol: str) -> bool:
    s = (symbol or "").upper().strip()
    return s.startswith(MAINSTREAM_PREFIXES) or s in MAINSTREAM_EXACT or s in MAINSTREAM_CG_IDS

session = requests.Session()
log = lambda *a: print(*a, flush=True)


def es_sleep():
    time.sleep(0.25)  # Etherscan 免费限速 5 req/s


def es(action, module, **params) -> dict:
    q = {"chainid": CHAIN_ID, "module": module, "action": action, "apikey": API_KEY, **params}
    for attempt in range(1, 4):
        try:
            r = session.get(ES_URL, params=q, timeout=15)
            es_sleep()
            r.raise_for_status()
            data = r.json()
            if str(data.get("status")) == "0":
                msg = f'{data.get("message", "")} {data.get("result", "")}'.lower()
                if "rate limit" in msg or "max rate" in msg:
                    time.sleep(1.2)
                    continue
            return data
        except (requests.RequestException, ValueError) as e:
            log(f"  [warn] Etherscan {action} 失败({attempt}/3): {e}")
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"Etherscan {action} 连续失败")


def es_rows(data) -> list:
    res = data.get("result")
    return res if isinstance(res, list) else []


# ----------------------------------------------------------------------------
# 缓存
# ----------------------------------------------------------------------------
def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"wallets": {}, "prices": {}, "native": {}, "meta": {}}


def save_cache(cache: dict):
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CACHE_FILE)


# ----------------------------------------------------------------------------
# 阶段 1: 采集每个钱包近30日活动（tokentx + txlist）
# ----------------------------------------------------------------------------
def scan_activity(wallets: list[dict], cache: dict) -> None:
    todo = [w for w in wallets if w["address"] not in cache["wallets"]]
    log(f"\n[阶段1] 采集30日活动: 共{len(wallets)}个钱包, 待采集{len(todo)}个")
    t0 = time.time()
    for i, w in enumerate(todo, 1):
        addr = w["address"]
        # --- 30天前区块（缓存一次） ---
        if "b30" not in cache["meta"]:
            d = es("getblocknobytime", "block", timestamp=int(time.time()) - 30 * 86400, closest="before")
            cache["meta"]["b30"] = int(d.get("result") or 0)
            cache["meta"]["b30_at"] = int(time.time())
        b30 = cache["meta"]["b30"]

        # --- ERC20 转账 ---
        txs = []
        for page in range(1, PAGES_TOKENTX + 1):
            rows = es_rows(es("tokentx", "account", address=addr, startblock=b30,
                              endblock=99999999, page=page, offset=100, sort="desc"))
            txs.extend(rows)
            if len(rows) < 100:
                break
        tokens: dict[str, dict] = {}  # contract -> {sym, dec, in, out}
        for t in txs:
            c = t.get("contractAddress", "").lower()
            if not c:
                continue
            meta = tokens.setdefault(c, {"sym": t.get("tokenSymbol") or "?",
                                         "dec": int(t.get("tokenDecimal") or 0 or 18),
                                         "in": "0", "out": "0"})
            v = t.get("value", "0") or "0"
            if t.get("to", "").lower() == addr:
                meta["in"] = str(Decimal(meta["in"]) + Decimal(v))
            elif t.get("from", "").lower() == addr:
                meta["out"] = str(Decimal(meta["out"]) + Decimal(v))

        # --- ETH 普通转账 ---
        eth_in = eth_out = Decimal(0)
        for t in es_rows(es("txlist", "account", address=addr, startblock=b30,
                            endblock=99999999, page=1, offset=PAGE_SIZE_TXLIST, sort="desc")):
            v = Decimal(t.get("value", "0") or "0")
            if v == 0:
                continue
            if t.get("from", "").lower() == addr:
                eth_out += v
            if t.get("to", "").lower() == addr:
                eth_in += v

        cache["wallets"][addr] = {
            "label": w.get("label", ""), "priority": bool(w.get("priority")),
            "tokens": tokens, "eth_in": str(eth_in), "eth_out": str(eth_out),
        }
        if i % 25 == 0 or i == len(todo):
            save_cache(cache)
            speed = i / max(time.time() - t0, 1)
            log(f"  进度 {i}/{len(todo)} ({speed:.1f}个/秒, 预计剩余 {(len(todo)-i)/max(speed,0.01)/60:.1f} 分钟)")
    save_cache(cache)


# ----------------------------------------------------------------------------
# 阶段 2: 价格解析（Pendle API -> CoinGecko -> 稳定币兜底）
# ----------------------------------------------------------------------------
def resolve_prices(cache: dict, eth_price: float) -> dict:
    # 只解析主流代币（长尾/不常见代币直接忽略, 不参与估值）
    sym_of = {}
    for info in cache["wallets"].values():
        for c, m in info["tokens"].items():
            sym_of[c] = m["sym"]
    mainstream = [c for c in sym_of if is_mainstream(sym_of[c])]
    todo = [c for c in mainstream if cache["prices"].get(c) is None]
    log(f"\n[阶段2] 价格解析(仅主流代币): 涉及{len(mainstream)}个, 待解析{len(todo)}个 "
        f"(忽略长尾代币{len(sym_of) - len(mainstream)}个)")

    # --- 已知恒等关系: WETH = ETH ---
    WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
    for c in todo:
        if c == WETH:
            cache["prices"][c] = eth_price
    todo = [c for c in todo if cache["prices"].get(c) is None]

    # --- Pendle 官方价格（含 PT/YT/LPT/SY/主流资产; 单次最多20个地址） ---
    pendle_map = {}
    for i in range(0, len(todo), 20):
        chunk = todo[i:i + 20]
        try:
            r = session.get(PENDLE_PRICE_URL, params={"addresses": ",".join(chunk)}, timeout=20)
            if r.status_code == 200:
                for k, v in (r.json().get("prices") or {}).items():
                    pendle_map[k.lower()] = float(v)
            else:
                log(f"  [warn] Pendle价格接口 {r.status_code}: {r.text[:80]}")
        except requests.RequestException as e:
            log(f"  [warn] Pendle价格请求失败: {e}")
        time.sleep(1.0)

    # --- 主流币符号级兜底: CoinGecko 一次请求 ---
    need_syms = {sym_of[c].upper() for c in todo
                 if c not in pendle_map and sym_of[c].upper() in MAINSTREAM_CG_IDS}
    cg_sym_map = {}
    if need_syms:
        ids = {MAINSTREAM_CG_IDS[s] for s in need_syms if s in MAINSTREAM_CG_IDS}
        try:
            r = session.get(CG_SIMPLE_URL, params={"ids": ",".join(sorted(ids)),
                                                   "vs_currencies": "usd"}, timeout=20)
            if r.status_code == 200:
                for sym, cid in MAINSTREAM_CG_IDS.items():
                    v = (r.json() or {}).get(cid, {}).get("usd")
                    if v is not None:
                        cg_sym_map[sym] = float(v)
            else:
                log(f"  [warn] CoinGecko主流币 {r.status_code}: {r.text[:80]}")
        except requests.RequestException as e:
            log(f"  [warn] CoinGecko请求失败: {e}")

    # --- 合并 + 稳定币 $1 兜底 ---
    for c in todo:
        sym = sym_of[c].upper()
        if c in pendle_map:
            cache["prices"][c] = pendle_map[c]
        elif sym in cg_sym_map:
            cache["prices"][c] = cg_sym_map[sym]
        elif sym in STABLE_SYMBOLS or sym.startswith(("USD", "SUSD", "AUSD", "GUSD")):
            cache["prices"][c] = 1.0
        else:
            cache["prices"][c] = None  # 无法定价(按$0计, 报告标注)
    save_cache(cache)
    priced = sum(1 for c in todo if cache["prices"][c] is not None)
    log(f"  完成: Pendle定价{len(pendle_map)} + CG主流{len(cg_sym_map)} + 稳定币兜底, "
        f"共可定价{priced}/{len(todo)}, 无法定价{len(todo) - priced}个(按$0计)")
    return {"eth_price": eth_price}


def get_eth_price(api_key: str) -> float:
    try:
        r = session.get(CG_SIMPLE_URL, params={"ids": "ethereum", "vs_currencies": "usd"}, timeout=15)
        if r.status_code == 200:
            return float(r.json()["ethereum"]["usd"])
    except Exception:
        pass
    d = es("ethprice", "stats")  # 兜底: Etherscan
    return float(d["result"]["ethusd"])


# ----------------------------------------------------------------------------
# 阶段 3: 原生余额（balancemulti 批量） + 代币当前余额（tokenbalance）
# ----------------------------------------------------------------------------
def scan_balances(wallets: list[dict], cache: dict) -> None:
    addrs = [w["address"] for w in wallets]
    todo_native = [a for a in addrs if a not in cache["native"]]
    log(f"\n[阶段3a] ETH原生余额(balancemulti批量): 待查{len(todo_native)}个")
    for i in range(0, len(todo_native), 20):
        chunk = todo_native[i:i + 20]
        rows = es_rows(es("balancemulti", "account", address=",".join(chunk), tag="latest"))
        for r in rows:
            cache["native"][r["account"].lower()] = r["balance"]
        if i % 200 == 0:
            save_cache(cache)
    save_cache(cache)

    # 每钱包只查主流代币的当前余额（按30日成交额排序, 上限N个）
    log(f"[阶段3b] 代币当前余额(tokenbalance): 仅主流代币, 每钱包最多{MAX_TOKENS_PER_WALLET}个")
    pairs = []
    for w in wallets:
        info = cache["wallets"].get(w["address"])
        if not info:
            continue
        ranked = sorted(
            ((c, m) for c, m in info["tokens"].items() if is_mainstream(m["sym"])),
            key=lambda kv: Decimal(kv[1]["in"]) + Decimal(kv[1]["out"]),
            reverse=True,
        )[:MAX_TOKENS_PER_WALLET]
        for c, _ in ranked:
            key = f"{w['address']}|{c}"
            if key not in cache.get("tokbal", {}):
                pairs.append((w["address"], c, key))
    cache.setdefault("tokbal", {})
    t0 = time.time()
    for i, (addr, contract, key) in enumerate(pairs, 1):
        d = es("tokenbalance", "account", contractaddress=contract, address=addr, tag="latest")
        res = d.get("result")
        cache["tokbal"][key] = res if isinstance(res, str) else "0"
        if i % 100 == 0 or i == len(pairs):
            save_cache(cache)
            speed = i / max(time.time() - t0, 1)
            log(f"  进度 {i}/{len(pairs)} ({speed:.1f}对/秒)")
    save_cache(cache)


# ----------------------------------------------------------------------------
# 阶段 4: 汇总估值 + 过滤判定 + 报告/应用
# ----------------------------------------------------------------------------
def evaluate(cache: dict, eth_price: float) -> list[dict]:
    results = []
    for addr, info in cache["wallets"].items():
        prices = cache["prices"]
        native = Decimal(cache["native"].get(addr, "0")) / Decimal(10 ** 18)
        holdings = float(native) * eth_price
        volume = (Decimal(info["eth_in"]) + Decimal(info["eth_out"])) / Decimal(10 ** 18)
        volume_usd = float(volume) * eth_price
        unpriced = 0

        ranked = sorted(
            ((c, m) for c, m in info["tokens"].items() if is_mainstream(m["sym"])),
            key=lambda kv: Decimal(kv[1]["in"]) + Decimal(kv[1]["out"]),
            reverse=True,
        )[:MAX_TOKENS_PER_WALLET]
        tail_ignored = sum(1 for m in info["tokens"].values() if not is_mainstream(m["sym"]))
        for c, meta in ranked:
            p = prices.get(c)
            dec = int(meta["dec"]) or 18
            amt30 = (Decimal(meta["in"]) + Decimal(meta["out"])) / Decimal(10 ** dec)
            if p is None:
                unpriced += 1
                continue
            volume_usd += float(amt30) * p
            bal = cache.get("tokbal", {}).get(f"{addr}|{c}")
            if bal is not None:
                holdings += float(Decimal(bal) / Decimal(10 ** dec)) * p

        keep = holdings >= MIN_HOLD_USD and volume_usd >= MIN_VOL30D_USD
        results.append({
            "address": addr, "label": info["label"], "priority": info["priority"],
            "holdings_usd": round(holdings, 2), "volume30d_usd": round(volume_usd, 2),
            "eth_native": float(native), "unpriced_tokens": unpriced,
            "tail_ignored": tail_ignored, "keep": keep,
        })
    results.sort(key=lambda r: (not r["keep"], -(r["holdings_usd"] + r["volume30d_usd"])))
    return results


def write_report(results: list[dict], eth_price: float):
    kept = [r for r in results if r["keep"]]
    removed = [r for r in results if not r["keep"]]
    low_hold = [r for r in removed if r["holdings_usd"] < MIN_HOLD_USD]
    low_vol = [r for r in removed if r["volume30d_usd"] < MIN_VOL30D_USD]
    pri_removed = [r for r in removed if r["priority"]]
    report = {
        "chain": "Ethereum (仅ETH主网数据)",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "eth_price_usd": eth_price,
        "rule": f"移除: 持仓<{MIN_HOLD_USD} 或 30日交互额<{MIN_VOL30D_USD} (USD); 仅统计ETH主网主流代币",
        "summary": {
            "total": len(results), "kept": len(kept), "removed": len(removed),
            "removed_low_holdings": len(low_hold), "removed_low_volume": len(low_vol),
            "removed_priority_wallets": len(pri_removed),
            "kept_priority": sum(1 for r in kept if r["priority"]),
        },
        "kept_wallets": kept,
        "removed_wallets": removed,
        "note": "口径: 仅ETH主网; 持仓与交互额只统计主流代币白名单(含PT/YT/SY/LPT), 长尾代币忽略(tail_ignored标注); 30日未动的老持仓不可见; 无法定价代币按$0计(unpriced_tokens标注)",
    }
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    s = report["summary"]
    log("\n" + "=" * 62)
    log(f"过滤结果(仅ETH链口径): 总{s['total']} | 保留{s['kept']} | 移除{s['removed']}")
    log(f"  移除原因: 持仓不足{MIN_HOLD_USD}u的有{len(low_hold)}个, 30日交互不足{MIN_VOL30D_USD}u的有{len(low_vol)}个(可重叠)")
    log(f"  其中✪高优钱包被移除 {len(pri_removed)} 个 (详见 filter_report.json)")
    log(f"完整报告: {REPORT_FILE.name}")
    return report


def apply(results: list[dict]):
    kept_addrs = {r["address"] for r in results if r["keep"]}
    ts = time.strftime("%Y%m%d-%H%M%S")

    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    before = len(cfg["wallets"])
    CONFIG_FILE.with_name(f"config.backup-{ts}.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg["wallets"] = [w for w in cfg["wallets"] if w["address"] in kept_addrs]
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    if STATE_FILE.exists():
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        STATE_FILE.with_name(f"state.backup-{ts}.json").write_text(
            json.dumps(st, ensure_ascii=False), encoding="utf-8")
        st = {k: v for k, v in st.items() if k.split("_", 1)[-1] in kept_addrs}
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        log("state.json: 已清理被移除钱包的状态")

    pri = sum(1 for w in cfg["wallets"] if w.get("priority"))
    n_chains = len(cfg.get("chains", []))
    est = (pri * 144 + (len(cfg["wallets"]) - pri) * 48) * n_chains
    log(f"\n[已应用] config.json: {before} -> {len(cfg['wallets'])} 个钱包"
        f" (✪{pri} + 普通{len(cfg['wallets']) - pri})")
    log(f"  备份: config.backup-{ts}.json / state.backup-{ts}.json")
    log(f"  新预估日用量: ~{est} 次 API (预算 {cfg.get('daily_call_budget', 90000)})")


def main():
    global API_KEY
    load_dotenv(BASE_DIR / ".env")
    API_KEY = os.getenv("ETHERSCAN_API_KEY", "").strip()
    if not API_KEY:
        log("缺少 ETHERSCAN_API_KEY, 请先在 .env 填写")
        sys.exit(1)
    if not CONFIG_FILE.exists():
        log("缺少 config.json, 请先运行 import_wallets.py")
        sys.exit(1)
    if "--reset" in sys.argv and CACHE_FILE.exists():
        CACHE_FILE.unlink()
        log("已清除采集缓存, 将重新采集")

    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    wallets = cfg["wallets"]
    cache = load_cache()

    log(f"钱包过滤 | 规则: 主流代币口径下 持仓>={MIN_HOLD_USD}u 且 30日交互>={MIN_VOL30D_USD}u 才保留 | 仅统计ETH主网")
    log(f"钱包总数: {len(wallets)} | 模式: {'应用(--apply)' if '--apply' in sys.argv else '试运行(仅报告)'}")

    eth_price = get_eth_price(API_KEY)
    log(f"ETH 价格: ${eth_price:,.2f}")

    scan_activity(wallets, cache)
    resolve_prices(cache, eth_price)
    scan_balances(wallets, cache)
    results = evaluate(cache, eth_price)
    report = write_report(results, eth_price)

    if "--apply" in sys.argv:
        apply(results)
    else:
        log("\n当前为试运行, 确认报告无误后执行: python filter_wallets.py --apply")


if __name__ == "__main__":
    main()
