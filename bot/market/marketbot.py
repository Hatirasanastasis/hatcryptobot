#!/usr/bin/env python3
"""
Lunyx market bot — the meme bot's brain applied to regular crypto markets on exchanges
(BTC, ETH, SOL and the top altcoins on OKX, Binance, Bybit, Kraken, Coinbase and 100+ more via ccxt).

Same brain as meme/strategy.py: momentum, dip-buy, breakout and rocket strategies, smart sizing,
market-mood gate, adaptive allocation (winning strategies get bigger, losing ones benched),
lessons from losses, risk-based sizing, loss limits, kill switch, live guard.
Thresholds are retuned for big coins (moves of 1–8%, not 50%).

What it reads per coin, every minute:
  * 1-minute candles  → 5m / 1h / 5h moves, volume speed-up
  * recent trades     → real buys vs sells in the last 5 minutes (exchange tells the side)
  * order book        → money within 1% of the price (how much can be traded without moving it)

Modes (market/config.json "mode"):
  paper : live prices, fake USDT (default)
  demo  : real orders on the exchange's demo/testnet account (OKX, Binance, Bybit, Bitget, ...)
  live  : real orders with your API key — also needs --live, a budget, and typing YES

  python marketbot.py            run
  python marketbot.py --status   balance, positions, per-strategy results
  python marketbot.py --scan     what each strategy sees right now
"""
import argparse
import copy
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt
import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "meme"))   # repo layout
sys.path.insert(0, str(HERE.parent))            # HatCryptoBot app layout (bot\market\ next to bot\strategy.py)
import lessons as LESSONS          # noqa: E402  (shared with the meme bot)
import strategy as S               # noqa: E402

CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"
LOG_PATH = HERE / "marketbot.log"
log = logging.getLogger("marketbot")

STABLE_BASES = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD", "EUR", "EURC", "USDD", "BUSD", "USD1", "RLUSD", "USDG"}
SKIP_SUFFIX = ("UP", "DOWN", "BULL", "BEAR", "3L", "3S", "5L", "5S")
# leveraged / inverse ETFs (tokenized as X<ticker> on OKX): they swing 2-3x harder and decay over time
LEVERAGED_ETFS = {"SOXL", "SOXS", "TQQQ", "SQQQ", "UPRO", "SPXL", "SPXS", "SPXU", "TNA", "TZA", "LABU", "LABD", "FNGU", "FNGD",
                  "UVXY", "SVXY", "TECL", "TECS", "QLD", "SSO", "SDS", "TSLL", "TSLQ", "TSLS", "NVDL", "NVDU", "NVDD", "NVDQ",
                  "AMDL", "CONL", "MSTU", "MSTX", "MSTZ", "BITX", "BITU", "ETHU", "YINN", "YANG", "TMF", "TMV", "GUSH", "DRIP",
                  "NUGT", "DUST", "JNUG", "JDST", "BOIL", "KOLD", "UCO", "SCO", "AGQ", "ZSL", "DPST", "WEBL", "BULZ", "HIBL",
                  "SOXQ", "USD", "ROM", "UYG", "URTY", "UDOW", "SDOW", "DDM", "DXD", "MVV", "SMDD", "TSLT", "NVDX", "GGLL", "AAPU",
                  "AMZU", "MSFU", "METU", "PLTU", "COIU"}

DEFAULTS = {
    "mode": "paper",
    "exchange": "okx",
    "quote": "USDT",
    "universe_size": 50,               # top coins by 24h volume
    "scan_seconds": 15,
    "manage_seconds": 3,
    "paper_start": 100.0,              # in quote currency
    "paper_fee_pct": 0.10,             # taker fee each way
    "paper_slippage_pct": 0.05,
    "live_budget": 0.0,
    "demo_budget": 1000.0,             # demo accounts start with huge fake balances: trade as if it were this much
    "data_from_live": True,

    "max_positions": 5,
    "max_new_per_10min": 2,            # big coins move together: don't stack many buys on one market tick
    "risk_per_trade_pct": 1.0,
    "max_position_pct": 25.0,
    "min_order": 5.0,
    "max_daily_loss": 6.0,             # quote currency
    "max_drawdown_pct": 25.0,
    "max_consecutive_losses": 4,
    "loss_streak_pause_minutes": 60,
    "rebuy_cooldown_hours": 4,
    "max_hold_hours": 24,
    "liquidity_drop_exit_pct": 70.0,

    "min_volume_24h": 2_000_000,       # quote currency
    "max_spread_pct": 0.25,
    "min_depth_1pct": 20_000,          # money within 1% of price on each side

    "regime_min_breadth": 0.30,
    "regime_btc_h1_min": -1.0,
    "risk_off_size_mult": 0.5,
    "conviction_sizing": True,
    "conviction_max": 1.5,
    "adapt_window": 20, "adapt_min_trades": 6, "adapt_bench_after": 10,
    "adapt_bench_expectancy_pct": -0.8, "adapt_bench_hours": 12,

    "strategy_priority": ["rocket", "breakout", "dip", "momentum"],
    "strategies": {
        "momentum": {"enabled": True, "min_liquidity_usd": 0, "min_volume_h1_usd": 50_000,
                     "h1_min": 0.8, "h1_max": 12, "m5_min": -1.0, "m5_max": 2.5, "h6_min": -1.0, "min_buy_ratio": 1.15,
                     "min_txns_m5": 20, "confirm_scans": 2, "confirm_max_run_pct": 2.5, "confirm_min_run_pct": -1.0,
                     "stop_loss_pct": 3.0, "take_profit_pct": 5.0, "take_profit_sell_pct": 50,
                     "trail_activate_pct": 2.0, "trailing_stop_pct": 2.0, "breakeven_trigger_pct": 2.5, "breakeven_lock_pct": 0.3,
                     "stale_minutes": 120, "stale_min_gain_pct": 1.0},
        "dip": {"enabled": True, "min_liquidity_usd": 0, "min_volume_h1_usd": 100_000,
                "h1_min": 1.5, "h1_max": 20, "h6_min": 2, "m5_min": -4, "m5_max": -0.6, "min_buy_ratio": 0.9,
                "min_txns_m5": 20, "confirm_scans": 2, "confirm_max_run_pct": 1.5, "confirm_min_run_pct": -1.0,
                "stop_loss_pct": 3.0, "take_profit_pct": 4.0, "take_profit_sell_pct": 50,
                "trail_activate_pct": 1.5, "trailing_stop_pct": 1.5, "breakeven_trigger_pct": 2.0, "breakeven_lock_pct": 0.2,
                "stale_minutes": 90, "stale_min_gain_pct": 1.0},
        "breakout": {"enabled": True, "min_liquidity_usd": 0, "min_volume_h1_usd": 50_000, "volume_accel": 2.5,
                     "m5_min": 0.6, "m5_max": 4, "h1_min": -3, "h1_max": 10, "h6_min": -100, "min_buy_ratio": 1.5,
                     "min_txns_m5": 30, "confirm_scans": 1, "confirm_max_run_pct": 100, "confirm_min_run_pct": -100,
                     "stop_loss_pct": 2.5, "take_profit_pct": 5.0, "take_profit_sell_pct": 50,
                     "trail_activate_pct": 1.5, "trailing_stop_pct": 1.5, "breakeven_trigger_pct": 2.0, "breakeven_lock_pct": 0.2,
                     "stale_minutes": 45, "stale_min_gain_pct": 0.8},
        "rocket": {"enabled": True, "min_liquidity_usd": 0, "min_volume_h1_usd": 50_000, "volume_accel": 5.0,
                   "m5_min": 2, "m5_max": 8, "h1_min": -5, "h1_max": 15, "h6_min": -100, "min_buy_ratio": 2.0,
                   "min_txns_m5": 60, "confirm_scans": 1, "confirm_max_run_pct": 100, "confirm_min_run_pct": -100,
                   "stop_loss_pct": 3.0, "take_profit_pct": 8.0, "take_profit_sell_pct": 50,
                   "trail_activate_pct": 2.5, "trailing_stop_pct": 2.5, "breakeven_trigger_pct": 3.0, "breakeven_lock_pct": 0.3,
                   "stale_minutes": 20, "stale_min_gain_pct": 1.5, "max_position_pct": 15},
    },
    "ntfy_topic": "",
}

