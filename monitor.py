# -*- coding: utf-8 -*-
"""
Pendle 聪明钱链上交易监控机器人（单文件版）

用法:
    python monitor.py             # 模式由 .env 的 RUN_MODE 决定 (loop=常驻 / once=单次)
    python monitor.py --loop      # 强制本地常驻轮询
    python monitor.py --once      # 强制单次执行（适配 GitHub Actions）
    python monitor.py --selftest  # 离线自检：内置样例数据验证聚合/去重/消息格式，无需任何 Key

架构:
    数据源   Etherscan V2 统一 API（一个 Key 查 Ethereum / Monad / Arbitrum 等所有链）
    判定条件 A  ERC20 符号特征匹配: PT- / YT- / SY- 前缀, 包含 PENDLE-LPT（默认启用，最准）
    判定条件 B  Pendle Router 交互匹配（config: enable_router_check，默认关闭省额度）
    聚合     同一 txHash 的所有代币转入/转出合并为 1 条 Telegram 通知
    防重     state.json: last_block + recent_txs(50)，重启不重复推送
    轮询     分级: ✪ 高优钱包每 tier1_interval_min 分钟，其余每 tier2_interval_min 分钟
"""

from __future__ import annotations

import html
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

try:
    import requests
    from dotenv import load_dotenv
except ImportError:
    print("缺少依赖，请先执行: pip install -r requirements.txt")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "monitor.log"
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"

# Windows 控制台重定向输出时避免 GBK 编码崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("pendle-monitor")


class EtherscanError(Exception):
    """Etherscan 请求多次失败"""


