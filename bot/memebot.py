#!/usr/bin/env python3
"""
Lunyx meme bot v3 — multi-strategy Solana meme-coin trader for a Fomo (or any Solana) wallet.

  * Scans ~150-300 Solana pools a minute (DexScreener boosts/profiles + GeckoTerminal
    trending and top-volume pools) and keeps following them for 6 hours.
  * Three strategies — momentum, dip-buy in an uptrend, volume breakout — each with
    its own entry rules and exits (strategy.py).
  * Risk-based sizing, market-regime gate, adaptive allocation per strategy,
    loss-streak pause, daily loss limit, drawdown kill switch.
  * Every buy passes on-chain + RugCheck safety checks first.
  * Records every scan to data/ so replay.py can test and tune settings on real history.

Modes (config.json -> "mode"):
  paper : real market data, fake SOL, nothing signed (default)
  live  : signs real swaps — also requires the --live flag on the command line

Usage:
  python memebot.py               run (paper)
  python memebot.py --live        run live (config mode must also be "live")
  python memebot.py --status      balance, positions, per-strategy results
  python memebot.py --scan        one scan: what each strategy sees right now, no trading
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # bundled Python does not add the script folder
import argparse
import base64
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from collections import defaultdict, deque

import model as MODEL
import edge as EDGE
import lessons as LESSONS
import narrative as NARR
import social as SOCIAL
import ai_brain as AI
import smartmoney as SMART
import strategy as S
from strategy import SOL_MINT

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"
LOG_PATH = HERE / "memebot.log"

LAMPORTS = 1_000_000_000
DEX = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2/networks/solana"
RUGCHECK = "https://api.rugcheck.xyz/v1"

log = logging.getLogger("memebot")
http = requests.Session()
http.headers["User-Agent"] = "memebot/3.0"


# ============================================================================ setup

def apply_patches():
    """Strategy improvements shipped by updates (patches.json), applied once to the user's own config.json."""
    pf, df = HERE / "patches.json", HERE / "patches_done.json"
    try:
        patches = json.loads(pf.read_text()) if pf.exists() else []
        done = set(json.loads(df.read_text())) if df.exists() else set()
    except Exception:
        return
    todo = [p for p in patches if p.get("id") not in done and p.get("file") == "config.json"
            and (str(p.get("path", "")).startswith("strategies.") or p.get("path") in ("autotune", "strategy_priority"))]
    if not todo or not CONFIG_PATH.exists():
        return
    cfg = json.loads(CONFIG_PATH.read_text())
    for p in todo:
        d = cfg
        parts = p["path"].split(".")
        for k in parts[:-1]:
            d = d.setdefault(k, {})
        d[parts[-1]] = p["value"]
        done.add(p["id"])
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    df.write_text(json.dumps(sorted(done)))


def load_config():
    apply_patches()
    over = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    cfg = S.deep_merge(S.DEFAULTS, over)
    if cfg["mode"] not in ("paper", "live"):
        sys.exit("config: mode must be 'paper' or 'live'")
    return cfg


def load_env():
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_keypair():
    from solders.keypair import Keypair
    raw = os.getenv("SOLANA_PRIVATE_KEY", "").strip()
    if not raw:
        sys.exit("live mode needs SOLANA_PRIVATE_KEY in .env")
    if raw.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(raw)))
    return Keypair.from_base58_string(raw)


def notify(cfg, title, msg, tags=""):
    """Push to phone via ntfy.sh (free app: subscribe to the topic in config.json)."""
    topic = cfg.get("ntfy_topic")
    if not topic:
        return
    try:
        http.post(f"https://ntfy.sh/{topic}", data=msg.encode(), timeout=10,
                  headers={"Title": title.encode("ascii", "ignore").decode(), "Tags": tags})
    except Exception as e:
        log.debug("notify failed: %s", e)


def utcnow():
    return datetime.now(timezone.utc)


# ============================================================================ chain