LIVE_LIMITS = [("risk_per_trade_pct", "max", 1.0), ("max_position_pct", "max", 20.0), ("max_positions", "max", 4),
               ("max_drawdown_pct", "max", 20.0), ("max_consecutive_losses", "max", 3), ("regime_min_breadth", "min", 0.35),   # live keeps the stricter line
               ("max_spread_pct", "max", 0.2), ("min_volume_24h", "min", 5_000_000), ("conviction_max", "max", 1.25)]


# ============================================================================ setup

def load_config():
    over = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    cfg = S.deep_merge(DEFAULTS, over)
    if cfg["mode"] not in ("paper", "demo", "live"):
        sys.exit("config: mode must be paper, demo or live")
    # the shared brain reads these names
    cfg.setdefault("base", {})
    cfg["liquidity_drop_exit_pct"] = cfg["liquidity_drop_exit_pct"]
    return cfg


def load_env():
    for p in (HERE / ".env", HERE.parent / ".env"):
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def notify(cfg, title, msg, tags=""):
    topic = cfg.get("ntfy_topic")
    if not topic:
        return
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=msg.encode(), timeout=10,
                      headers={"Title": title.encode("ascii", "ignore").decode(), "Tags": tags})
    except Exception:
        pass


def utcnow():
    return datetime.now(timezone.utc)


def us_market_open(now=None):
    """NYSE/Nasdaq regular hours, 13:30-20:00 UTC in US summer time, 14:30-21:00 in winter (Mon-Fri).
    Holidays are not handled; the bot simply sees flat prices then."""
    now = now or utcnow()
    if now.weekday() >= 5:
        return False
    y = now.year
    # US DST: second Sunday of March 07:00 UTC to first Sunday of November 06:00 UTC
    mar = datetime(y, 3, 8, 7, tzinfo=timezone.utc)
    dst_start = mar + timedelta(days=(6 - mar.weekday()) % 7)
    nov = datetime(y, 11, 1, 6, tzinfo=timezone.utc)
    dst_end = nov + timedelta(days=(6 - nov.weekday()) % 7)
    off = 0 if dst_start <= now < dst_end else 1
    mins = now.hour * 60 + now.minute
    return (13 * 60 + 30 + off * 60) <= mins < (20 * 60 + off * 60)


# ============================================================================ market data