# ----------------------------------------------------------------------------
# 数据拉取模块 (fetcher)
# ----------------------------------------------------------------------------
class EtherscanClient:
    def __init__(self, api_key: str, sleep_sec: float = 0.25, daily_budget: int = 90000):
        self.api_key = api_key
        self.sleep_sec = sleep_sec
        self.daily_budget = daily_budget
        self.session = requests.Session()
        self._day = ""
        self._calls_today = 0
        self._head_cache: dict[int, tuple[float, int]] = {}  # chain_id -> (ts, head)

    # ---- 配额计数 ----
    def _tick_day(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self._calls_today = today, 0

    def calls_today(self) -> int:
        self._tick_day()
        return self._calls_today

    def over_budget(self) -> bool:
        return self.calls_today() >= self.daily_budget

    # ---- 底层请求（重试 + 限速 + 限流退避）----
    def _get(self, chain_id: int, params: dict) -> dict:
        query = {"chainid": chain_id, "apikey": self.api_key, **params}
        last_err: Exception | None = None
        for attempt in range(1, 4):
            try:
                resp = self.session.get(ETHERSCAN_V2_URL, params=query, timeout=10)
                self._tick_day()
                self._calls_today += 1
                time.sleep(self.sleep_sec)  # 严格 <=5 req/s
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                last_err = e
                log.warning(f"Etherscan 请求失败({attempt}/3): {e}")
                time.sleep(1.5 * attempt)
                continue

            # 免费限流 => 稍长退避后重试
            if str(data.get("status")) == "0":
                msg = f'{data.get("message", "")} {data.get("result", "")}'.lower()
                if "rate limit" in msg or "max rate" in msg:
                    log.warning("Etherscan 限流，退避 1.2s 重试...")
                    time.sleep(1.2)
                    continue
            return data
        raise EtherscanError(f"Etherscan 连续 3 次失败: {last_err}")

    @staticmethod
    def _rows(data: dict) -> list[dict]:
        res = data.get("result")
        return res if isinstance(res, list) else []  # "No transactions found" 等 => 空

    def tokentx(self, chain_id: int, address: str, startblock: int,
                max_pages: int = 3, page_size: int = 100) -> list[dict]:
        """ERC20 转账记录；翻页保护：单页满额才继续取（防高频钱包漏扫）"""
        rows: list[dict] = []
        for page in range(1, max_pages + 1):
            data = self._get(chain_id, {
                "module": "account", "action": "tokentx", "address": address,
                "startblock": startblock, "endblock": 99999999,
                "page": page, "offset": page_size, "sort": "desc",
            })
            batch = self._rows(data)
            rows.extend(batch)
            if len(batch) < page_size:
                break
        rows.sort(key=lambda t: int(t.get("blockNumber", 0)))  # 时间升序处理
        # 极端情况下跨页重叠的去重保护
        seen, deduped = set(), []
        for t in rows:
            k = (t.get("hash"), t.get("from", "").lower(), t.get("to", "").lower(),
                 t.get("contractAddress", "").lower(), t.get("value"))
            if k not in seen:
                seen.add(k)
                deduped.append(t)
        return deduped

    def txlist(self, chain_id: int, address: str, startblock: int) -> list[dict]:
        """普通交易记录（仅条件 B 开启时调用）"""
        data = self._get(chain_id, {
            "module": "account", "action": "txlist", "address": address,
            "startblock": startblock, "endblock": 99999999,
            "page": 1, "offset": 40, "sort": "desc",
        })
        return self._rows(data)

    def chain_head(self, chain_id: int) -> int:
        """当前链最新区块（5 分钟缓存；仅首次初始化无记录的钱包时用到）"""
        cached = self._head_cache.get(chain_id)
        if cached and time.time() - cached[0] < 300:
            return cached[1]
        data = self._get(chain_id, {"module": "proxy", "action": "eth_blockNumber"})
        try:
            head = int(str(data.get("result") or "0x0"), 16)
        except (TypeError, ValueError):
            head = 0
        self._head_cache[chain_id] = (time.time(), head)
        return head


# ----------------------------------------------------------------------------
# 轻量价格预言机（用于单笔交易 USD 规模过滤, 仅在命中 Pendle 交易时才调用）
# ----------------------------------------------------------------------------
STABLE_SYMBOLS_USD = {
    "USDC", "USDT", "DAI", "USDE", "SUSDE", "USDS", "SUSDS", "FDUSD", "TUSD", "PYUSD",
    "USD0", "AUSD", "SRUSDE", "SRUSDT", "SUSDT", "AUSDC", "GUSDC", "CRVUSD", "SDAI",
    "GHO", "SLVUSD", "SPLUSD", "SYRUPUSDC", "USDAI", "WUSD", "EUSD",
}
WETH_CONTRACTS = {  # chain_id -> WETH 合约（价格=原生币）
    1: "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    42161: "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",
}
NATIVE_CG_IDS = {1: "ethereum", 42161: "ethereum", 143: "monad"}


class PriceOracle:
    """价格来源: 稳定币≈$1 / WETH=原生币价(CoinGecko) / Pendle官方API(PT/YT/LPT/主流币, 仅eth+arb)"""

    TOKEN_TTL = 900   # 代币价格缓存 15 分钟
    NATIVE_TTL = 600  # 原生币价格缓存 10 分钟

    def __init__(self, etherscan_key: str = ""):
        self.es_key = etherscan_key
        self._tokens: dict[tuple, tuple[float, float | None]] = {}  # (chain,contract)->(ts,price)
        self._native: dict[int, tuple[float, float | None]] = {}    # chain->(ts,price)

    def _native_price(self, chain_id: int, etherscan_key: str = "") -> float | None:
        cached = self._native.get(chain_id)
        if cached and time.time() - cached[0] < self.NATIVE_TTL:
            return cached[1]
        price = None
        try:
            r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                             params={"ids": "ethereum,monad", "vs_currencies": "usd"}, timeout=10)
            if r.status_code == 200:
                price = (r.json() or {}).get(NATIVE_CG_IDS.get(chain_id, ""), {}).get("usd")
        except requests.RequestException:
            pass
        if price is None and chain_id in (1, 42161):  # 兜底: Etherscan ethprice
            try:
                r = requests.get(ETHERSCAN_V2_URL, params={
                    "chainid": 1, "module": "stats", "action": "ethprice", "apikey": etherscan_key}, timeout=10)
                price = float(r.json()["result"]["ethusd"])
            except Exception:
                price = None
        self._native[chain_id] = (time.time(), float(price) if price else None)
        return self._native[chain_id][1]

    def prices_for(self, chain_id: int, items: list[tuple[str, str]]) -> dict[str, float | None]:
        """items: [(contract, symbol)] -> {contract: usd价格或None}"""
        out: dict[str, float | None] = {}
        pendle_todo = []
        for contract, symbol in items:
            if not contract:
                continue
            s = (symbol or "").upper()
            key = (chain_id, contract)
            cached = self._tokens.get(key)
            if cached and time.time() - cached[0] < self.TOKEN_TTL:
                out[contract] = cached[1]
                continue
            if s in STABLE_SYMBOLS_USD or s.startswith(("USD", "SUSD", "AUSD", "GUSD")):
                out[contract] = 1.0
                self._tokens[key] = (time.time(), 1.0)
            elif contract == WETH_CONTRACTS.get(chain_id) or s == "WETH":
                p = self._native_price(chain_id, self.es_key)
                out[contract] = p
                self._tokens[key] = (time.time(), p)
            elif chain_id in (1, 42161):  # Pendle 官方价格（批量上限20）
                pendle_todo.append(contract)
            else:
                out[contract] = None
                self._tokens[key] = (time.time(), None)
        for i in range(0, len(pendle_todo), 20):
            chunk = pendle_todo[i:i + 20]
            fetched: dict[str, float | None] = {}
            try:
                r = requests.get(f"https://api-v2.pendle.finance/core/v1/{chain_id}/assets/prices",
                                 params={"addresses": ",".join(chunk)}, timeout=10)
                if r.status_code == 200:
                    prices = r.json().get("prices") or {}
                    fetched = {k.lower(): float(v) for k, v in prices.items()}
            except requests.RequestException as e:
                log.warning(f"Pendle价格请求失败: {e}")
            time.sleep(0.3)
            for c in chunk:
                p = fetched.get(c)
                out[c] = p
                self._tokens[(chain_id, c)] = (time.time(), p)
        return out