class Solana:
    def __init__(self, url):
        self.url = url
        self._id = 0

    def call(self, method, params):
        self._id += 1
        r = http.post(self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}, timeout=20)
        r.raise_for_status()
        j = r.json()
        if "error" in j:
            raise RuntimeError(f"RPC {method}: {j['error']}")
        return j["result"]

    def sol_balance(self, owner):
        return self.call("getBalance", [owner, {"commitment": "confirmed"}])["value"] / LAMPORTS

    def token_balance(self, owner, mint) -> tuple[int, int]:
        """(raw amount, decimals) across all token accounts for this mint."""
        res = self.call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        amt, dec = 0, 0
        for acc in res["value"]:
            info = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            amt += int(info["amount"])
            dec = info["decimals"]
        return amt, dec

    def mint_info(self, mint):
        res = self.call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        v = res.get("value")
        if not v:
            return None
        return v["data"]["parsed"]["info"]

    def send(self, tx_b64):
        return self.call("sendTransaction", [tx_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3,
                                                      "preflightCommitment": "confirmed"}])

    def confirm(self, sig, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            st = self.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": False}])["value"][0]
            if st:
                if st.get("err"):
                    return False, st["err"]
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True, None
            time.sleep(2)
        return False, "timeout"



# ============================================================================ market data

_pair_cache: dict = {}


def dex_get(path):
    r = http.get(DEX + path, timeout=15)
    r.raise_for_status()
    return r.json()


def discover_dex() -> list[str]:
    mints = []
    for path in ("/token-boosts/top/v1", "/token-boosts/latest/v1", "/token-profiles/latest/v1"):
        try:
            data = dex_get(path)
        except Exception as e:
            log.debug("dexscreener %s failed: %s", path, e)
            continue
        for t in data if isinstance(data, list) else []:
            if t.get("chainId") == "solana" and t.get("tokenAddress"):
                mints.append(t["tokenAddress"])
    return mints


GECKO_PATHS = ["/trending_pools?page=1&duration=5m", "/trending_pools?page=1&duration=1h",
               "/pools?page=1&sort=h24_volume_usd_desc", "/trending_pools?page=2&duration=1h",
               "/pools?page=2&sort=h24_volume_usd_desc", "/trending_pools?page=1&duration=6h",
               # wider net = more real coins recorded = more evidence for every strategy test
               "/trending_pools?page=3&duration=1h", "/pools?page=3&sort=h24_volume_usd_desc",
               "/trending_pools?page=2&duration=6h", "/pools?page=1&sort=h24_tx_count_desc",
               "/pools?page=2&sort=h24_tx_count_desc", "/trending_pools?page=2&duration=5m"]
_gecko = {"i": 0, "until": 0.0}


def discover_gecko(per_call=2) -> list[str]:
    """GeckoTerminal allows ~30 calls/min — fetch 2 lists per scan, rotating; tracked coins persist 6 h."""
    mints = []
    if time.time() < _gecko["until"]:
        return mints
    for _ in range(per_call):
        path = GECKO_PATHS[_gecko["i"] % len(GECKO_PATHS)]
        _gecko["i"] += 1
        try:
            r = http.get(GECKO + path, headers={"Accept": "application/json;version=20230302"}, timeout=15)
            if r.status_code == 429:
                _gecko["until"] = time.time() + 120
                log.debug("gecko rate-limited, backing off 2 min")
                break
            if r.status_code != 200:
                continue
            for p in r.json().get("data", []):
                rel = p.get("relationships", {})
                base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
                quote = ((rel.get("quote_token") or {}).get("data") or {}).get("id", "")
                for tid in (base, quote):
                    m = tid.split("_", 1)[-1]
                    if m and m != SOL_MINT and m not in S.STABLES:
                        mints.append(m)
                        break
        except Exception as e:
            log.debug("gecko %s failed: %s", path, e)
    return mints


def discover() -> list[str]:
    seen, out = set(), []
    for m in discover_dex() + discover_gecko():
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def best_pairs(mints: list[str], max_age=3.0) -> dict:
    """Most-liquid SOL/USD pair per mint, cached for a few seconds."""
    now = time.time()
    out, need = {}, []
    for m in dict.fromkeys(mints):
        c = _pair_cache.get(m)
        if c and now - c[0] < max_age:
            if c[1]:
                out[m] = c[1]
        else:
            need.append(m)
    for i in range(0, len(need), 30):
        chunk = need[i:i + 30]
        try:
            pairs = dex_get("/tokens/v1/solana/" + ",".join(chunk))
        except Exception as e:
            log.debug("pairs lookup failed: %s", e)
            continue
        got = {}
        for p in pairs or []:
            m = p.get("baseToken", {}).get("address")
            if m not in chunk:
                continue
            if p.get("quoteToken", {}).get("address") not in ({SOL_MINT} | S.STABLES) and m != SOL_MINT:
                continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if m not in got or liq > ((got[m].get("liquidity") or {}).get("usd") or 0):
                got[m] = p
        for m in chunk:
            _pair_cache[m] = (now, got.get(m))
            if m in got:
                out[m] = got[m]
    return out


def rugcheck(mint):
    try:
        r = http.get(f"{RUGCHECK}/tokens/{mint}/report/summary", timeout=15)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


_safety_cache: dict = {}


def safety_screen(cfg, chain, mint, sym, light=False) -> tuple[bool, str]:
    """On-chain + RugCheck checks, cached 30 min. light=True: for fresh coins, RugCheck's generic danger flags
    (which count the trading pool as a holder, and call every new pool 'low liquidity') are replaced by the
    pool-aware holder check; mint/freeze authority and creators with rugged tokens still block."""
    key = ("light:" if light else "") + mint
    c = _safety_cache.get(key)
    if c and time.time() - c[0] < 1800:
        return c[1]
    res = _safety(cfg, chain, mint, sym, light)
    _safety_cache[key] = (time.time(), res)
    if light:
        return res
    try:   # share verdicts with the edge check (it skips coins the bot would never buy)
        p = HERE / "data" / "safety.json"
        d = json.loads(p.read_text()) if p.exists() else {}
        d[mint] = {"ok": res[0], "why": res[1], "t": int(time.time())}
        p.write_text(json.dumps(d))
    except Exception:
        pass
    return res


def _safety(cfg, chain, mint, sym, light=False):
    if cfg["require_revoked_authorities"]:
        try:
            info = chain.mint_info(mint)
        except Exception as e:
            return False, f"{sym}: mint lookup failed ({e})"
        if not info:
            return False, f"{sym}: mint not found"
        if info.get("mintAuthority"):
            return False, f"{sym}: dev can still mint more supply"
        if info.get("freezeAuthority"):
            return False, f"{sym}: dev can freeze your tokens"
    rc = rugcheck(mint)
    if rc and light:
        if any("rugged" in (r.get("name") or "").lower() for r in rc.get("risks", [])):
            return False, f"{sym}: creator has rugged tokens before"
        rc = None                        # the rest is judged by the pool-aware holder check below
    if rc:
        score = rc.get("score_normalised", rc.get("score", 0)) or 0
        danger = [r.get("name") for r in rc.get("risks", []) if r.get("level") == "danger"]
        if danger:
            return False, f"{sym}: RugCheck danger — {', '.join(danger[:3])}"
        if score > cfg["max_rugcheck_score"]:
            return False, f"{sym}: RugCheck score {score} too risky"
        for r in rc.get("risks", []):
            if "holder" in (r.get("name") or "").lower():
                try:
                    if float(str(r.get("value", "")).rstrip("%")) > cfg["max_top_holder_pct"]:
                        return False, f"{sym}: {r['name']} {r['value']}"
                except ValueError:
                    pass
        lp = rc.get("lpLockedPct")
        if lp is not None and lp < cfg["min_lp_locked_pct"]:
            return False, f"{sym}: only {lp:.0f}% of liquidity locked/burned"
    if cfg.get("holder_check", {}).get("enabled", True):
        ok, why = holder_check(cfg, mint, sym)
        if not ok:
            return False, why
    return True, "safe"


def holder_check(cfg, mint, sym):
    """Who owns the coin: dev bag, insider/bundled wallets, a few wallets holding most of the supply.
    Pools and lockers are not counted as holders. Unknown data = pass (the other checks still apply)."""
    h = cfg.get("holder_check", {})
    try:
        r = http.get(f"{RUGCHECK}/tokens/{mint}/report", timeout=20)
        d = r.json() if r.status_code == 200 else None
    except Exception:
        d = None
    if not d:
        return True, "holder data unavailable"
    if d.get("rugged"):
        return False, f"{sym}: RugCheck marks it as rugged"
    known = d.get("knownAccounts") or {}
    pools = {a for a, k in known.items() if (k or {}).get("type") in ("AMM", "LOCKER")}
    pools |= {m.get("pubkey") for m in d.get("markets") or []}
    holders = [x for x in d.get("topHolders") or [] if x.get("owner") not in pools and x.get("address") not in pools]
    creator = d.get("creator")
    if creator:
        cp = sum(float(x.get("pct") or 0) for x in holders if x.get("owner") == creator)
        if cp > h.get("max_creator_pct", 10):
            return False, f"{sym}: the dev still holds {cp:.0f}% of the supply (can dump on buyers)"
    ins = sum(float(x.get("pct") or 0) for x in holders if x.get("insider"))
    if ins > h.get("max_insider_pct", 15):
        return False, f"{sym}: linked insider/bundled wallets hold {ins:.0f}%"
    if holders:
        top1 = float(holders[0].get("pct") or 0)
        top10 = sum(float(x.get("pct") or 0) for x in holders[:10])
        if top1 > h.get("max_single_holder_pct", 12):
            return False, f"{sym}: one wallet holds {top1:.0f}%"
        if top10 > h.get("max_top10_pct", 40):
            return False, f"{sym}: top 10 wallets hold {top10:.0f}% (excluding the pool)"
    n = d.get("totalHolders")
    if n is not None and n < h.get("min_holders", 50):
        return False, f"{sym}: only {n} holders"
    return True, "holders ok"


def record(cfg, pairs: dict, kind="scan"):
    """Append a market snapshot per pair — the dataset replay.py tunes on."""
    if not cfg.get("record_data") or not pairs:
        return
    d = HERE / "data"
    d.mkdir(exist_ok=True)
    ts = int(time.time())
    with open(d / f"snapshots-{utcnow():%Y%m%d}.jsonl", "a") as f:
        for mint, p in pairs.items():
            f.write(json.dumps({"ts": ts, "kind": kind, "mint": mint, "symbol": p.get("baseToken", {}).get("symbol"),
                                "quote": p.get("quoteToken", {}).get("address"), "price": p.get("priceUsd"),
                                "liq": (p.get("liquidity") or {}).get("usd"), "mcap": p.get("marketCap") or p.get("fdv"),
                                "vol": p.get("volume"), "chg": p.get("priceChange"), "txns": p.get("txns"),
                                "created": p.get("pairCreatedAt")}) + "\n")


# ============================================================================ trading

class Jupiter:
    def __init__(self, cfg):
        self.base = cfg["jupiter_url"].rstrip("/")
        self.cfg = cfg
        key = os.getenv("JUPITER_API_KEY")
        if key:
            http.headers["x-api-key"] = key

    def quote(self, in_mint, out_mint, amount_raw, slippage_bps=None):
        r = http.get(f"{self.base}/quote", params={
            "inputMint": in_mint, "outputMint": out_mint, "amount": str(int(amount_raw)),
            "slippageBps": slippage_bps or self.cfg["slippage_bps"], "restrictIntermediateTokens": "true"}, timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"Jupiter quote {r.status_code}: {r.text[:200]}")
        return r.json()

    def swap_tx(self, quote, user_pubkey):
        r = http.post(f"{self.base}/swap", json={
            "quoteResponse": quote, "userPublicKey": user_pubkey, "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
                "maxLamports": self.cfg["priority_fee_lamports"], "priorityLevel": "veryHigh"}}}, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"Jupiter swap {r.status_code}: {r.text[:200]}")
        return r.json()["swapTransaction"]


_jup_px = {}


def jup_prices(mints, max_age=2.0):
    """USD prices from Jupiter's free price API (updates every few seconds, much fresher than DexScreener).
    Returns {mint: usd}; missing coins are simply absent (caller falls back to DexScreener)."""
    now = time.time()
    need = [m for m in dict.fromkeys(mints) if now - _jup_px.get(m, (0, None))[0] > max_age]
    for i in range(0, len(need), 50):
        chunk = need[i:i + 50]
        try:
            r = http.get("https://lite-api.jup.ag/price/v3", params={"ids": ",".join(chunk)}, timeout=8)
            data = r.json() if r.status_code == 200 else {}
        except Exception:
            data = {}
        for mnt in chunk:
            px = (data.get(mnt) or {}).get("usdPrice")
            _jup_px[mnt] = (now, float(px) if px else None)
    return {mnt: _jup_px[mnt][1] for mnt in mints if _jup_px.get(mnt, (0, None))[1]}