class Market:
    def __init__(self, cfg):
        self.cfg = cfg
        params = {"enableRateLimit": True}
        mode = cfg["mode"]
        if mode in ("demo", "live"):
            pre = "DEMO_" if mode == "demo" else ""
            key, sec = os.getenv(pre + "API_KEY"), os.getenv(pre + "API_SECRET")
            if not key or not sec:
                sys.exit(f"{mode} mode needs {pre}API_KEY and {pre}API_SECRET in .env")
            params.update(apiKey=key, secret=sec)
            if os.getenv(pre + "API_PASSWORD"):
                params["password"] = os.getenv(pre + "API_PASSWORD")
        if cfg["exchange"] not in ccxt.exchanges:
            sys.exit(f"unknown exchange {cfg['exchange']}")
        self.ex = getattr(ccxt, cfg["exchange"])(params)
        self.ex.session.trust_env = True                 # respect HTTPS_PROXY / CA settings if the machine has them
        if mode == "demo":
            try:
                self.ex.set_sandbox_mode(True)
            except Exception:
                sys.exit(f"{cfg['exchange']} has no demo account in ccxt — use paper, or okx / binance / bybit / bitget")
        self.ex.load_markets()
        self.cache = {}
        # demo exchanges have thin practice order books: read REAL prices/candles/trades from the live market,
        # place orders on the demo account
        if mode == "demo" and cfg.get("data_from_live", True):
            self.data = getattr(ccxt, cfg["exchange"])({"enableRateLimit": True})
            self.data.session.trust_env = True
            self.data.load_markets()
        else:
            self.data = self.ex
        # USDC pairs (OKX Europe) are thin: read signals from the deep BASE/USDT market of the same coin
        # (same price), place the order on BASE/USDC
        self.sig_data, self.sig = self.data, {}
        if cfg["quote"] != "USDT" and cfg.get("signals_from_usdt", True) and cfg["mode"] != "live":
            for name in dict.fromkeys(("okx", cfg["exchange"]) if cfg["exchange"] in ("okx", "myokx") else (cfg["exchange"],)):
                try:
                    sd = getattr(ccxt, name)({"enableRateLimit": True})
                    sd.session.trust_env = True
                    sd.load_markets()
                    self.sig_data = sd
                    break
                except Exception as e:
                    log.warning("USDT signal data from %s unavailable (%s)", name, str(e)[:80])

    def src(self, sym):
        """(exchange, symbol) to read market data from for a traded symbol."""
        s = self.sig.get(sym)
        return (self.sig_data, s) if s else (self.data, sym)

    def universe(self):
        q = self.cfg["quote"]
        tk = self.data.fetch_tickers()
        stk = self.sig_data.fetch_tickers() if self.sig_data is not self.data or q != "USDT" else {}
        rows = []
        self.sig = {}
        for sym, t in tk.items():
            if sym not in self.ex.markets:            # must be tradable on the account we place orders on
                continue
            m = self.data.markets.get(sym)
            if not m or not m.get("spot") or m.get("quote") != q or not m.get("active", True):
                continue
            base = m["base"]
            alt = f"{base}/USDT"
            if q != "USDT" and alt in stk and alt in self.sig_data.markets:
                aqv = stk[alt].get("quoteVolume") or 0
                if aqv > (t.get("quoteVolume") or 0):
                    self.sig[sym] = alt
                    t = stk[alt]
            if base in STABLE_BASES or base.endswith(SKIP_SUFFIX):
                continue
            kind = self.kind(m)
            if kind == "stock":
                if not self.cfg.get("trade_stocks", True) or (base[1:] if base.startswith("X") else base) in LEVERAGED_ETFS:
                    continue
                if self.cfg.get("stocks_market_hours_only", True) and not us_market_open():
                    continue
            if kind == "gold" and not self.cfg.get("trade_gold", True):
                continue
            qv = t.get("quoteVolume") or ((t.get("baseVolume") or 0) * (t.get("last") or 0))
            if qv and qv >= self.cfg["min_volume_24h"]:
                rows.append((qv, sym, t))
        rows.sort(reverse=True)
        return [(s, t, qv) for qv, s, t in rows[: self.cfg["universe_size"]]]

    @staticmethod
    def kind(m):
        """crypto / stock / gold. OKX tags tokenized stocks (instCategory 3) and gold (PAXG, XAUT)."""
        if m["base"] in ("PAXG", "XAUT"):
            return "gold"
        cat = str((m.get("info") or {}).get("instCategory") or "1")
        return "stock" if cat == "3" else "crypto"

    def candles(self, sym, n=300):
        ex, s = self.src(sym)
        return ex.fetch_ohlcv(s, "1m", limit=n)

    def features(self, sym, ticker, qv24, deep=False):
        """Build the same feature dict the meme brain uses, from exchange data."""
        c = self.candles(sym)
        if len(c) < 70:
            return None
        closes = [x[4] for x in c]
        p = closes[-1]

        def chg(n):
            return (p / closes[-1 - n] - 1) * 100 if len(closes) > n and closes[-1 - n] else None
        qvol = [x[5] * x[4] for x in c]
        vm5, vh1 = sum(qvol[-5:]), sum(qvol[-60:])
        f = {"sym": sym, "quote": S.SOL_MINT, "price": p, "vol_h1": vh1, "vol_m5": vm5,
             "m5": chg(5), "h1": chg(60), "h6": chg(len(closes) - 1), "accel": vm5 * 12 / vh1 if vh1 else 0,
             "liq": 0.0, "mcap": 1e12, "age_min": 1e7, "buys": 0, "sells": 0, "ratio": 0.0,
             "spread": None, "qv24": qv24}
        # volume-weighted up/down candles as a cheap buy-pressure estimate for every coin
        up = sum(v for x, v in zip(c[-5:], qvol[-5:]) if x[4] >= x[1])
        dn = sum(v for x, v in zip(c[-5:], qvol[-5:]) if x[4] < x[1])
        f["ratio"] = up / dn if dn else (9.9 if up else 0)
        bid, ask = ticker.get("bid"), ticker.get("ask")
        if bid and ask:
            f["spread"] = (ask - bid) / ((ask + bid) / 2) * 100
        if deep:
            self.deepen(sym, f)
        return f

    def deepen(self, sym, f):
        """Real buys/sells from the trade tape + order book depth — only for coins close to a signal."""
        ex, s = self.src(sym)
        now = ex.milliseconds()
        tr = ex.fetch_trades(s, limit=500)
        recent = [t for t in tr if now - t["timestamp"] <= 5 * 60_000]
        buys = sum(1 for t in recent if t["side"] == "buy")
        sells = sum(1 for t in recent if t["side"] == "sell")
        bv = sum(t["cost"] or t["amount"] * t["price"] for t in recent if t["side"] == "buy")
        sv = sum(t["cost"] or t["amount"] * t["price"] for t in recent if t["side"] == "sell")
        f["buys"], f["sells"] = buys, sells
        f["ratio"] = bv / sv if sv else (9.9 if bv else 0)
        ob = ex.fetch_order_book(s, limit=100)
        p = f["price"]
        bd = sum(px * q for px, q, *_ in ob["bids"] if px >= p * 0.99)
        ad = sum(px * q for px, q, *_ in ob["asks"] if px <= p * 1.01)
        f["liq"] = min(bd, ad) * 2
        f["deep"] = True
        return f


# ============================================================================ bot