# ----------------------------------------------------------------------------
# Pendle 市场信息缓存（通知里展示 市场名/到期日/隐含APY; 仅 eth+arb, Monad 官方API未收录）
# ----------------------------------------------------------------------------
class MarketInfoCache:
    TTL = 900  # 市场列表缓存 15 分钟
    SUPPORTED_CHAINS = (1, 42161)

    def __init__(self):
        self._by_chain: dict[int, tuple[float, dict[str, dict]]] = {}

    def _load(self, chain_id: int) -> dict[str, dict]:
        cached = self._by_chain.get(chain_id)
        if cached and time.time() - cached[0] < self.TTL:
            return cached[1]
        index: dict[str, dict] = {}
        for page in range(3):  # 翻页保护: 最多 3 页
            try:
                r = requests.get(
                    f"https://api-v2.pendle.finance/core/v1/{chain_id}/markets/active",
                    params={"limit": 100, "skip": page * 100}, timeout=15)
                if r.status_code != 200:
                    log.warning(f"Pendle市场列表 {r.status_code}: {r.text[:80]}")
                    break
                markets = (r.json() or {}).get("markets", [])
            except requests.RequestException as e:
                log.warning(f"Pendle市场列表请求失败: {e}")
                break
            for m in markets:
                d = m.get("details") or {}
                apy = d.get("impliedApy")
                info = {"name": str(m.get("name") or ""), "expiry": str(m.get("expiry") or "")[:10],
                        "apy": float(apy) if isinstance(apy, (int, float)) else None}
                for key in ("address", "pt", "yt", "sy"):  # 市场合约=PENDLE-LPT
                    v = str(m.get(key) or "")
                    addr = v.split("-")[-1].lower()
                    if addr.startswith("0x") and len(addr) == 42:
                        index.setdefault(addr, info)
            if len(markets) < 100:
                break
            time.sleep(0.3)
        self._by_chain[chain_id] = (time.time(), index)
        return index

    def find(self, chain_id: int, contracts: list[str]) -> str | None:
        """按交易涉及的代币合约匹配市场, 返回 'sUSDE · 2026-12-26 · 隐含APY 8.97%' 或 None"""
        if chain_id not in self.SUPPORTED_CHAINS:
            return None
        index = self._load(chain_id)
        for c in contracts:
            info = index.get((c or "").lower())
            if info:
                apy = f"隐含APY {info['apy'] * 100:.2f}%" if info["apy"] is not None else "隐含APY 未知"
                expiry = f" · {info['expiry']}" if info["expiry"] else ""
                return f"{html.escape(info['name'])}{expiry} · {apy}"
        return None


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            bak = self.path.with_suffix(".json.bak")
            log.error(f"state.json 损坏({e})，已备份到 {bak.name} 并从空状态重建（首跑不推送历史）")
            try:
                self.path.rename(bak)
            except OSError:
                pass
            return {}

    def save(self) -> None:
        """原子写入，避免进程被杀时写坏文件"""
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, key: str) -> dict | None:
        return self.data.get(key)

    def set(self, key: str, value: dict) -> None:
        self.data[key] = value


# ----------------------------------------------------------------------------
# 解析与过滤引擎 (analyzer) —— 纯函数，selftest 直接复用
# ----------------------------------------------------------------------------
def is_pendle_symbol(symbol: str | None, prefixes: list[str]) -> bool:
    """条件 A: PT-/YT-/SY- 前缀 或 包含 PENDLE-LPT（忽略大小写）"""
    if not symbol:
        return False
    s = symbol.upper()
    return any(p.upper() in s or s.startswith(p.upper()) for p in prefixes)


def collect_new_groups(txs: list[dict], last_block: int, recent: set) -> list[tuple[str, list[dict]]]:
    """按 PRD 规则筛新交易并按 txHash 聚合:
    新交易 := blockNumber >= last_block 且 hash 不在 recent_txs 中"""
    groups: dict[str, list[dict]] = {}
    for tx in txs:
        h = tx.get("hash")
        if not h:
            continue
        if int(tx.get("blockNumber", 0)) >= last_block and h not in recent:
            groups.setdefault(h, []).append(tx)
    return sorted(groups.items(), key=lambda kv: (int(kv[1][0].get("blockNumber", 0)), kv[0]))