class PaperQuotes:
    """Jupiter-shaped quotes for paper mode without a Jupiter key: prices from Jupiter's price API
    (DexScreener as fallback), a 0.3% pool fee, and price impact from the pool's liquidity."""

    def __init__(self, chain: Solana):
        self.chain = chain
        self.decimals = {SOL_MINT: 9}

    def _dec(self, mint):
        if mint not in self.decimals:
            info = self.chain.mint_info(mint)
            self.decimals[mint] = int(info["decimals"]) if info else 6
        return self.decimals[mint]

    def quote(self, in_mint, out_mint, amount_raw, slippage_bps=None):
        token = out_mint if in_mint == SOL_MINT else in_mint
        pairs = best_pairs([token, SOL_MINT])
        tp, sp = pairs.get(token), pairs.get(SOL_MINT)
        if not tp or not sp or not tp.get("priceUsd"):
            raise RuntimeError("no price for token")
        tok_usd, sol_usd_px = float(tp["priceUsd"]), float(sp["priceUsd"])
        jp = jup_prices([token, SOL_MINT])
        tok_usd, sol_usd_px = jp.get(token) or tok_usd, jp.get(SOL_MINT) or sol_usd_px
        liq = ((tp.get("liquidity") or {}).get("usd") or 0) or 1
        if in_mint == SOL_MINT:
            usd = amount_raw / LAMPORTS * sol_usd_px
        else:
            usd = amount_raw / 10 ** self._dec(token) * tok_usd
        impact = min(usd / (liq / 2), 0.5)
        usd_out = usd * (1 - 0.003) * (1 - impact)
        out = usd_out / tok_usd * 10 ** self._dec(token) if in_mint == SOL_MINT else usd_out / sol_usd_px * LAMPORTS
        return {"outAmount": str(int(out)), "priceImpactPct": str(impact), "paper": True}