class Bot:
    def __init__(self, cfg, live_flag=False):
        load_env()
        if cfg["mode"] == "live":
            if not live_flag:
                sys.exit('config says "live" — add --live to confirm real trading')
            if not cfg.get("live_budget"):
                sys.exit('live mode needs "live_budget" in config.json')
            for path, kind, lim in LIVE_LIMITS:
                v = cfg[path]
                if (kind == "max" and v > lim) or (kind == "min" and v < lim):
                    log.warning("live guard: %s %s → %s", path, v, lim)
                    cfg[path] = lim
        self.cfg = cfg
        self.mk = Market(cfg)
        self.q = cfg["quote"]
        self.st = self.load()
        self.regime = {"risk_off": False, "breadth": 0.5, "btc_h1": None}
        self.last = {}

    # ---------------------------------------------------------------- state
    def load(self):
        st = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
        st.setdefault("cash", self.cfg["paper_start"])
        for k, v in (("pos", {}), ("results", []), ("trades", []), ("watch", {}), ("cooldown", {}), ("bench", {}),
                     ("equity_history", [])):
            st.setdefault(k, v)
        for k in ("start_equity", "peak_equity", "day", "day_start", "halted_until", "pause_until"):
            st.setdefault(k, None)
        st.setdefault("killed", False)
        st.setdefault("streak", 0)
        return st

    def save(self):
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.st, indent=1))
        tmp.replace(STATE_PATH)

    # ---------------------------------------------------------------- money
    def cash(self):
        if self.cfg["mode"] == "paper":
            return self.st["cash"]
        bal = self.mk.ex.fetch_balance()
        free = float((bal.get("free") or {}).get(self.q) or 0)
        if self.cfg["mode"] == "demo" and self.cfg.get("demo_budget"):
            realized = sum(r["pnl"] for r in self.st["results"])
            in_pos = sum(p["cost"] for p in self.st["pos"].values())
            return max(0.0, min(free, self.cfg["demo_budget"] + realized - in_pos))
        if self.cfg["mode"] == "live":
            realized = sum(r["pnl"] for r in self.st["results"])
            in_pos = sum(p["cost"] for p in self.st["pos"].values())
            return max(0.0, min(free, self.cfg["live_budget"] + realized - in_pos))
        return free

    def equity(self):
        return self.cash() + sum(p["qty"] * p.get("last", p["entry_px"]) for p in self.st["pos"].values())

    def fill(self, side, sym, amount_quote=None, qty=None, price=None):
        """Returns (qty, avg_price, cost_quote)."""
        c = self.cfg
        if c["mode"] == "paper":
            slip = (c["paper_slippage_pct"] + c["paper_fee_pct"]) / 100
            if side == "buy":
                px = price * (1 + slip)
                q = amount_quote / px
                self.st["cash"] -= amount_quote
                return q, px, amount_quote
            px = price * (1 - slip)
            got = qty * px
            self.st["cash"] += got
            return qty, px, got
        ex = self.mk.ex
        if side == "buy":
            q = float(ex.amount_to_precision(sym, amount_quote / price))
            o = ex.create_market_buy_order(sym, q)
        else:
            q = float(ex.amount_to_precision(sym, qty))
            o = ex.create_market_sell_order(sym, q)
        for _ in range(10):
            if o.get("status") == "closed":
                break
            time.sleep(1)
            o = ex.fetch_order(o["id"], sym)
        filled = float(o.get("filled") or q)
        avg = float(o.get("average") or price)
        return filled, avg, float(o.get("cost") or filled * avg)

    # ---------------------------------------------------------------- trading
    def buy(self, sym, f, name, why, spend):
        q, px, cost = self.fill("buy", sym, amount_quote=spend, price=f["price"])
        self.st["pos"][sym] = {"strategy": name, "qty": q, "entry_px": px, "cost": cost, "cost0": cost, "realized": 0.0,
                               "peak": cost, "last": px, "opened": time.time(), "took": False,
                               "entry": LESSONS.entry_snapshot(f)}
        log.info("BUY  %s  %.2f %s @ %s — %s", sym, cost, self.q, self.mk.ex.price_to_precision(sym, px), why)
        notify(self.cfg, f"BUY {sym} [{name}] ({self.cfg['mode']})", f"{cost:.2f} {self.q}\n{why}", "green_circle")
        self.st["trades"].append({"t": utcnow().isoformat(timespec="seconds"), "side": "buy", "sym": sym, "amt": cost,
                                  "strategy": name, "why": why})

    def sell(self, sym, frac, why, price):
        p = self.st["pos"][sym]
        qty = p["qty"] * frac
        q, px, got = self.fill("sell", sym, qty=qty, price=price)
        cost_part = p["cost"] * frac
        p["realized"] += got
        pnl = got - cost_part
        log.info("SELL %s  %d%% → %.2f %s (P&L %+.2f, %+.2f%%) [%s] — %s", sym, round(frac * 100), got, self.q, pnl,
                 (got / cost_part - 1) * 100, p["strategy"], why)
        notify(self.cfg, f"SELL {sym} {pnl:+.2f} {self.q} ({self.cfg['mode']})", f"[{p['strategy']}] {why}",
               "moneybag" if pnl > 0 else "red_circle")
        self.st["trades"].append({"t": utcnow().isoformat(timespec="seconds"), "side": "sell", "sym": sym, "amt": got,
                                  "pnl": pnl, "strategy": p["strategy"], "why": why})
        self.st["trades"] = self.st["trades"][-1000:]
        if frac >= 0.999:
            total = p["realized"] - p["cost0"]
            self.st["results"].append({"strategy": p["strategy"], "ret_pct": total / p["cost0"] * 100, "pnl_sol": total,
                                       "pnl": total, "symbol": sym, "time": utcnow().isoformat(timespec="seconds"),
                                       "entry": p.get("entry"), "exit": why})
            self.st["results"] = self.st["results"][-1000:]
            self.st["streak"] = self.st["streak"] + 1 if total < 0 else 0
            del self.st["pos"][sym]
            self.st["cooldown"][sym] = time.time() + self.cfg["rebuy_cooldown_hours"] * 3600
            self.after_close(p["strategy"], total)
        else:
            p["took"] = True
            p["qty"] -= qty
            p["cost"] -= cost_part
            p["peak"] *= (1 - frac)

    def after_close(self, name, pnl):
        c = self.cfg
        st = S.strategy_stats(c, self.st["results"], name)
        if S.benched(c, st) and name not in self.st["bench"]:
            self.st["bench"][name] = time.time() + c["adapt_bench_hours"] * 3600
            log.warning("benching [%s] for %dh — last %d trades average %+.2f%%", name, c["adapt_bench_hours"], st["n"], st["expectancy"])
        if pnl < 0:
            rule = LESSONS.learn(c, self.st["results"], name)
            if rule:
                LESSONS.save_rules(CONFIG_PATH, c)
                log.warning("lesson learned — %s (%s)", rule["text"], rule["why"])
                notify(c, "Market bot learned a lesson", f"{rule['text']}\n{rule['why']}", "brain")
        if self.st["streak"] >= c["max_consecutive_losses"]:
            self.st["pause_until"] = time.time() + c["loss_streak_pause_minutes"] * 60
            self.st["streak"] = 0
            log.warning("%d losses in a row — pausing new buys for %d min", c["max_consecutive_losses"], c["loss_streak_pause_minutes"])

    def manage(self):
        if not self.st["pos"]:
            return
        tk = self.mk.data.fetch_tickers(list(self.st["pos"]))
        now = time.time()
        for sym in list(self.st["pos"]):
            p = self.st["pos"][sym]
            t = tk.get(sym) or {}
            bid = t.get("bid") or t.get("last")
            if not bid:
                continue
            p["last"] = bid
            val = p["qty"] * bid
            p["peak"] = max(p["peak"], val)
            s = self.cfg["strategies"][p["strategy"]]
            entry_val = p["qty"] * p["entry_px"]
            frac, why = S.exit_step(self.cfg, s, {"entry": entry_val, "peak": p["peak"], "opened": p["opened"],
                                                  "took_profit": p["took"], "entry_liq": None}, val, None, now)
            if frac:
                self.sell(sym, frac, why, bid)

    def risk_ok(self):
        s, c = self.st, self.cfg
        eq = self.equity()
        today = utcnow().date().isoformat()
        if s["start_equity"] is None:
            s["start_equity"] = s["peak_equity"] = eq
        if s["day"] != today:
            s["day"], s["day_start"] = today, eq
            s["halted_until"] = None
        s["peak_equity"] = max(s["peak_equity"], eq)
        if s["killed"]:
            return False
        if (s["results"] or s["pos"]) and s["peak_equity"] > 0 and eq <= s["peak_equity"] * (1 - c["max_drawdown_pct"] / 100):
            log.error("kill switch: %.0f%% below peak — selling everything and stopping", c["max_drawdown_pct"])
            for sym in list(s["pos"]):
                self.sell(sym, 1.0, "kill switch", s["pos"][sym]["last"])
            s["killed"] = True
            return False
        if eq - s["day_start"] <= -c["max_daily_loss"]:
            if not s["halted_until"]:
                log.warning("daily loss limit hit — no new buys until tomorrow (UTC)")
                notify(c, "Market bot: daily loss limit", "No new buys until tomorrow (UTC).", "warning")
                s["halted_until"] = today
            return False
        if s["pause_until"] and time.time() < s["pause_until"]:
            return False
        return True

    def log_near_misses(self, feats, allowed):
        """Every few minutes: which coins are closest to a buy, and what is still missing (so a quiet bot is explainable)."""
        rows = []
        for sym, f in feats.items():
            if sym in self.st["pos"] or f.get("h1") is None or f.get("m5") is None:
                continue
            for n in allowed:
                s = self.cfg["strategies"].get(n)
                if not s or not s.get("enabled"):
                    continue
                miss = []
                if not s["h1_min"] <= f["h1"] <= s["h1_max"]:
                    miss.append(f"1h {f['h1']:+.1f}% (needs {s['h1_min']:+g}..{s['h1_max']:+g})")
                if not s["m5_min"] <= f["m5"] <= s["m5_max"]:
                    miss.append(f"5m {f['m5']:+.1f}% (needs {s['m5_min']:+g}..{s['m5_max']:+g})")
                if f.get("h6") is not None and f["h6"] < s["h6_min"]:
                    miss.append(f"6h {f['h6']:+.1f}% (needs {s['h6_min']:+g}+)")
                if "volume_accel" in s and f["accel"] < s["volume_accel"]:
                    miss.append(f"volume x{f['accel']:.1f} (needs x{s['volume_accel']:g})")
                if f["vol_h1"] < s["min_volume_h1_usd"]:
                    miss.append("too little volume")
                rows.append((len(miss), sym, n, miss))
        rows.sort(key=lambda r: r[0])
        if rows:
            log.info("closest to a buy: " + " | ".join(
                f"{sym.split('/')[0]} {n}: " + (", ".join(miss) if miss else "price pattern OK, checking buyers")
                for _, sym, n, miss in rows[:3]))

    def base_ok(self, f):
        c = self.cfg
        if f["spread"] is not None and f["spread"] > c["max_spread_pct"]:
            return False, f"spread {f['spread']:.2f}% too wide"
        if f.get("deep") and f["liq"] < c["min_depth_1pct"]:
            return False, f"order book too thin (${f['liq']:,.0f} within 1%)"
        return True, ""

    def scan(self, trade=True):
        c = self.cfg
        now = time.time()
        uni = self.mk.universe()
        feats = {}

        def one(row):
            sym, t, qv = row
            try:
                return sym, self.mk.features(sym, t, qv)
            except Exception as e:
                log.debug("%s features failed: %s", sym, e)
                return sym, None
        # fetch every coin's candles at the same time instead of one after another (scan in ~2s, not ~25s)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=c.get("fetch_threads", 8)) as pool:
            for sym, f in pool.map(one, uni):
                if f:
                    feats[sym] = f
        self.last = feats
        try:   # record a snapshot per scan: the dataset future self-tuning learns from
            (HERE / "data").mkdir(exist_ok=True)
            with open(HERE / "data" / f"market-{utcnow():%Y%m%d}.jsonl", "a") as fh:
                ts = int(now)
                for sym, f in feats.items():
                    fh.write(json.dumps({"ts": ts, "sym": sym, **{k: (round(v, 6) if isinstance(v, float) else v)
                                         for k, v in f.items() if k in ("price", "m5", "h1", "h6", "accel", "ratio",
                                                                        "vol_h1", "vol_m5", "spread", "qv24")}}) + "\n")
        except Exception as e:
            log.debug("record failed: %s", e)
        # regime: breadth across the universe + BTC trend
        act = [f for f in feats.values() if f["h1"] is not None]
        breadth = sum(1 for f in act if f["h1"] > 0) / len(act) if act else 0.5
        btc = next((f for s, f in feats.items() if s.startswith("BTC/")), None)
        btc_h1 = btc["h1"] if btc else None
        risk_off = breadth < c["regime_min_breadth"] or (btc_h1 is not None and btc_h1 < c["regime_btc_h1_min"])
        self.regime = {"risk_off": risk_off, "breadth": breadth, "btc_h1": btc_h1}

        self.st["cooldown"] = {k: v for k, v in self.st["cooldown"].items() if v > now}
        self.st["bench"] = {k: v for k, v in self.st["bench"].items() if v > now}
        allowed = {n for n in c["strategies"] if n not in self.st["bench"]}
        if risk_off:
            allowed.discard("breakout")
            allowed.discard("rocket")
        ready, seen = [], {}
        if now - getattr(self, "_near_t", 0) >= c.get("near_miss_log_seconds", 300):
            self._near_t = now
            self.log_near_misses(feats, allowed)
        for sym, f in feats.items():
            if sym in self.st["pos"] or sym in self.st["cooldown"] or sym in self.st.get("blocked", {}):
                continue
            ok, why = self.base_ok(f)
            if not ok:
                continue
            # cheap pre-check with candle data, then fetch the real trade tape + order book
            loose = {n: dict(c["strategies"][n], min_txns_m5=0, min_buy_ratio=0) for n in allowed if n in c["strategies"]}
            if sym not in self.st["watch"] and not any(S.signal(n, loose[n], dict(f, buys=99)) for n in loose):
                continue
            try:
                self.mk.deepen(sym, f)
            except Exception as e:
                log.debug("%s deepen failed: %s", sym, e)
                continue
            ok, why = self.base_ok(f)
            if not ok:
                log.info("skip %s: %s", sym, why)
                continue
            pair = {"_f": f}
            action, name, why, w = self.entry(f, self.st["watch"].get(sym), now, allowed)
            if action == "NONE":
                nm = getattr(self, "_nm", {})
                self._nm = nm
                if now - nm.get(sym, 0) > 300:
                    nm[sym] = now
                    log.info("almost %s: price fits, but buyers too weak (buy/sell %.2f, %d trades in 5 min)",
                             sym.split("/")[0], f["ratio"], f["buys"] + f["sells"])
            if action in ("NONE", "DROP"):
                if action == "DROP" and sym in self.st["watch"]:
                    log.info(why if why.startswith("dropped") else f"dropped {sym}: price moved too far while confirming")
                self.st["watch"].pop(sym, None)
                continue
            seen[sym] = (name, why)
            if action == "WATCH":
                if sym not in self.st["watch"]:
                    log.info("watching %s", why)
                self.st["watch"][sym] = w
            elif action == "BUY":
                self.st["watch"].pop(sym, None)
                ready.append((sym, f, name, why))
        if not trade:
            return seen, len(feats)
        bought = []
        ready.sort(key=lambda r: -r[1]["vol_h1"])
        recent_buys = [t for t in self.st["trades"] if t["side"] == "buy"
                       and time.time() - datetime.fromisoformat(t["t"]).timestamp() < 600]
        room = max(0, c.get("max_new_per_10min", 2) - len(recent_buys))
        for sym, f, name, why in ready:
            if len(self.st["pos"]) >= c["max_positions"]:
                break
            if room <= 0:
                log.info("skip %s: already bought %d coins in the last 10 min (they move together)", sym,
                         c.get("max_new_per_10min", 2))
                break
            s = c["strategies"][name]
            st = S.strategy_stats(c, self.st["results"], name)
            conv, cwhy = self.conviction(f, name)
            mult = S.size_mult(c, st) * conv * (c["risk_off_size_mult"] if risk_off else 1.0)
            eq = self.equity()
            raw = eq * c["risk_per_trade_pct"] / 100 / (s["stop_loss_pct"] / 100) * mult
            cap = eq * min(c["max_position_pct"], s.get("max_position_pct", 100)) / 100
            spend = min(raw, cap, self.cash() * 0.98, f["liq"] * 0.02 if f.get("liq") else raw)
            if spend < c["min_order"]:
                log.info("skip %s: size %.2f %s below the minimum order", sym, spend, self.q)
                continue
            try:
                self.buy(sym, f, name, f"{why} | size {conv:.2f}x: {', '.join(cwhy) or 'average setup'}", spend)
                bought.append(sym)
                room -= 1
            except Exception as e:
                txt = str(e)
                if any(code in txt for code in ("50123", "51155", "51087", "51001")):
                    # the account may not trade this coin (OKX Europe restricts some coins / stock tokens): stop trying
                    self.st.setdefault("blocked", {})[sym] = txt[-120:]
                    log.warning("%s can't be traded on this account (%s) — skipping it from now on", sym, txt[-80:])
                else:
                    log.warning("buy %s failed: %s", sym, e)
        return bought, len(feats)

    def entry(self, f, w, now, allowed):
        c = self.cfg
        for name in c["strategy_priority"]:
            s = c["strategies"].get(name)
            if not s or not s.get("enabled") or name not in allowed or not S.signal(name, s, f):
                continue
            why = (f"[{name}] {f['sym']}: 1h {f['h1']:+.1f}%, 5m {f['m5']:+.1f}%, vol x{f['accel']:.1f}, "
                   f"{f['buys']}/{f['sells']} buys/sells, depth ${f['liq']:,.0f}")
            if s["confirm_scans"] <= 1:
                return "BUY", name, why, None
            if not w or w.get("strategy") != name or now - w["first"] > 900:
                return "WATCH", name, why, {"strategy": name, "first": now, "last": now, "price": f["price"], "hits": 1}
            if now - w["last"] < 45:
                return "WAIT", name, why, w
            run = (f["price"] / w["price"] - 1) * 100
            if not (s["confirm_min_run_pct"] <= run <= s["confirm_max_run_pct"]):
                return "DROP", name, why, None
            w = dict(w, hits=w["hits"] + 1, last=now)
            if w["hits"] >= s["confirm_scans"]:
                return "BUY", name, f"{why}, confirmed ({run:+.1f}%)", None
            return "WATCH", name, why, w
        # the setup fired earlier and is being confirmed: one wobbly scan must not throw it away.
        # Buy if the price held (not chased up, not falling) for 45 s+; drop after 5 minutes.
        if w and w.get("strategy") in allowed and now - w["first"] <= 300:
            s = c["strategies"][w["strategy"]]
            if now - w["last"] < 45:
                return "WAIT", w["strategy"], "", w
            run = (f["price"] / w["price"] - 1) * 100
            if s["confirm_min_run_pct"] <= run <= s["confirm_max_run_pct"] and f["ratio"] >= 0.9:
                return "BUY", w["strategy"], (f"[{w['strategy']}] {f['sym']}: setup from {now - w['first']:.0f}s ago held "
                                              f"({run:+.1f}%), buy/sell {f['ratio']:.2f}"), None
            return "DROP", w["strategy"], f"dropped {f['sym']}: moved {run:+.1f}% while confirming", None
        return "NONE", None, "", None

    def conviction(self, f, name):
        c = self.cfg
        if not c.get("conviction_sizing", True):
            return 1.0, []
        sc, why = 0.0, []

        def add(x, t):
            nonlocal sc
            sc += x
            why.append(("+" if x > 0 else "−") + t)
        if f["qv24"] >= 100_000_000: add(0.10, "very liquid coin")
        elif f["qv24"] < 5_000_000: add(-0.15, "small coin")
        if f["ratio"] >= 2.0: add(0.15, "strong buying")
        elif f["ratio"] < 1.1 and name != "dip": add(-0.10, "weak buying")
        if f["spread"] is not None and f["spread"] > 0.1: add(-0.10, "wide spread")
        if self.regime["breadth"] >= 0.6: add(0.10, "market rising")
        elif self.regime["breadth"] < 0.4: add(-0.15, "market weak")
        b = self.regime.get("btc_h1")
        if b is not None and b > 0.5: add(0.05, "BTC rising")
        elif b is not None and b < -0.5: add(-0.10, "BTC falling")
        if f["h1"] is not None and f["h1"] > 8: add(-0.15, "already ran hard")
        return max(0.4, min(c.get("conviction_max", 1.5), 1 + sc)), why

    # ---------------------------------------------------------------- loop
    def run(self):
        c = self.cfg
        log.info("starting market bot — %s mode on %s, %.2f %s", c["mode"].upper(), c["exchange"], self.cash(), self.q)
        if c["mode"] == "live" and sys.stdin.isatty() and not os.getenv("MARKETBOT_CONFIRMED"):
            if input(f"Trade REAL money on {c['exchange']} with up to {c['live_budget']} {self.q}? Type YES: ").strip() != "YES":
                sys.exit("not confirmed")
        last_scan = 0
        while True:
            try:
                self.manage()
                can = self.risk_ok()
                if self.st["killed"]:
                    self.save()
                    return
                if time.time() - last_scan >= c["scan_seconds"]:
                    bought, n = self.scan(trade=can)
                    last_scan = time.time()
                    eq = self.equity()
                    rg = self.regime
                    log.info("scan: %d coins | watching %d | %s breadth %.0f%%, BTC 1h %s | equity %.2f %s (%+.2f) | %d open%s",
                             n, len(self.st["watch"]), "RISK-OFF" if rg["risk_off"] else "risk-on", rg["breadth"] * 100,
                             f"{rg['btc_h1']:+.2f}%" if rg["btc_h1"] is not None else "?", eq, self.q,
                             eq - self.st["start_equity"], len(self.st["pos"]), "" if can else " | buying paused")
                    h = self.st["equity_history"]
                    h.append([int(time.time()), round(eq, 4)])
                    del h[:-1440]
                self.save()
                time.sleep(c["manage_seconds"])
            except KeyboardInterrupt:
                self.save()
                log.info("stopped — open positions stay in your account: %s", ", ".join(self.st["pos"]) or "none")
                return
            except (ccxt.NetworkError, requests.RequestException) as e:
                log.warning("network trouble (%s), retrying", type(e).__name__)
                time.sleep(10)
            except Exception as e:
                log.exception("unexpected error: %s", e)
                time.sleep(15)

    def status(self):
        eq = self.equity()
        start = self.st["start_equity"] or eq
        print(f"mode      {self.cfg['mode']} on {self.cfg['exchange']}")
        print(f"cash      {self.cash():.2f} {self.q}")
        for sym, p in self.st["pos"].items():
            v = p["qty"] * p.get("last", p["entry_px"])
            print(f"  {sym:<12} [{p['strategy']}] in {p['cost']:.2f} now {v:.2f} ({(v / p['cost'] - 1) * 100:+.2f}%)")
        print(f"equity    {eq:.2f} {self.q}  ({eq - start:+.2f}, {(eq / start - 1) * 100 if start else 0:+.2f}%)")
        res = self.st["results"]
        print(f"closed    {len(res)} trades, {sum(1 for r in res if r['pnl'] > 0)} winners")
        for n in self.cfg["strategies"]:
            st = S.strategy_stats(self.cfg, res, n)
            print(f"  {n:<9} {st['n']:>3} trades  win {st['win_rate'] * 100:3.0f}%  avg {st['expectancy']:+.2f}%"
                  + (" (benched)" if n in self.st["bench"] else ""))
        for n, s in self.cfg["strategies"].items():
            for r in s.get("learned", []):
                if r.get("expires", 0) > time.time():
                    print(f"lesson    {r.get('text')}")