def _norm_decimals(tx: dict) -> int:
    try:
        d = int(str(tx.get("tokenDecimal", "")).strip() or 0)
        return d if d > 0 else 18
    except (TypeError, ValueError):
        return 18


def build_token_flows(txs: list[dict], wallet: str) -> tuple[dict, dict, dict]:
    """把同一 txHash 下与该钱包相关的所有 ERC20 变动汇总为 {符号: 数量}, 并返回 {符号: 合约}"""
    wallet = wallet.lower()
    ins: dict[str, Decimal] = {}
    outs: dict[str, Decimal] = {}
    sym2con: dict[str, str] = {}
    for tx in txs:
        symbol = (tx.get("tokenSymbol") or tx.get("tokenName")
                  or (tx.get("contractAddress") or "?")[:10]).strip() or "?"
        sym2con.setdefault(symbol, (tx.get("contractAddress") or "").lower())
        try:
            value = Decimal(str(tx.get("value", "0") or "0"))
        except Exception:
            value = Decimal(0)
        amount = value / (Decimal(10) ** _norm_decimals(tx))
        src, dst = tx.get("from", "").lower(), tx.get("to", "").lower()
        if dst == wallet:
            ins[symbol] = ins.get(symbol, Decimal(0)) + amount
        elif src == wallet:
            outs[symbol] = outs.get(symbol, Decimal(0)) + amount
        # 其余为与该钱包无关的中间转账，忽略
    return ins, outs, sym2con


def tx_usd_value(chain_id: int, txs: list[dict], wallet: str, oracle: PriceOracle) -> tuple[float, int]:
    """估算单笔交易对该钱包的 USD 规模 = max(转入合计, 转出合计), 仅统计可定价代币; 返回 (usd, 未定价腿数)"""
    wallet = wallet.lower()
    items = [((t.get("contractAddress") or "").lower(), t.get("tokenSymbol") or "") for t in txs]
    prices = oracle.prices_for(chain_id, items)
    in_usd = out_usd = 0.0
    unpriced = 0
    for t in txs:
        c = (t.get("contractAddress") or "").lower()
        if not c:
            continue
        dst, src = t.get("to", "").lower(), t.get("from", "").lower()
        if dst != wallet and src != wallet:
            continue  # 与该钱包无关的中间转账
        p = prices.get(c)
        if p is None:
            unpriced += 1
            continue
        try:
            amt = float(Decimal(str(t.get("value", "0") or "0")) / (Decimal(10) ** _norm_decimals(t)))
        except Exception:
            continue
        if dst == wallet:
            in_usd += amt * p
        else:
            out_usd += amt * p
    return max(in_usd, out_usd), unpriced


def derive_action(ins: dict, outs: dict, prefixes: list[str]) -> str:
    """动作标签智能识别（按 PRD 3.2，LP 判定优先）"""
    all_syms = list(ins) + list(outs)
    if any("PENDLE-LPT" in s.upper() for s in all_syms):
        return "💧 LP 流动性操作"
    in_pendle = [s for s in ins if is_pendle_symbol(s, prefixes)]
    if any(s.upper().startswith("PT-") for s in in_pendle):
        return "🟢 买入 PT (锁定固收)"
    if any(s.upper().startswith("YT-") for s in in_pendle):
        return "🔥 买入 YT (做多收益/积分)"
    if any(is_pendle_symbol(s, prefixes) for s in outs):
        return "🔴 卖出/赎回 PT/YT"
    return "🔄 Pendle 协议交互"


def fmt_amount(d: Decimal) -> str:
    s = f"{d:,.6f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def render_message(chain: dict, wallet: dict, tx_hash: str, ts: int,
                   ins: dict, outs: dict, action: str,
                   usd: tuple[float, int] | None = None,
                   market: str | None = None) -> str:
    addr = wallet["address"]
    short = f"{addr[:6]}...{addr[-4:]}"
    label = html.escape(str(wallet.get("label") or short))

    def section(flows: dict) -> str:
        if not flows:
            return "• —"
        return "\n".join(f"• <code>{fmt_amount(v)}</code> {html.escape(s)}" for s, v in flows.items())

    time_str = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    explorer = chain["explorer"].rstrip("/")
    scale_line = ""
    if usd is not None:  # (usd估值, 未定价代币腿数); 有未定价腿时为下界
        scale_line = f"💰 <b>规模：</b>{'≥' if usd[1] else '≈'}{usd[0]:,.0f} USD\n"
    market_line = f"📈 <b>市场：</b>{market}\n" if market else ""

    return (
        "🚨 <b>Pendle 聪明钱交易提醒</b>\n\n"
        f"👤 <b>钱包：</b>{label} (<code>{short}</code>)\n"
        f"⛓ <b>网络：</b>{html.escape(chain['name'])}\n"
        f"🎯 <b>动作：</b>{action}\n"
        f"{market_line}"
        f"{scale_line}\n"
        f"📤 <b>支出 (Out)：</b>\n{section(outs)}\n\n"
        f"📥 <b>获得 (In)：</b>\n{section(ins)}\n\n"
        f"🕒 <b>时间：</b>{time_str}\n"
        f'🔗 <a href="{explorer}/tx/{tx_hash}">查看交易详情</a> | '
        f'<a href="https://debank.com/profile/{addr}">DeBank 主页</a>'
    )