class Bot:
    def __init__(self, cfg, live_flag=False):
        self.cfg = cfg
        load_env()
        if cfg["mode"] == "live" and not live_flag:
            sys.exit('config says "live" — add --live on the command line to confirm real trading')
        if live_flag and cfg["mode"] != "live":
            sys.exit('--live given but config.json mode is "paper"')
        if cfg["mode"] == "live":
            for ch in S.apply_live_guard(cfg):
                log.warning("live guard: %s", ch)
            if not cfg.get("live_budget_sol"):
                sys.exit('live mode needs "live_budget_sol" in config.json: the most SOL the bot may ever use')
            if not os.getenv("JUPITER_API_KEY"):
                sys.exit("live mode needs JUPITER_API_KEY in .env (free at portal.jup.ag)")
        self.chain = Solana(os.getenv("RPC_URL") or cfg["rpc_url"])
        if cfg["mode"] == "paper" and not os.getenv("JUPITER_API_KEY"):
            self.jup = PaperQuotes(self.chain)
        else:
            self.jup = Jupiter(cfg)
        self.kp = load_keypair() if cfg["mode"] == "live" else None
        self.owner = str(self.kp.pubkey()) if self.kp else None
        self.state = self.load_state()
        self.regime = {"risk_off": False, "breadth": 0.5, "sol_h1": None, "active": 0}
        self.pred = MODEL.Predictor()
        self.edge = EDGE.EdgeGate()
        self.shadow_logged = {}
        self.social = SOCIAL.SocialRadar(cfg, log, lambda t, msg, tag="": notify(self.cfg, t, msg, tag))
        self.narr = NARR.NarrativeRadar(log, lambda t, msg, tag="": notify(self.cfg, t, msg, tag))
        self.ai = AI.AIBrain(cfg, log)
        self.smart = SMART.SmartMoney(cfg, self.chain, log)
        self.mhist = defaultdict(lambda: deque(maxlen=31))   # per-coin minute history for look-ahead features
        self.rebuild_history()

    def rebuild_history(self, minutes=35):
        """After a restart, refill each coin's last 30+ minutes of prices from the recorded data,
        so the model and the price checks don't go blind for 10 minutes."""
        try:
            files = sorted((HERE / "data").glob("snapshots-*.jsonl"))
            if not files:
                return
            f = files[-1]
            size = f.stat().st_size
            with open(f, "rb") as fh:
                fh.seek(max(0, size - 12_000_000))
                lines = fh.read().decode(errors="ignore").splitlines()[1:]
            cutoff = time.time() - minutes * 60
            n = 0
            for line in lines:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("kind") not in ("scan", "position") or r["ts"] < cutoff or not r.get("price"):
                    continue
                f_ = S.features({"baseToken": {"symbol": r.get("symbol")}, "quoteToken": {"address": r.get("quote")},
                                 "priceUsd": r["price"], "liquidity": {"usd": r.get("liq")}, "marketCap": r.get("mcap"),
                                 "volume": r.get("vol"), "priceChange": r.get("chg"), "txns": r.get("txns"),
                                 "pairCreatedAt": r.get("created")}, r["ts"] * 1000)
                minute = r["ts"] // 60 * 60
                h = self.mhist[r["mint"]]
                if not h or h[-1][0] < minute:
                    h.append((minute, f_["price"], f_["vol_h1"], f_["ratio"]))
                    n += 1
            log.info("restored %d minutes of price history for %d coins", n, len(self.mhist))
        except Exception as e:
            log.debug("history rebuild failed: %s", e)

    # ---------------------------------------------------------------- state
    def load_state(self):
        st = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
        st.setdefault("paper_sol", self.cfg["paper_start_sol"])
        for k, v in (("positions", {}), ("cooldown", {}), ("trades", []), ("results", []), ("watch", {}),
                     ("tracked", {}), ("bench", {}), ("equity_history", [])):
            st.setdefault(k, v)
        for k in ("start_equity", "peak_equity", "day", "day_start", "halted_until", "pause_until"):
            st.setdefault(k, None)
        st.setdefault("killed", False)
        st.setdefault("loss_streak", 0)
        st["watch"] = {m: w for m, w in st["watch"].items() if isinstance(w, dict) and "strategy" in w}
        if self.cfg["mode"] == "paper":
            ref = st.get("paper_start_ref")
            if ref is None:
                st["paper_start_ref"] = self.cfg["paper_start_sol"]
            elif abs(ref - self.cfg["paper_start_sol"]) > 1e-6 and not st["positions"]:
                # demo balance changed in Settings: start the paper money over from it (like "Restart demo")
                log.warning("demo balance changed %.4f → %.4f SOL — starting the demo over", ref, self.cfg["paper_start_sol"])
                st["paper_sol"] = self.cfg["paper_start_sol"]
                st["paper_start_ref"] = self.cfg["paper_start_sol"]
                for k in ("start_equity", "peak_equity", "day", "day_start", "halted_until", "pause_until"):
                    st[k] = None
                st.update(killed=False, equity_history=[], loss_streak=0)
            if st["killed"] and not st["positions"] and not st["results"]:
                st["killed"] = False             # a stop with no trades behind it was a false alarm
                st["start_equity"] = st["peak_equity"] = None
        for p in st["positions"].values():  # upgrade v2 positions
            p.setdefault("strategy", "momentum")
            p.setdefault("realized_sol", 0.0)
            p.setdefault("cost_total", p.get("cost_sol", 0))
        return st

    def save(self):
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(STATE_PATH)

    # ---------------------------------------------------------------- balances
    def sol(self):
        if self.cfg["mode"] == "paper":
            return self.state["paper_sol"]
        bal = self.chain.sol_balance(self.owner)
        budget = self.cfg.get("live_budget_sol")
        if budget:
            # the bot only ever uses its budget, however much SOL the wallet holds;
            # profits it makes grow the budget, losses shrink it
            in_coins = sum(p["cost_sol"] for p in self.state["positions"].values())
            realized = sum(r["pnl_sol"] for r in self.state["results"])
            return max(0.0, min(bal, budget + realized - in_coins))
        return bal

    def token_raw(self, mint, pos):
        if self.cfg["mode"] == "paper":
            return pos["raw"]
        raw, _ = self.chain.token_balance(self.owner, mint)
        return raw

    def value_sol(self, mint, raw):
        if raw <= 0:
            return 0.0, None
        q = self.jup.quote(mint, SOL_MINT, raw)
        return int(q["outAmount"]) / LAMPORTS, q

    def equity(self):
        return self.sol() + sum(p.get("last_value_sol", p["cost_sol"]) for p in self.state["positions"].values())

    # ---------------------------------------------------------------- swaps
    def execute(self, quote):
        if self.cfg["mode"] == "paper":
            return True, "paper"
        tx_b64 = self.jup.swap_tx(quote, self.owner)
        from solders.transaction import VersionedTransaction
        raw = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        signed = VersionedTransaction(raw.message, [self.kp])
        sig = self.chain.send(base64.b64encode(bytes(signed)).decode())
        ok, err = self.chain.confirm(sig)
        return ok, sig if ok else f"{sig} failed: {err}"

    def buy(self, mint, pair, name, reason, spend, stop_pct=None):
        sym = pair["baseToken"]["symbol"]
        q = self.jup.quote(SOL_MINT, mint, int(spend * LAMPORTS))
        impact = float(q.get("priceImpactPct") or 0) * 100
        if impact > self.cfg["max_price_impact_pct"]:
            log.info("skip %s: price impact %.1f%%", sym, impact)
            return False
        if self.cfg["mode"] == "live":
            # re-check right before signing: price must not have run away since the scan
            fresh = best_pairs([mint], max_age=0).get(mint)
            if fresh and pair.get("priceUsd"):
                move = (float(fresh["priceUsd"]) / float(pair["priceUsd"]) - 1) * 100
                if abs(move) > 5:
                    log.info("skip %s: price moved %+.1f%% since the scan", sym, move)
                    return False
        try:
            back = self.jup.quote(mint, SOL_MINT, int(int(q["outAmount"]) * 0.98))
            rt = int(back["outAmount"]) / LAMPORTS / (spend * 0.98)
        except Exception as e:
            log.info("skip %s: can't get a sell quote (%s) — possible honeypot", sym, str(e)[:60])
            return False
        if rt < 1 - self.cfg.get("max_round_trip_loss_pct", 8) / 100:
            log.info("skip %s: buying and selling right away would lose %.0f%% (hidden tax / thin route)", sym, (1 - rt) * 100)
            return False
        ok, ref = self.execute(q)
        if not ok:
            log.warning("BUY %s failed: %s", sym, ref)
            return False
        raw = int(q["outAmount"])
        if self.cfg["mode"] == "paper":
            self.state["paper_sol"] -= spend + self.cfg.get("paper_tx_fee_sol", 0.00015)
        else:
            time.sleep(2)
            raw = self.token_raw(mint, {}) or raw
        self.state["positions"][mint] = {
            "symbol": sym, "strategy": name, "raw": raw, "cost_sol": spend, "cost_total": spend, "realized_sol": 0.0,
            "entry_value_sol": spend, "peak_value_sol": spend, "last_value_sol": spend,
            "opened": utcnow().isoformat(timespec="seconds"), "opened_ts": time.time(),
            "entry_liq_usd": (pair.get("liquidity") or {}).get("usd") or 0, "took_profit": False,
            "pair_url": pair.get("url"), "stop_pct": stop_pct,
            "entry": LESSONS.entry_snapshot(S.features(pair)),
            "themes": sorted(self.narr.themes(sym, pair.get("baseToken", {}).get("name"))) if hasattr(self, "narr") else []}
        log.info("BUY  %s  %.4f SOL — %s  [%s]", sym, spend, reason, ref)
        self.trade("buy", sym, mint, spend, None, reason, ref, name)
        notify(self.cfg, f"BUY {sym} [{name}] ({self.cfg['mode']})",
               f"{spend:.4f} SOL\n{reason.split(': ', 1)[-1]}\n{pair.get('url') or ''}", "green_circle")
        return True

    def sell(self, mint, fraction, reason):
        p = self.state["positions"][mint]
        held = self.token_raw(mint, p)
        raw = held if fraction >= 0.999 else int(held * fraction)
        if raw <= 0:
            log.warning("%s: nothing to sell, dropping position", p["symbol"])
            self.state["positions"].pop(mint)
            return
        got, q = self.value_sol(mint, raw)
        ok, ref = self.execute(q)
        tries = 0
        while not ok and self.cfg["mode"] == "live" and tries < 3:
            tries += 1
            slip = self.cfg["slippage_bps"] + 300 * tries
            log.warning("SELL %s failed (%s) — retry %d with %.1f%% slippage", p["symbol"], ref, tries, slip / 100)
            time.sleep(2)
            q = self.jup.quote(mint, SOL_MINT, raw, slippage_bps=slip)
            got = int(q["outAmount"]) / LAMPORTS
            ok, ref = self.execute(q)
        if not ok:
            log.error("SELL %s failed after retries: %s — will try again next check", p["symbol"], ref)
            notify(self.cfg, f"SELL {p['symbol']} FAILED", f"{reason}\n{ref}\nThe bot keeps retrying every few seconds.", "warning")
            return
        part = raw / held
        cost_part = p["cost_sol"] * part
        pnl = got - cost_part
        if self.cfg["mode"] == "paper":
            self.state["paper_sol"] += got - self.cfg.get("paper_tx_fee_sol", 0.00015)
        p["realized_sol"] += got
        log.info("SELL %s  %d%% → %.4f SOL (P&L %+.4f SOL, %+.0f%%) [%s] — %s  [%s]", p["symbol"], round(part * 100),
                 got, pnl, (got / cost_part - 1) * 100 if cost_part else 0, p["strategy"], reason, ref)
        self.trade("sell", p["symbol"], mint, got, pnl, reason, ref, p["strategy"])
        if part >= 0.999:
            total = p["realized_sol"] - p["cost_total"]
            ret = total / p["cost_total"] * 100 if p["cost_total"] else 0
            self.state["results"].append({"strategy": p["strategy"], "ret_pct": ret, "pnl_sol": total,
                                          "symbol": p["symbol"], "time": utcnow().isoformat(timespec="seconds"),
                                          "entry": p.get("entry"), "exit": reason,
                                          "held_min": round((time.time() - (p.get("opened_ts") or time.time())) / 60, 1)})
            self.state["results"] = self.state["results"][-500:]
            self.state["loss_streak"] = self.state["loss_streak"] + 1 if total < 0 else 0
            self.state["positions"].pop(mint)
            self.state["cooldown"][mint] = (utcnow() + timedelta(hours=self.cfg["rebuy_cooldown_hours"])).isoformat()
            self.after_close(p["strategy"])
        else:
            p["raw"] = held - raw
            p["cost_sol"] -= cost_part
            p["peak_value_sol"] *= (1 - part)
            p["entry_value_sol"] *= (1 - part)
            p["last_value_sol"] = p.get("last_value_sol", 0) * (1 - part)
        notify(self.cfg, f"SELL {p['symbol']} {pnl:+.4f} SOL ({self.cfg['mode']})",
               f"{round(part * 100)}% sold for {got:.4f} SOL [{p['strategy']}]\n{reason}\nBalance ≈ {self.equity():.4f} SOL",
               "moneybag" if pnl > 0 else "red_circle")

    def after_close(self, name):
        c = self.cfg
        last = self.state["results"][-1] if self.state["results"] else None
        if last:
            won = last["pnl_sol"] > 0
            log.info("trade review: %s %s %+.1f%% (%s) — re-checking the evidence with this result included",
                     "WIN" if won else "LOSS", last["symbol"], last["ret_pct"], last.get("exit", ""))
            mins = c.get("autotune_minutes", 30) * 60
            if time.time() - self.state.get("last_autotune", 0) > 600:      # not more than once per 10 min
                self.state["last_autotune"] = time.time() - mins + 90
        if last and name in c["strategies"] and name not in ("hype", "predict"):
            rule = LESSONS.learn(c, self.state["results"], name)
            if rule:
                LESSONS.save_rules(CONFIG_PATH, c)
                log.warning("lesson learned — %s (%s)", rule["text"], rule["why"])
                notify(c, "Bot learned a lesson", f"{rule['text']}\n{rule['why']}", "brain")
        st = S.strategy_stats(c, self.state["results"], name)
        if S.benched(c, st) and name not in self.state["bench"]:
            until = time.time() + c["adapt_bench_hours"] * 3600
            self.state["bench"][name] = until
            log.warning("benching [%s] for %dh — last %d trades average %+.1f%%", name, c["adapt_bench_hours"],
                        st["n"], st["expectancy"])
            notify(c, f"Strategy {name} benched", f"Average {st['expectancy']:+.1f}% over {st['n']} trades. "
                                                  f"Paused {c['adapt_bench_hours']}h.", "warning")
        if self.state["loss_streak"] >= c["max_consecutive_losses"]:
            self.state["pause_until"] = time.time() + c["loss_streak_pause_minutes"] * 60
            self.state["loss_streak"] = 0
            log.warning("%d losses in a row — no new buys for %d min", c["max_consecutive_losses"],
                        c["loss_streak_pause_minutes"])

    def trade(self, side, sym, mint, sol, pnl, reason, ref, strat):
        self.state["trades"].append({"time": utcnow().isoformat(timespec="seconds"), "side": side, "symbol": sym,
                                     "mint": mint, "sol": sol, "pnl_sol": pnl, "reason": reason, "tx": ref,
                                     "strategy": strat})
        self.state["trades"] = self.state["trades"][-1000:]

    # ---------------------------------------------------------------- risk
    def risk_ok(self):
        s, c = self.state, self.cfg
        eq = self.equity()
        today = utcnow().date().isoformat()
        if s["start_equity"] is None:
            s["start_equity"] = s["peak_equity"] = eq
        if s["day"] != today:
            s["day"], s["day_start"] = today, eq
        if s["halted_until"] and today >= s["halted_until"]:
            s["halted_until"] = None
            log.info("new day — trading resumed")
        s["peak_equity"] = max(s["peak_equity"], eq)
        if s["killed"]:
            return False
        drop = eq <= s["peak_equity"] * (1 - c["max_drawdown_pct"] / 100)
        if drop and (not (s["results"] or s["positions"]) or time.time() - s.get("dd_seen", time.time()) < 60):
            # never on a balance with no trading behind it; and the drop must still be there a minute later
            # (a price glitch on one coin must not sell everything)
            s.setdefault("dd_seen", time.time())
            if not (s["results"] or s["positions"]):
                s["peak_equity"] = eq
            drop = False
        elif not drop:
            s.pop("dd_seen", None)
        if drop:
            log.error("max drawdown %.0f%% hit — selling everything and stopping for good", c["max_drawdown_pct"])
            notify(c, "Bot stopped", f"Max drawdown {c['max_drawdown_pct']}% hit. Sold everything.", "warning")
            self.sell_all("kill switch")
            s["killed"] = True
            return False
        if eq - s["day_start"] <= -c["max_daily_loss_sol"]:
            if not s["halted_until"]:
                log.warning("daily loss limit %.3f SOL hit — no new buys until tomorrow (UTC)", c["max_daily_loss_sol"])
                notify(c, "Daily loss limit hit", "No new buys until tomorrow (UTC).", "warning")
                s["halted_until"] = (utcnow().date() + timedelta(days=1)).isoformat()
            return False
        if s["halted_until"]:
            return False
        if s["pause_until"] and time.time() < s["pause_until"]:
            return False
        return True

    def sell_all(self, reason):
        for mint in list(self.state["positions"]):
            try:
                self.sell(mint, 1.0, reason)
            except Exception as e:
                log.error("could not sell %s: %s", self.state["positions"][mint]["symbol"], e)

    # ---------------------------------------------------------------- loops
    def manage(self):
        c = self.cfg
        if not self.state["positions"]:
            return
        pairs = best_pairs(list(self.state["positions"]))
        if time.time() - self.state.get("last_pos_record", 0) >= 60:
            record(c, pairs, "position")
            self.state["last_pos_record"] = time.time()
        for mint in list(self.state["positions"]):
            p = self.state["positions"][mint]
            try:
                val, _ = self.value_sol(mint, self.token_raw(mint, p))
            except Exception as e:
                log.warning("%s: price check failed (%s)", p["symbol"], e)
                continue
            p["last_value_sol"] = val
            p["peak_value_sol"] = max(p["peak_value_sol"], val)
            s = c["strategies"].get(p["strategy"]) or c["strategies"]["momentum"]
            if p.get("stop_pct"):
                s = dict(s, stop_loss_pct=p["stop_pct"])     # this coin's own volatility-based stop
            opened = p.get("opened_ts") or datetime.fromisoformat(p["opened"]).timestamp()
            liq = ((pairs.get(mint) or {}).get("liquidity") or {}).get("usd")
            frac, why = S.exit_step(c, s, {"entry": p["entry_value_sol"], "peak": p["peak_value_sol"], "opened": opened,
                                           "took_profit": p["took_profit"], "entry_liq": p["entry_liq_usd"]},
                                    val, liq, time.time())
            if not frac and c.get("ai", {}).get("exits", True):
                v = self.ai.get(mint)
                if v and v.get("sell_now") and v["t"] > opened + 60:
                    frac, why = 1.0, f"AI exit: {v['reason']} (danger {v['danger']}/10)"
            if frac:
                if frac < 0.999:
                    p["took_profit"] = True
                self.sell(mint, frac, why)

    def scan(self, trade=True):
        c = self.cfg
        now = time.time()
        found = discover()
        tracked = self.state["tracked"]
        known = {((self.state.get("symbols") or {}).get(m) or "").upper() for m in tracked}
        for sym in self.social.discoveries(known):
            try:
                res = dex_get(f"/latest/dex/search?q={sym}")
                for p in (res or {}).get("pairs", [])[:5]:
                    if p.get("chainId") == "solana" and (p.get("baseToken", {}).get("symbol") or "").upper() == sym:
                        found.append(p["baseToken"]["address"])
                        log.info("social discovery: $%s is buzzing — now tracking it", sym)
                        break
            except Exception as e:
                log.debug("social search %s failed: %s", sym, e)
        maj = c.get("majors") or {}
        majors = set((maj.get("tokens") or {}).values()) if maj.get("enabled") else set()
        for m in list(found) + list(majors):
            tracked[m] = now
        for m in [m for m, t in tracked.items() if now - t > 6 * 3600]:
            tracked.pop(m)
        if len(tracked) > c["track_max"]:
            for m in sorted(tracked, key=tracked.get)[:len(tracked) - c["track_max"]]:
                tracked.pop(m)
        pairs = best_pairs(list(tracked) + [SOL_MINT], max_age=20)
        sol_pair = pairs.pop(SOL_MINT, None)
        self.last_pairs = pairs
        for mm, pp in pairs.items():
            bt = pp.get("baseToken", {})
            self.narr.observe(mm, bt.get("symbol"), bt.get("name"), (pp.get("priceChange") or {}).get("h1"), now)
        syms = self.state.setdefault("symbols", {})
        for mm, pp in pairs.items():
            syms[mm] = pp.get("baseToken", {}).get("symbol")
        for mm in [k for k in syms if k not in tracked]:
            syms.pop(mm)
        minute = int(now // 60)
        if self.state.get("last_record_min") != minute:
            self.state["last_record_min"] = minute
            record(c, pairs)
            if sol_pair:
                record(c, {SOL_MINT: sol_pair}, "sol")
        self.regime = S.regime(c, pairs, sol_pair)
        self.state["sol_usd"] = float(sol_pair["priceUsd"]) if sol_pair else self.state.get("sol_usd")

        cd = {m: t for m, t in self.state["cooldown"].items() if datetime.fromisoformat(t).timestamp() > now}
        self.state["cooldown"] = cd
        self.state["bench"] = {k: v for k, v in self.state["bench"].items() if v > now}
        allowed = {n for n in c["strategies"] if n not in self.state["bench"]}
        # falling market: everything still allowed, at half size (risk_off_size_mult)

        # look-ahead model: score every coin on where it is likely to be in 15 minutes
        self.pred.refresh()
        mm0 = self.pred.m or {}
        ml_gate = self.pred.active or (c["mode"] == "paper" and c.get("model_gate_in_demo", True) and mm0.get("w")
                                        and mm0.get("gate") is not None and (mm0.get("auc") or 0) >= 0.55)
        minute = int(now // 60 * 60)
        probs = {}
        for mint, pair in pairs.items():
            f = S.features(pair, now * 1000)
            h = self.mhist[mint]
            past = [x for x in h if x[0] < minute]
            if ml_gate and f["price"] > 0:
                try:
                    probs[mint] = self.pred.score(MODEL.vector(f, past, self.regime["breadth"]))
                except Exception as e:
                    log.debug("score failed: %s", e)
            if not h or h[-1][0] < minute:
                h.append((minute, f["price"], f["vol_h1"], f["ratio"]))
        self.probs = probs

        watch = self.state["watch"]
        ready, seen = [], {}
        gated = {n for n in allowed if not self.edge.allows(c, n)}
        for mint, pair in pairs.items():
            if mint in self.state["positions"] or mint in cd:
                continue
            sc = _safety_cache.get(mint)
            if sc and now - sc[0] < 1800 and not sc[1][0]:
                watch.pop(mint, None)
                continue                          # failed the safety checks recently: don't re-watch it every minute
            if mint in majors:
                action, name, why, w = S.entry_step(c, pair, watch.get(mint), now, allowed,
                                                    base=maj.get("base"), priority=maj.get("priority"))
                if name:
                    why = why.replace(f"[{name}]", f"[{name}] (regular)", 1)
            else:
                action, name, why, w = S.entry_step(c, pair, watch.get(mint), now, allowed)
            if action == "BUY" and name in gated:
                if now - self.shadow_logged.get(mint, 0) > 1800:
                    self.shadow_logged[mint] = now
                    log.info("shadow (not bought): %s — %s is not proven: %s", why.split(" | ")[0][:120], name,
                             self.edge.why(name))
                watch.pop(mint, None)
                continue
            if action in ("NONE", "DROP"):
                watch.pop(mint, None)
                if action == "DROP":
                    log.info(why)
                continue
            seen[mint] = (name, why)
            if action == "WATCH":
                if mint not in watch or watch[mint].get("strategy") != name:
                    log.info("watching %s", why.split(", liq")[0])
                watch[mint] = w
            elif action == "BUY":
                watch.pop(mint, None)
                ready.append((mint, pair, name, why))
        for m in [m for m in watch if m not in pairs]:
            watch.pop(m)

        hs = c["strategies"].get("hype", {})
        if hs.get("enabled") and "hype" in allowed:
            coins = {m: (p.get("baseToken", {}).get("symbol"), p.get("baseToken", {}).get("name")) for m, p in pairs.items()}
            hits = self.social.vip_hits(coins, now)
            best = {}   # many copycat tokens share a name: only the deepest pool counts
            for mint, (who, text, url, ts) in hits.items():
                sym = (coins[mint][0] or "").upper()
                liq = (pairs[mint].get("liquidity") or {}).get("usd") or 0
                if sym not in best or liq > best[sym][1]:
                    best[sym] = (mint, liq, who, text)
            taken = {r[0] for r in ready}
            for sym, (mint, liq, who, text) in best.items():
                if mint in taken or mint in self.state["positions"] or mint in cd:
                    continue
                pair = pairs[mint]
                f = S.features(pair, now * 1000)
                if not S.base_ok(c, f)[0] or f["liq"] < hs["min_liquidity_usd"]:
                    continue
                if f["m5"] is not None and f["m5"] > hs["max_m5_pct"]:
                    log.info("hype skip %s: already up %+.0f%% in 5 min after %s's post", f["sym"], f["m5"], who)
                    continue
                why = f'[hype] {f["sym"]}: {who} posted "{text[:80]}", liq ${f["liq"]:,.0f}, 5m {f["m5"]:+.0f}%'
                ready.append((mint, pair, "hype", why))
                seen[mint] = ("hype", why)

        if self.pred.active:
            mm = self.pred.m
            sp = c["strategies"].get("predict", {})
            if c.get("model_gate", True):
                kept = []
                for r in ready:
                    pr = probs.get(r[0])
                    if pr is not None and pr < mm["gate"]:
                        log.info("model veto %s: %.0f%% chance of +8%% before -6%% (below average)", r[1]["baseToken"]["symbol"], pr * 100)
                    else:
                        kept.append(r)
                ready = kept
            if sp.get("enabled") and "predict" in allowed:
                taken = {r[0] for r in ready}
                for mint, pr in sorted(probs.items(), key=lambda kv: -kv[1]):
                    if pr < mm["threshold"]:
                        break
                    if mint in taken or mint in self.state["positions"] or mint in cd:
                        continue
                    if len(self.mhist.get(mint, ())) < sp.get("min_history_minutes", 10):
                        continue
                    pair = pairs[mint]
                    f = S.features(pair, now * 1000)
                    if not S.base_ok(c, f)[0]:
                        continue
                    why = (f"[predict] {f['sym']}: model {pr * 100:.0f}% chance of +8% before -6% in 15 min "
                           f"(its top picks hit {mm['precision']:.0%} on unseen data), liq ${f['liq']:,.0f}, 1h {f['h1']:+.0f}%")
                    ready.append((mint, pair, "predict", why))
                    seen[mint] = ("predict", why)
            ready.sort(key=lambda r: -(probs.get(r[0]) or 0))

        sm = c.get("smart_money", {})
        if self.smart.on:
            for mint, pair in pairs.items():
                pc = (pair.get("priceChange") or {}).get("h6")
                created = (pair.get("pairCreatedAt") or 0) / 1000
                if (pc is not None and pc >= sm.get("learn_min_h6_pct", 150) and created and now - created < 12 * 3600
                        and ((pair.get("liquidity") or {}).get("usd") or 0) >= sm.get("learn_min_liq_usd", 20000)):
                    self.smart.queue_learn(mint, created, pair.get("baseToken", {}).get("symbol"))
            ss = c["strategies"].get("smart", {})
            hot = self.smart.hot()
            if hot and ss.get("enabled") and "smart" in allowed:
                taken = {r[0] for r in ready}
                missing = [m for m in hot if m not in pairs]
                extra = best_pairs(missing) if missing else {}
                for mint, buyers in hot.items():
                    if mint in taken or mint in self.state["positions"] or mint in cd or mint in majors:
                        continue
                    pair = pairs.get(mint) or extra.get(mint)
                    if not pair:
                        continue
                    f = S.features(pair, now * 1000)
                    if not S.base_ok(c, f)[0] or f["liq"] < ss.get("min_liquidity_usd", 30000):
                        continue
                    if f["h1"] is not None and f["h1"] > ss.get("max_h1_pct", 300):
                        log.info("smart skip %s: already up %+.0f%% in 1h — smart wallets may be selling into it", f["sym"], f["h1"])
                        continue
                    if f["m5"] is not None and f["m5"] > ss.get("max_m5_pct", 20):
                        log.info("smart skip %s: already up %+.0f%% in 5 min", f["sym"], f["m5"])
                        continue
                    pairs.setdefault(mint, pair)
                    why = (f"[smart] {f['sym']}: {len(buyers)} smart-money wallets bought in the last "
                           f"{sm.get('window_min', 20)} min, liq ${f['liq']:,.0f}, 1h {f['h1'] or 0:+.0f}%")
                    ready.append((mint, pair, "smart", why))
                    seen[mint] = ("smart", why)

        if not self.pred.active and ml_gate and c.get("model_gate", True):
            kept = []
            for r in ready:
                pr = probs.get(r[0])
                if pr is not None and pr < mm0["gate"] and r[2] not in ("hype", "smart"):
                    log.info("AI model veto %s: rates it in its bottom 40%% (%.0f%% chance of +8%% before -6%%)",
                             r[1]["baseToken"]["symbol"], pr * 100)
                else:
                    kept.append(r)
            ready = kept

        if self.ai.on:
            try:
                rank = sorted(pairs.items(), key=lambda kv: -((kv[1].get("volume") or {}).get("h1") or 0))
                held = {m: p for m, p in self.state["positions"].items() if m not in majors}
                hp = {**best_pairs(list(held)), **{m: pairs[m] for m in held if m in pairs}} if held else {}
                first = [(m, hp[m]) for m in held if m in hp] + \
                        [kv for kv in pairs.items() if kv[0] in {r[0] for r in ready} or kv[0] in watch]
                pick, seen_m = [], set()
                for mint, pair in first + rank:
                    if mint in seen_m or mint in majors:
                        continue
                    seen_m.add(mint)
                    f = S.features(pair, now * 1000)
                    bt = pair.get("baseToken", {})
                    pick.append({"mint": mint, "sym": f["sym"], "name": bt.get("name"),
                                 "age_min": round(f["age_min"] or 0), "liq_usd": round(f["liq"] or 0),
                                 "mcap_usd": round(f["mcap"] or 0), "m5_pct": f["m5"], "h1_pct": f["h1"], "h6_pct": f["h6"],
                                 "vol_h1_usd": round(f["vol_h1"] or 0), "buys_m5": f["buys"], "sells_m5": f["sells"],
                                 "social_buzz": self.social.buzzing(bt.get("symbol"), now),
                                 "themes": sorted(self.narr.themes(bt.get("symbol"), bt.get("name"), now))[:3],
                                 "bot_signal": next((r[2] for r in ready if r[0] == mint), None),
                                 "smart_wallets_buying": len(self.smart.buyers(mint)) if self.smart.on else 0})
                    if mint in held:
                        hpz = held[mint]
                        pick[-1].update(held=True, pnl_pct=round(((hpz.get("last_value_sol") or hpz["entry_value_sol"])
                                                                  / hpz["entry_value_sol"] - 1) * 100, 1),
                                        held_min=round((now - (hpz.get("opened_ts") or now)) / 60))
                themes = [w for w, t in sorted(self.narr.alerted.items(), key=lambda kv: -kv[1]) if now - t < 3 * 3600]
                try:
                    themes += [str(e.get("name")) for e in self.narr.active_events(now)]
                except Exception:
                    pass
                self.ai.observe(pick, list(self.social.vip_posts), themes,
                                {"risk_off": self.regime["risk_off"], "breadth": round(self.regime["breadth"], 2),
                                 "sol_h1_pct": self.regime.get("sol_h1")})
            except Exception as e:
                log.debug("ai observe failed: %s", e)

        if not trade:
            return seen, len(pairs)

        bought = []
        if not self.pred.active:
            ready.sort(key=lambda r: -((r[1].get("volume") or {}).get("h1") or 0))
        for mint, pair, name, why in ready:
            if len(self.state["positions"]) >= c["max_positions"]:
                break
            bt = pair.get("baseToken", {})
            mine = self.narr.themes(bt.get("symbol"), bt.get("name"), now)
            counts = {}
            for p in self.state["positions"].values():
                for t in set(p.get("themes") or []) & mine:
                    counts[t] = counts.get(t, 0) + 1
            full = [t for t, k in counts.items() if k >= c.get("max_per_theme", 2)]
            if full:
                log.info("skip %s: already holding %d '%s' coins (they rise and fall together)", bt.get("symbol"),
                         counts[full[0]], full[0])
                continue
            # spike guard: don't buy the top of a sudden candle (OGTA +10% in 2 min, then −11% seconds after buying)
            h = [x for x in self.mhist.get(mint, []) if x[0] >= now - 180]
            px_now = float(pair.get("priceUsd") or 0)
            if h and px_now and min(x[1] for x in h if x[1]) > 0:
                jump = (px_now / min(x[1] for x in h if x[1]) - 1) * 100
                if jump > c.get("max_jump_2min_pct", 8):
                    log.info("skip %s: jumped %+.0f%% in the last 2-3 min — buying a spike top usually loses",
                             pair["baseToken"]["symbol"], jump)
                    continue
            if mint not in majors:
                ok, swhy = safety_screen(c, self.chain, mint, pair["baseToken"]["symbol"],
                                         light=bool((c["strategies"].get(name) or {}).get("light_safety")))
                if not ok:
                    log.info(swhy)
                    continue
            av = self.ai.veto(mint) if mint not in majors else None
            if av:
                log.info("AI veto %s: danger %d/10 — %s", pair["baseToken"]["symbol"], av["danger"], av["reason"])
                continue
            st = S.strategy_stats(c, self.state["results"], name)
            mult = S.size_mult(c, st) * (c["risk_off_size_mult"] if self.regime["risk_off"] else 1.0)
            h = [x[1] for x in self.mhist.get(mint, [])][-11:]
            rets = [b / a - 1 for a, b in zip(h, h[1:]) if a]
            vol10 = (sum(r * r for r in rets) / len(rets)) ** 0.5 if len(rets) >= 4 else None
            conv, conv_why = S.conviction(c, S.features(pair, now * 1000), name, self.regime["breadth"], vol10,
                                          (getattr(self, "probs", {}) or {}).get(mint), self.pred.m if self.pred.active else None,
                                          self.social.buzzing(pair["baseToken"].get("symbol"), now))
            nar = self.narr.match(pair["baseToken"].get("symbol"), pair["baseToken"].get("name"), now)
            if nar and nar.startswith("after-event"):
                conv_why.append(f"={nar} (no bonus: sell-the-news risk)")
                nar = None
            if nar:
                conv = min(c.get("conviction_max", 1.6), conv + 0.15)
                conv_why.append(f"+{nar}")
            nsm = len(self.smart.buyers(mint)) if self.smart.on else 0
            if nsm and name != "smart":
                conv = min(c.get("conviction_max", 1.6), conv + c.get("smart_money", {}).get("boost", 0.15))
                conv_why.append(f"+{nsm} smart wallet{'s' if nsm > 1 else ''} bought")
            ab, av = self.ai.bonus(mint)
            if ab:
                conv = min(c.get("conviction_max", 1.6), conv + ab)
                conv_why.append(f"+AI hype {av['hype']}/10")
            if av:
                why = f"{why} | AI: hype {av['hype']}, danger {av['danger']} — {av['reason']}"
            em = self.edge.size_mult(c, name)
            if em != 1.0:
                conv_why.append(f"edge size {em:.2f}x")
            mult *= conv * em
            sname = c["strategies"][name]
            stop_pct = S.vol_stop(c, sname, vol10)
            if stop_pct:
                conv_why.append(f"stop {stop_pct:.0f}% (its own swings)")
                sname = dict(sname, stop_loss_pct=stop_pct)   # wider stop = smaller size: same money at risk
            spend = S.position_size(c, sname, self.equity(), self.sol(), mult)
            why = f"{why} | size {conv:.2f}x: {', '.join(conv_why) or 'average setup'}"
            if spend < c["min_position_sol"]:
                log.info("skip %s: size %.4f SOL below minimum", pair["baseToken"]["symbol"], spend)
                continue
            try:
                if self.buy(mint, pair, name, why, spend, stop_pct):
                    bought.append(pair["baseToken"]["symbol"])
            except Exception as e:
                log.warning("buy %s failed: %s", pair["baseToken"]["symbol"], e)
        return bought, len(pairs)

    def fast_scan(self, can_buy):
        """Every few seconds: re-fetch only the coins that are heating up and let 'rocket' pounce."""
        c = self.cfg
        rs = c["strategies"].get("rocket", {})
        if not can_buy or not rs.get("enabled") or "rocket" in self.state["bench"] or not self.edge.allows(c, "rocket"):
            return
        if len(self.state["positions"]) >= c["max_positions"]:
            return
        lp = getattr(self, "last_pairs", {}) or {}
        hot = []
        for mint, p in lp.items():
            f = S.features(p)
            if (f["m5"] or 0) >= 3 or f["accel"] >= 2:
                hot.append((f["accel"] + (f["m5"] or 0) / 10, mint))
        hot = [m for _, m in sorted(hot, reverse=True)[:30]]
        if not hot:
            return
        now = time.time()
        fresh = best_pairs(hot, max_age=0)
        cd = self.state["cooldown"]
        for mint, pair in fresh.items():
            if mint in self.state["positions"] or (mint in cd and datetime.fromisoformat(cd[mint]).timestamp() > now):
                continue
            action, name, why, _ = S.entry_step(c, pair, None, now, {"rocket"})
            if action != "BUY":
                continue
            sym = pair["baseToken"]["symbol"]
            ok, swhy = safety_screen(c, self.chain, mint, sym)
            if not ok:
                log.info(swhy)
                continue
            st = S.strategy_stats(c, self.state["results"], "rocket")
            f = S.features(pair, now * 1000)
            conv, conv_why = S.conviction(c, f, "rocket", self.regime["breadth"], None, None, None,
                                          self.social.buzzing(sym, now))
            mult = S.size_mult(c, st) * conv * (c["risk_off_size_mult"] if self.regime["risk_off"] else 1.0)
            spend = S.position_size(c, rs, self.equity(), self.sol(), mult)
            if spend < c["min_position_sol"]:
                continue
            t0 = time.time()
            try:
                if self.buy(mint, pair, "rocket", f"{why} | size {conv:.2f}x: {', '.join(conv_why) or 'average setup'}", spend):
                    log.info("rocket: bought %s %.1f s after the 5 s check saw it", sym, time.time() - t0)
                    lp[mint] = pair
            except Exception as e:
                log.warning("rocket buy %s failed: %s", sym, e)
            if len(self.state["positions"]) >= c["max_positions"]:
                break

    def run(self):
        who = self.owner or "paper wallet"
        log.info("starting v3 — %s mode, wallet %s, %.4f SOL", self.cfg["mode"].upper(), who, self.sol())
        self.social.start()
        self.ai.start()
        self.smart.start()
        notify(self.cfg, "Meme bot v3 started", f"{self.cfg['mode']} mode, {self.sol():.4f} SOL", "rocket")
        if self.cfg["mode"] == "live":
            log.warning("LIVE MODE: real swaps will be signed and sent from %s, budget %.3f SOL",
                        self.owner, self.cfg["live_budget_sol"])
            if sys.stdin.isatty() and not os.getenv("MEMEBOT_CONFIRMED"):
                ans = input(f"Trade REAL money from wallet {self.owner} with up to {self.cfg['live_budget_sol']} SOL? Type YES: ")
                if ans.strip() != "YES":
                    sys.exit("not confirmed — nothing traded")
        last_scan = 0
        last_fast = 0
        while True:
            try:
                self.manage()
                can_buy = self.risk_ok()
                if self.state["killed"]:
                    self.save()
                    log.error("bot stopped by max drawdown. Delete state.json to start over.")
                    return
                if time.time() - last_fast >= self.cfg.get("fast_scan_seconds", 5) and time.time() - last_scan > 3:
                    last_fast = time.time()
                    try:
                        self.fast_scan(can_buy)
                    except requests.RequestException:
                        pass
                if time.time() - last_scan >= self.cfg["scan_seconds"]:
                    bought, seen = self.scan(trade=can_buy)
                    last_scan = time.time()
                    eq = self.equity()
                    rg = self.regime
                    self.state["last_scan"] = {"time": utcnow().isoformat(timespec="seconds"), "seen": seen,
                                               "passed": bought if isinstance(bought, list) else [],
                                               "watching": len(self.state["watch"]), "regime": rg}
                    log.info("scan: %d coins | watching %d | %s breadth %.0f%%, SOL 1h %s | equity %.4f SOL (%+.4f) | %d open%s",
                             seen, len(self.state["watch"]), "RISK-OFF" if rg["risk_off"] else "risk-on",
                             rg["breadth"] * 100, f"{rg['sol_h1']:+.1f}%" if rg["sol_h1"] is not None else "?",
                             eq, eq - self.state["start_equity"], len(self.state["positions"]),
                             "" if can_buy else " | buying paused")
                self.autotune_tick()
                self.daily_report()
                hist = self.state["equity_history"]
                if not hist or time.time() - hist[-1][0] >= 60:
                    hist.append([int(time.time()), round(self.equity(), 6)])
                    del hist[:-1440]
                self.state["sol_free"] = self.sol()
                self.save()
                time.sleep(min(self.cfg["manage_seconds"], self.cfg.get("fast_scan_seconds", 5)))
            except KeyboardInterrupt:
                self.save()
                log.info("stopped by you — open positions stay in the wallet: %s",
                         ", ".join(p["symbol"] for p in self.state["positions"].values()) or "none")
                return
            except requests.RequestException as e:
                log.warning("network trouble (%s), retrying", type(e).__name__)
                time.sleep(10)
            except Exception as e:
                log.exception("unexpected error: %s", e)
                time.sleep(15)

    def daily_report(self):
        today = utcnow().date().isoformat()
        if self.state.get("report_day") == today:
            return
        if self.state.get("report_day") is None:
            self.state["report_day"] = today
            return
        day = self.state["report_day"]
        self.state["report_day"] = today
        res = [r for r in self.state["results"] if r.get("time", "").startswith(day)]
        eq = self.equity()
        lines = [f"Balance {eq:.4f} SOL ({eq - self.state['start_equity']:+.4f} since start)",
                 f"{len(res)} trades closed, {sum(1 for r in res if r['pnl_sol'] > 0)} winners, "
                 f"{sum(r['pnl_sol'] for r in res):+.4f} SOL"]
        for n in self.cfg["strategies"]:
            rs = [r for r in res if r["strategy"] == n]
            if rs:
                lines.append(f"{n}: {len(rs)} trades, {sum(r['pnl_sol'] for r in rs):+.4f} SOL")
        if res:
            b = max(res, key=lambda r: r["ret_pct"])
            w = min(res, key=lambda r: r["ret_pct"])
            lines.append(f"best {b['symbol']} {b['ret_pct']:+.0f}%, worst {w['symbol']} {w['ret_pct']:+.0f}%")
        log.info("daily report %s: %s", day, " | ".join(lines))
        notify(self.cfg, f"Daily report {day}", "\n".join(lines), "bar_chart")

    def autotune_tick(self):
        """Every hour, run replay.py --autotune in a separate process (keeps trading fast).
        Adopt its settings only when they beat the current ones on data they weren't tuned on."""
        c = self.cfg
        # the background job ALWAYS runs: it refreshes the proven-strategy check (edge) and the model.
        # "autotune": false only stops it from changing settings by itself.
        proc = getattr(self, "_tune_proc", None)
        if proc is not None:
            if proc.poll() is None:
                return
            self._tune_proc = None
            if c.get("autotune", True):
                self.apply_tuned()
        if time.time() - self.state.get("last_autotune", 0) < min(30, c.get("autotune_minutes", 60)) * 60:
            return
        self.state["last_autotune"] = time.time()
        import subprocess
        self._tune_proc = subprocess.Popen([sys.executable, str(HERE / "replay.py"), "--autotune"]
                                           + ([] if c.get("autotune", True) else ["--light"]), cwd=HERE,
                                           stdout=open(HERE / "data" / "autotune.log", "a"), stderr=subprocess.STDOUT)
        log.info("autotune: testing new settings on recorded data in the background")

    def apply_tuned(self):
        f = HERE / "data" / "tuned.json"
        if not f.exists():
            return
        rep = json.loads(f.read_text())
        if rep["time"] <= self.state.get("tuned_applied", 0):
            return
        self.state["tuned_applied"] = rep["time"]
        if not rep.get("adopt"):
            log.info("autotune: no better settings found (current unseen %+.1f%%, best candidate unseen %+.1f%%)",
                     rep["current"]["unseen"], rep["unseen"]["ret"])
            return
        disk = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
        for k, v in rep["settings"].items():
            S.set_path(self.cfg, k, v)
            d = disk
            parts = k.split(".")
            for p in parts[:-1]:
                d = d.setdefault(p, {})
            d[parts[-1]] = v
        disk["_autotuned"] = utcnow().isoformat(timespec="seconds")
        CONFIG_PATH.write_text(json.dumps(disk, indent=2))
        msg = (f"New settings adopted: {rep['unseen']['ret']:+.1f}% on unseen data vs "
               f"{rep['current']['unseen']:+.1f}% for the old ones ({rep['hours']} h of data)")
        log.warning("autotune: %s", msg)
        notify(self.cfg, "Bot upgraded itself", msg, "brain")

    def status(self):
        sol = self.sol()
        price = self.state.get("sol_usd")
        print(f"mode       {self.cfg['mode']}" + (f"  wallet {self.owner}" if self.owner else ""))
        print(f"SOL free   {sol:.4f}" + (f"  (${sol * price:,.2f})" if price else ""))
        for mint, p in self.state["positions"].items():
            val = p.get("last_value_sol", p["cost_sol"])
            print(f"  {p['symbol']:<10} [{p['strategy']}] cost {p['cost_sol']:.4f} now {val:.4f} SOL "
                  f"({(val / p['cost_sol'] - 1) * 100:+.0f}%)")
        eq = self.equity()
        start = self.state["start_equity"] or eq
        res = self.state["results"]
        print(f"equity     {eq:.4f} SOL  ({eq - start:+.4f}, {(eq / start - 1) * 100 if start else 0:+.1f}%)"
              + (f"  ≈ ${eq * price:,.2f}" if price else ""))
        print(f"closed     {len(res)} trades, {sum(1 for r in res if r['pnl_sol'] > 0)} winners")
        for name in self.cfg["strategies"]:
            st = S.strategy_stats(self.cfg, res, name)
            b = " (benched)" if name in self.state["bench"] else ""
            print(f"  {name:<9} {st['n']:>3} trades  win {st['win_rate'] * 100:3.0f}%  avg {st['expectancy']:+.1f}%{b}")
        for n, s_ in self.cfg["strategies"].items():
            for r in s_.get("learned", []):
                if r.get("expires", 0) > time.time():
                    print(f"lesson     {r.get('text', r)}")
        self.edge.refresh()
        if self.edge.rep:
            for n in EDGE.RULES:
                print(f"edge       {n:<9} {self.edge.rep['verdict'].get(n, '?').upper():<6} {self.edge.why(n)}")
        else:
            print("edge       no evidence measured yet: rule strategies stay in shadow until it is")
        st_social = ", ".join(f"{k}: {v}" for k, v in self.social.status.items())
        print(f"social     {st_social or 'starting (sources check in once a minute)'}")
        print(f"smart $    {self.smart.status if self.smart.on else 'off'}")
        print(f"AI brain   {self.ai.status}" + (f" — {self.ai.who()}, {len(self.ai.verdicts)} coins rated" if self.ai.on else ""))
        self.pred.refresh()
        mm = self.pred.m
        if mm:
            print(f"model      {'ACTIVE' if self.pred.active else 'learning'} — {mm.get('rows', 0)} samples"
                  + (f", unseen AUC {mm['auc']}, top picks hit {mm.get('precision', 0):.0%}" if mm.get('auc') else "")
                  + (f" ({mm['why']})" if mm.get('why') else ""))
        if self.state["killed"]:
            print("STATE      stopped by max drawdown")
        elif self.state["halted_until"]:
            print(f"STATE      no new buys until {self.state['halted_until']}")


def main():
    ap = argparse.ArgumentParser(description="Lunyx Solana meme bot v3")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--scan", action="store_true", help="what each strategy sees right now, no trading")
    ap.add_argument("--live", action="store_true", help="confirm live trading (config mode must be live)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s  %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH)])
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    cfg = load_config()
    if a.status or a.scan:
        cfg = dict(cfg, mode="paper") if cfg["mode"] == "live" and not a.live else cfg
    bot = Bot(cfg, live_flag=a.live)
    if a.status:
        bot.status()
    elif a.scan:
        seen, n = bot.scan(trade=False)
        print(f"\n{n} coins checked. Regime: {'RISK-OFF' if bot.regime['risk_off'] else 'risk-on'}, "
              f"breadth {bot.regime['breadth'] * 100:.0f}%")
        for mint, (name, why) in seen.items():
            print("  " + why)
        if not seen:
            print("  no coin fits any strategy right now")
    else:
        bot.run()


if __name__ == "__main__":
    main()