def test_order(bot):
    """Proof the account is connected: buy about 5 (quote) of BTC on the DEMO account, then sell exactly that back."""
    say = lambda s: (print(s, flush=True), log.info("test order: %s", s))
    if bot.cfg["mode"] != "demo":
        say("test order only runs on the demo account")
        sys.exit(2)
    ex, q = bot.mk.ex, bot.cfg["quote"]
    sym = next((s for s in (f"BTC/{q}", f"ETH/{q}", f"SOL/{q}") if s in ex.markets), None)
    if not sym:
        say(f"no BTC/ETH/SOL market in {q} on this account")
        sys.exit(2)
    base = ex.markets[sym]["base"]
    try:
        px = float(ex.fetch_ticker(sym)["last"])
        lim = ex.markets[sym].get("limits") or {}
        amt = max(5.0 / px, float((lim.get("amount") or {}).get("min") or 0) * 1.05)
        before = float((ex.fetch_balance().get("free") or {}).get(base) or 0)
        o = ex.create_market_buy_order(sym, float(ex.amount_to_precision(sym, amt)))
        say(f"BUY sent: {sym} order {o['id']}")
        time.sleep(3)
        got = float((ex.fetch_balance().get("free") or {}).get(base) or 0) - before
        if got <= 0:
            say("buy did not fill yet — check Order history in OKX Demo trading")
            sys.exit(2)
        say(f"bought {got:.8f} {base} (about {got * px:.2f} {q})")
        time.sleep(3)
        s = ex.create_market_sell_order(sym, float(ex.amount_to_precision(sym, got)))
        say(f"SELL sent: {sym} order {s['id']}")
        say("done: both orders are in OKX → Demo trading → Order history")
    except ccxt.BaseError as e:
        say("OKX refused the test order: " + str(e)[-160:])
        sys.exit(2)