# ----------------------------------------------------------------------------
# Telegram 通知模块 (notifier)
# ----------------------------------------------------------------------------
class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, dry_run: bool = False):
        self.token = token.strip()
        self.chat_id = chat_id.strip()
        self.dry_run = dry_run

    def send(self, text: str) -> bool:
        if self.dry_run or not self.token or not self.chat_id:
            log.info("[DRY-RUN] 消息预览:\n" + text + "\n" + "-" * 50)
            return True
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }
        for attempt in range(1, 4):
            try:
                resp = requests.post(url, json=payload, timeout=10)
                if resp.status_code == 429:  # 频率限制
                    wait = 3
                    try:
                        wait = int(resp.json()["parameters"]["retry_after"])
                    except Exception:
                        pass
                    log.warning(f"Telegram 限流，等待 {wait}s 重试")
                    time.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    log.warning(f"Telegram 服务端错误 {resp.status_code}，重试({attempt}/3)")
                    time.sleep(2 * attempt)
                    continue
                data = resp.json()
                if data.get("ok"):
                    time.sleep(1.1)  # 同一会话 sendMessage 限频保护
                    return True
                log.error(f"Telegram 拒绝消息: {data.get('description')}")
                return False
            except requests.RequestException as e:
                log.warning(f"Telegram 请求异常({attempt}/3): {e}")
                time.sleep(2 * attempt)
        return False


# ----------------------------------------------------------------------------
# 单钱包扫描（fetcher + analyzer + notifier 串联）
# ----------------------------------------------------------------------------
def scan_wallet(chain: dict, wallet: dict, client: EtherscanClient,
                state: StateStore, cfg: dict, notifier: TelegramNotifier,
                oracle: PriceOracle | None = None,
                markets: MarketInfoCache | None = None) -> int:
    """扫描一个 (链, 钱包) 组合，返回发出的通知数。失败抛 EtherscanError 由上层跳过。"""
    key = f"{chain['chain_id']}_{wallet['address']}"
    prefixes = cfg["pendle_token_prefixes"]
    st = state.get(key)

    # ---- 首次运行：只记高度，不推历史（防历史消息轰炸）----
    if st is None:
        head_txs = client.tokentx(chain["chain_id"], wallet["address"],
                                  startblock=0, max_pages=1, page_size=1)
        if head_txs:
            last_block = max(int(t["blockNumber"]) for t in head_txs)
        else:  # 该链无任何代币记录 => 以链头为起点
            last_block = client.chain_head(chain["chain_id"])
        state.set(key, {"last_block": last_block, "recent_txs": []})
        state.save()
        log.info(f"[初始化] {wallet.get('label', '?')} @ {chain['name']} 起始块 {last_block}")
        return 0

    last_block = int(st.get("last_block", 0))
    recent = set(st.get("recent_txs", []))

    txs = client.tokentx(chain["chain_id"], wallet["address"],
                         startblock=last_block, max_pages=int(cfg["max_pages"]))
    max_block = last_block
    for t in txs:
        max_block = max(max_block, int(t.get("blockNumber", 0)))

    groups = collect_new_groups(txs, last_block, recent)

    # ---- 条件 B（可选）: Router 交互兜底 ----
    router_hashes: set[str] = set()
    if cfg.get("enable_router_check"):
        routers = {r.lower() for r in cfg.get("pendle_routers", [])}
        for t in client.txlist(chain["chain_id"], wallet["address"], startblock=last_block):
            max_block = max(max_block, int(t.get("blockNumber", 0)))
            if (t.get("to", "").lower() in routers and t.get("isError") == "0"
                    and t.get("hash") not in recent):
                router_hashes.add(t["hash"])

    # ---- 命中条件 A 或 B 的 txHash => 聚合发送（每个 hash 仅 1 条）----
    sent_ok, failed_hashes = True, set()
    count = 0
    for tx_hash, group in groups:
        hit_a = any(is_pendle_symbol(t.get("tokenSymbol"), prefixes) for t in group)
        if not (hit_a or tx_hash in router_hashes):
            continue
        ts = int(group[0].get("timeStamp", 0))
        ins, outs, _sym2con = build_token_flows(group, wallet["address"])
        action = derive_action(ins, outs, prefixes)
        usd_info = None
        min_usd = float(cfg.get("min_tx_usd") or 0)
        if min_usd > 0 and oracle is not None:
            usd_val, unpriced = tx_usd_value(chain["chain_id"], group, wallet["address"], oracle)
            if usd_val < min_usd:
                log.info(f"[小额过滤] {wallet.get('label', '?')} @ {chain['name']} {action} "
                         f"规模≈{usd_val:,.0f}u < {min_usd:,.0f}u 不通知 tx={tx_hash[:16]}...")
                continue
            usd_info = (usd_val, unpriced)
        market_info = None
        if markets is not None:
            market_info = markets.find(chain["chain_id"],
                                       [t.get("contractAddress", "") for t in group])
        msg = render_message(chain, wallet, tx_hash, ts, ins, outs, action, usd_info, market_info)
        if notifier.send(msg):
            count += 1
            log.info(f"[通知] {wallet.get('label', '?')} @ {chain['name']} {action} tx={tx_hash[:18]}...")
        else:
            sent_ok = False
            failed_hashes.add(tx_hash)

    # ---- 更新状态：发送失败的哈希不记入，留待下一轮重发；有失败则不推进块高 ----
    new_hashes = [h for h, _ in groups if h not in failed_hashes]
    st["recent_txs"] = (st.get("recent_txs", []) + new_hashes)[-int(cfg["recent_txs_cap"]):]
    if sent_ok:
        st["last_block"] = max_block
    state.set(key, st)
    if new_hashes or sent_ok:
        state.save()
    return count