def main():
    ap = argparse.ArgumentParser(description="Lunyx market bot")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--test-order", action="store_true", help="demo only: buy ~5 of BTC, sell it back")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
                        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH)])
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    cfg = load_config()
    bot = None
    for attempt in range(2):
        try:
            bot = Bot(cfg, a.live)
            bot.cash()                     # checks the API key right away
            if cfg["mode"] != "paper":
                # trade in the stablecoin the account actually holds (OKX Europe demo: USDC; global: USDT)
                free = bot.mk.ex.fetch_balance().get("free") or {}
                if float(free.get(cfg["quote"]) or 0) < 1:
                    alt = next((q for q in ("USDC", "USDT", "USD", "EUR") if float(free.get(q) or 0) >= 1), None)
                    if not alt:
                        msg = (f"OKX {cfg['mode']} account has no USDC/USDT to trade with. In OKX (Demo trading) check "
                               f"Assets, then start again.")
                        log.error(msg)
                        print(msg, file=sys.stderr)
                        sys.exit(2)
                    log.warning("account holds %s, not %s — switching", alt, cfg["quote"])
                    cfg["quote"] = alt
                    bot.q = alt
                    try:
                        disk = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
                        disk["quote"] = alt
                        CONFIG_PATH.write_text(json.dumps(disk, indent=2))
                    except Exception:
                        pass
            st = bot.st
            if (st.get("killed") or st.get("start_equity") == 0) and not st["results"] and not st["pos"]:
                # a "stop" from a start with no money (nothing was ever traded) is not a real loss: start clean
                for k in ("killed", "start_equity", "peak_equity", "day", "day_start", "halted_until", "pause_until"):
                    st[k] = False if k == "killed" else None
                st["equity_history"] = []
                bot.save()
            break
        except ccxt.BaseError as e:
            txt = str(e)
            # OKX has separate servers: global (okx) and Europe (myokx, my.okx.com). A key made on one
            # "doesn't exist" on the other — switch once and remember the right one.
            if attempt == 0 and "50119" in txt and cfg["exchange"] in ("okx", "myokx"):
                other = "myokx" if cfg["exchange"] == "okx" else "okx"
                log.warning("API key not found on %s — trying %s (OKX %s)", cfg["exchange"], other,
                            "Europe" if other == "myokx" else "global")
                cfg["exchange"] = other
                try:
                    disk = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
                    disk["exchange"] = other
                    CONFIG_PATH.write_text(json.dumps(disk, indent=2))
                except Exception:
                    pass
                continue
            if isinstance(e, ccxt.AuthenticationError) or "50119" in txt or "50111" in txt or "50113" in txt:
                msg = ("OKX rejected the API key: check the key, secret and passphrase in Wallet & keys, and that the key "
                       "was created while in Demo trading. (" + txt[-90:] + ")")
            elif isinstance(e, ccxt.PermissionDenied) or "50110" in txt or "whitelist" in txt.lower():
                msg = ("OKX blocked this PC's IP address: switch off Trusted IP addresses for the key on okx.com, "
                       "or add this PC's IP. (" + txt[-90:] + ")")
            else:
                msg = "OKX error: " + txt[-200:]
            log.error(msg)
            print(msg, file=sys.stderr)
            sys.exit(2)
    if a.test_order:
        test_order(bot)
    elif a.status:
        bot.status()
    elif a.scan:
        seen, n = bot.scan(trade=False)
        rg = bot.regime
        print(f"\n{n} coins checked on {cfg['exchange']}. Market: {'RISK-OFF' if rg['risk_off'] else 'risk-on'}, "
              f"breadth {rg['breadth'] * 100:.0f}%, BTC 1h {rg['btc_h1']}")
        for sym, (name, why) in seen.items():
            print("  " + why)
        if not seen:
            print("  no coin fits any strategy right now")
    else:
        bot.run()


if __name__ == "__main__":
    main()