def scan_batch(chains: list[dict], wallets: list[dict], client: EtherscanClient,
               state: StateStore, cfg: dict, notifier: TelegramNotifier,
               oracle: PriceOracle | None = None,
               markets: MarketInfoCache | None = None) -> int:
    """扫描一批钱包 × 所有链；单钱包异常不中断整轮"""
    total = 0
    for chain in chains:
        for wallet in wallets:
            if client.over_budget():
                log.error(f"已达每日 API 保护阈值 {cfg['daily_call_budget']}，本轮中止，明日自动恢复")
                return total
            try:
                total += scan_wallet(chain, wallet, client, state, cfg, notifier, oracle, markets)
            except EtherscanError as e:
                log.warning(f"跳过 {wallet.get('label', '?')} @ {chain['name']}: {e}（下轮自动补扫）")
            except Exception:
                log.exception(f"扫描异常: {wallet.get('label', '?')} @ {chain['name']}")
    return total


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log.error(f"找不到 {CONFIG_FILE.name}，请先运行: python import_wallets.py")
        sys.exit(1)
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    for k, default in {
        "pendle_token_prefixes": ["PT-", "YT-", "SY-", "PENDLE-LPT"],
        "enable_router_check": False,
        "tier1_interval_min": 10, "tier2_interval_min": 30,
        "api_sleep_sec": 0.25, "recent_txs_cap": 50,
        "max_pages": 3, "daily_call_budget": 90000, "min_tx_usd": 0,
    }.items():
        cfg.setdefault(k, default)
    if not cfg.get("chains") or not cfg.get("wallets"):
        log.error("config.json 中缺少 chains 或 wallets，请先运行 import_wallets.py")
        sys.exit(1)
    return cfg


def run(mode: str) -> None:
    load_dotenv(BASE_DIR / ".env")
    cfg = load_config()

    api_key = os.getenv("ETHERSCAN_API_KEY", "").strip()
    dry_run = os.getenv("DRY_RUN", "0").strip() in ("1", "true", "True")
    notifier = TelegramNotifier(os.getenv("TG_BOT_TOKEN", ""),
                                os.getenv("TG_CHAT_ID", ""), dry_run)
    if not api_key:
        log.error("缺少 ETHERSCAN_API_KEY：请复制 .env.example 为 .env 并填写后重试"
                  "（离线验证逻辑请用 python monitor.py --selftest，无需任何 Key）")
        sys.exit(1)

    client = EtherscanClient(api_key, float(cfg["api_sleep_sec"]), int(cfg["daily_call_budget"]))
    oracle = PriceOracle(api_key)
    markets = MarketInfoCache()
    state = StateStore(STATE_FILE)

    tier1 = [w for w in cfg["wallets"] if w.get("priority")]
    tier2 = [w for w in cfg["wallets"] if not w.get("priority")]
    est = (len(tier1) * 144 + len(tier2) * 48) * len(cfg["chains"])
    log.info(
        f"启动 | 链: {','.join(c['name'] for c in cfg['chains'])} | "
        f"✪高优 {len(tier1)} 个/{cfg['tier1_interval_min']}min · 普通 {len(tier2)} 个/{cfg['tier2_interval_min']}min | "
        f"条件B: {'开' if cfg['enable_router_check'] else '关'} | "
        f"规模过滤: {'≥' + str(cfg['min_tx_usd']) + 'u 才通知' if cfg.get('min_tx_usd') else '关'} | "
        f"DRY_RUN: {dry_run} | 预估日用量 ~{est} 次 (预算 {cfg['daily_call_budget']})"
    )

    if mode == "once":
        t0 = time.time()
        n = scan_batch(cfg["chains"], cfg["wallets"], client, state, cfg, notifier, oracle, markets)
        log.info(f"单轮完成: 扫描 {len(cfg['wallets'])} 钱包 × {len(cfg['chains'])} 链，"
                 f"通知 {n} 条，用时 {time.time() - t0:.0f}s，API {client.calls_today()} 次")
        return

    # loop 模式：双日程表驱动分级轮询
    schedule = {"tier1": 0.0, "tier2": 0.0}
    intervals = {"tier1": int(cfg["tier1_interval_min"]) * 60, "tier2": int(cfg["tier2_interval_min"]) * 60}
    log.info("进入常驻轮询模式，Ctrl+C 退出（状态已实时保存）")
    try:
        while True:
            now = time.time()
            for tier in ("tier1", "tier2"):
                if now < schedule[tier]:
                    continue
                wallets = tier1 if tier == "tier1" else tier2
                if not wallets:
                    schedule[tier] = now + intervals[tier]
                    continue
                if client.over_budget():
                    log.error("已达每日 API 保护阈值，本轮休眠 30 分钟")
                    schedule[tier] = time.time() + 1800
                    continue
                t0 = time.time()
                n = scan_batch(cfg["chains"], wallets, client, state, cfg, notifier, oracle, markets)
                log.info(f"[{tier}] 完成 {len(wallets)} 钱包 | 通知 {n} 条 | "
                         f"{time.time() - t0:.0f}s | 今日API {client.calls_today()} 次")
                schedule[tier] = time.time() + intervals[tier]
            time.sleep(1)
    except KeyboardInterrupt:
        state.save()
        log.info("收到退出信号，状态已保存，再见 👋")


# ----------------------------------------------------------------------------
# 离线自检（无需 API Key / Telegram，验证 PRD 验收标准 2/3/4 的核心逻辑）
# ----------------------------------------------------------------------------
def selftest() -> None:
    cfg = {"pendle_token_prefixes": ["PT-", "YT-", "SY-", "PENDLE-LPT"]}
    chain = {"name": "Arbitrum", "chain_id": 42161, "explorer": "https://arbiscan.io"}
    wallet = {"address": "0x1234567890abcdef1234567890abcdef12345678", "label": "测试巨鲸A✪"}
    W = wallet["address"]
    ROUTER = "0x888888888889758f76e7103c6cbf23abbf58f946"

    _CONTRACTS: dict[str, str] = {}  # 每个符号分配一个不同的伪合约地址(价格按合约映射)

    def _contract_of(sym: str) -> str:
        if sym not in _CONTRACTS:
            _CONTRACTS[sym] = "0x" + f"{len(_CONTRACTS) + 1:040x}"
        return _CONTRACTS[sym]

    def tx(h, blk, src, dst, val, sym, dec, ts=1762900000):
        return {"hash": h, "blockNumber": str(blk), "timeStamp": str(ts),
                "from": src, "to": dst, "value": str(val),
                "tokenSymbol": sym, "tokenDecimal": str(dec),
                "contractAddress": _contract_of(sym)}

    txs = [
        # T1 同一 hash 两笔变动: 转出 25,000 USDC + 转入 485,210.55 YT-sUSDE => 应合1条·买入YT
        tx("0xT1", 1001, W, ROUTER, 25_000 * 10**6, "USDC", 6),
        tx("0xT1", 1001, ROUTER, W, Decimal("485210.55") * 10**18, "YT-sUSDE-26DEC2026", 18),
        # T2 噪音: 普通 USDC->USDT 兑换，不应通知
        tx("0xT2", 1002, W, "0x" + "a" * 40, 1000 * 10**6, "USDC", 6),
        tx("0xT2", 1002, "0x" + "a" * 40, W, 1000 * 10**6, "USDT", 6),
        # T3 LP: 转出 USDC + 转入 PENDLE-LPT => 💧 LP 操作
        tx("0xT3", 1003, W, "0x" + "b" * 40, 1000 * 10**6, "USDC", 6),
        tx("0xT3", 1003, "0x" + "b" * 40, W, 999000, "PENDLE-LPT", 18),
    ]

    results, errors = [], []

    def check(name, cond, detail=""):
        (results if cond else errors).append(name + (f" [{detail}]" if detail else ""))

    # 第一轮: last_block=1000, recent 为空
    groups = collect_new_groups(txs, 1000, set())
    check("筛选出 3 个新 txHash 分组", len(groups) == 3, f"got {len(groups)}")
    check("T1 组含 2 条转账(按hash聚合)", len(dict(groups)["0xT1"]) == 2)

    hits = [(h, g) for h, g in groups
            if any(is_pendle_symbol(t.get("tokenSymbol"), cfg["pendle_token_prefixes"]) for t in g)]
    check("条件A命中 T1/T3, 排除噪音 T2", [h for h, _ in hits] == ["0xT1", "0xT3"])

    for h, g in hits:
        ins, outs, _ = build_token_flows(g, W)
        action = derive_action(ins, outs, cfg["pendle_token_prefixes"])
        print("\n" + "=" * 62)
        print(render_message(chain, wallet, h, int(g[0]["timeStamp"]), ins, outs, action))
        if h == "0xT1":
            check("T1 动作=买入YT", action.startswith("🔥"), action)
            check("T1 OUT 仅USDC且金额25000", list(outs) == ["USDC"] and fmt_amount(outs["USDC"]) == "25,000")
            check("T1 IN 为YT且金额485,210.55", fmt_amount(ins["YT-sUSDE-26DEC2026"]) == "485,210.55")
        if h == "0xT3":
            check("T3 动作=LP操作", action.startswith("💧"), action)

    # 模拟第二轮(状态已推进): 同一批数据 => 0 命中（验收4 防重）
    recent2 = ["0xT1", "0xT2", "0xT3"]
    groups2 = collect_new_groups(txs, 1003, set(recent2))
    check("第二轮同数据 0 条新通知(防重)", len(groups2) == 0, f"got {len(groups2)}")

    # 同区块多笔: last_block=1001 时, T2/T3 仍应被发现而 T1 被 recent 挡住
    groups3 = collect_new_groups(txs, 1001, {"0xT1"})
    check("同区块多笔不漏(block>=last 且 recent 去重)", [h for h, _ in groups3] == ["0xT2", "0xT3"])

    # ---- USD 规模过滤逻辑 (min_tx_usd) ----
    class StubOracle:
        def prices_for(self, chain_id, items):
            out = {}
            for c, sym in items:
                s = (sym or "").upper()
                if s == "USDC":
                    out[c] = 1.0
                elif s.startswith("YT-"):
                    out[c] = 0.02
                else:
                    out[c] = None
            return out

    oracle = StubOracle()
    u1, up1 = tx_usd_value(42161, dict(groups)["0xT1"], W, oracle)
    check("T1 规模=25,000u(OUT侧定价)", abs(u1 - 25000) < 1 and up1 == 0, f"{u1:,.0f}u 未定价{up1}腿")
    u3, up3 = tx_usd_value(42161, dict(groups)["0xT3"], W, oracle)
    check("T3 规模=1,000u(LPT腿未定价计下界)", abs(u3 - 1000) < 1 and up3 == 1, f"{u3:,.0f}u 未定价{up3}腿")
    small = [
        tx("0xT4", 1004, W, "0x" + "d" * 40, 300 * 10**6, "USDC", 6),
        tx("0xT4", 1004, "0x" + "d" * 40, W, Decimal(15000) * 10**18, "YT-sUSDE-26DEC2026", 18),
    ]
    u4, up4 = tx_usd_value(42161, small, W, oracle)
    check("T4 小额~300u 应低于500u阈值", u4 < 500, f"{u4:,.0f}u")
    msg4 = render_message(chain, wallet, "0xT4", 1762900000,
                          *build_token_flows(small, W)[:2], "🔥 买入 YT (做多收益/积分)", (u4, up4))
    check("消息含规模行(≈全定价)", "≈300 USD" in msg4)

    # ---- 市场隐含APY 行 ----
    msg5 = render_message(chain, wallet, "0xT5", 1762900000,
                          *build_token_flows(small, W)[:2], "🟢 买入 PT (锁定固收)",
                          (25000, 0), "sUSDE · 2026-12-26 · 隐含APY 8.97%")
    check("消息含市场隐含APY行", "📈 <b>市场：</b>sUSDE · 2026-12-26 · 隐含APY 8.97%" in msg5)
    check("无市场信息时不显示该行", "📈" not in msg4)

    print("\n" + "=" * 62)
    for r in results:
        print(f"  ✅ {r}")
    if errors:
        for e in errors:
            print(f"  ❌ {e}")
        sys.exit(1)
    print(f"自检全部通过 ({len(results)} 项) ✅")


if __name__ == "__main__":
    arg = sys.argv[1].lstrip("-") if len(sys.argv) > 1 else ""
    if arg == "selftest":
        selftest()
    elif arg in ("loop", "once"):
        run(arg)
    else:
        run(os.getenv("RUN_MODE", "loop").strip().lower() or "loop")
