"""
Lunyx meme bot — strategy brain (pure logic, no network).

Shared by memebot.py (live/paper) and replay.py (backtests), so what you test
is exactly what trades.

Principles it encodes:
  * Many small, independent bets beat a few big ones (Renaissance).
  * Risk a fixed slice of equity per trade; size = risk / stop distance (PTJ: defense first).
  * Cut losers fast, protect winners with breakeven + trailing stops.
  * Trade with the tide: market regime gates and shrinks exposure (Druckenmiller).
  * Allocate to what works: each strategy's size follows its own recent results,
    and a losing strategy benches itself.
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # bundled Python does not add the script folder
import copy
import time

SOL_MINT = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}

DEFAULTS = {
    "mode": "paper",
    "rpc_url": "https://api.mainnet-beta.solana.com",
    "jupiter_url": "https://api.jup.ag/swap/v1",
    "scan_seconds": 60,
    "manage_seconds": 5,
    "track_max": 300,

    "paper_start_sol": 0.68,
    "sol_reserve": 0.02,
    "max_positions": 5,
    "risk_per_trade_pct": 2.0,        # equity lost if a trade hits its stop
    "max_position_pct": 20.0,         # never more than this of equity in one coin
    "min_position_sol": 0.01,
    "max_position_sol": 0.5,          # hard cap per trade, whatever the equity
    "live_budget_sol": 0.0,           # live mode: the most SOL the bot may use, whatever the wallet holds
    "slippage_bps": 300,
    "priority_fee_lamports": 200000,
    "max_price_impact_pct": 3.0,
    "edge_gate": True,                # rule strategies trade only when the recorded data proves they make money
    "edge_min_signals": 8,
    "edge_min_avg_pct": 0.5,
    "edge_min_win": 0.5,
    "edge_probation_min_signals": 3,  # promising strategies trade at half size before they have 8 signals
    "max_round_trip_loss_pct": 8.0,
    "max_jump_2min_pct": 8.0,          # skip coins that spiked this much in the last 2-3 minutes   # buy-then-sell quote must not lose more than this (honeypot / hidden tax check)
    "max_per_theme": 2,               # at most this many open coins sharing a theme word (GTA, PEPE, ...)
    "paper_tx_fee_sol": 0.00015,      # paper: network + priority fee per swap

    # filters every strategy must pass
    "base": {
        "min_liquidity_usd": 25000,
        "min_volume_h1_usd": 20000,
        "min_mcap_usd": 100000,
        "max_mcap_usd": 50000000,
        "min_age_minutes": 20,
        "max_age_hours": 168,
        "min_liq_mcap_ratio": 0.04,
    },

    # safety (on-chain + RugCheck), checked right before any buy
    "require_revoked_authorities": True,
    "max_rugcheck_score": 1000,
    "max_top_holder_pct": 25.0,
    "min_lp_locked_pct": 80,

    # market regime
    "regime_min_breadth": 0.35,       # share of active coins up over 1h
    "regime_min_sol_h1_pct": -2.5,
    "risk_off_size_mult": 0.5,

    # adaptive allocation
    "adapt_window": 20,
    "adapt_min_trades": 6,
    "adapt_bench_after": 10,
    "adapt_bench_expectancy_pct": -3.0,
    "adapt_bench_hours": 6,
    "max_consecutive_losses": 4,
    "loss_streak_pause_minutes": 30,

    "jackpot_pct": 50,
    "liquidity_drop_exit_pct": 40.0,
    "max_hold_hours": 8,
    "rebuy_cooldown_hours": 12,
    "max_daily_loss_sol": 0.14,
    "max_drawdown_pct": 40.0,

    "strategies": {
        "momentum": {
            "enabled": True,
            "min_liquidity_usd": 50000, "min_volume_h1_usd": 40000,
            "h1_min": 5, "h1_max": 60, "m5_min": -8, "m5_max": 8, "h6_min": 0, "min_buy_ratio": 1.3,
            "confirm_scans": 2, "confirm_max_run_pct": 15, "confirm_min_run_pct": -10,
            "stop_loss_pct": 15, "take_profit_pct": 25, "take_profit_sell_pct": 50,
            "trail_activate_pct": 8, "trailing_stop_pct": 12,
            "breakeven_trigger_pct": 15, "breakeven_lock_pct": 2,
            "stale_minutes": 90, "stale_min_gain_pct": 5,
        },
        "dip": {
            "enabled": True,
            "min_liquidity_usd": 50000, "min_volume_h1_usd": 50000,
            "h1_min": 20, "h1_max": 200, "h6_min": 20, "m5_min": -15, "m5_max": -3, "min_buy_ratio": 0.9,
            "confirm_scans": 2, "confirm_max_run_pct": 8, "confirm_min_run_pct": -3,
            "stop_loss_pct": 12, "take_profit_pct": 20, "take_profit_sell_pct": 50,
            "trail_activate_pct": 6, "trailing_stop_pct": 10,
            "breakeven_trigger_pct": 10, "breakeven_lock_pct": 1,
            "stale_minutes": 60, "stale_min_gain_pct": 4,
        },
        "breakout": {
            "enabled": True,
            "min_liquidity_usd": 30000, "min_volume_h1_usd": 30000,
            "volume_accel": 2.5, "m5_min": 2, "m5_max": 12, "h1_min": -10, "h1_max": 80, "h6_min": -100,
            "min_buy_ratio": 1.5,
            "confirm_scans": 1, "confirm_max_run_pct": 100, "confirm_min_run_pct": -100,
            "stop_loss_pct": 10, "take_profit_pct": 20, "take_profit_sell_pct": 50,
            "trail_activate_pct": 6, "trailing_stop_pct": 8,
            "breakeven_trigger_pct": 10, "breakeven_lock_pct": 1,
            "stale_minutes": 30, "stale_min_gain_pct": 3,
        },
        "scalp": {
            # found in recorded data (11/12 wins, +4.2% avg after costs, both halves positive):
            # a coin already in a strong hourly uptrend, buyers in control, still rising → take a quick +6% and leave.
            # On probation: small size; the edge gate + auto-bench switch it off if live results turn negative.
            "enabled": True,
            "min_liquidity_usd": 40000, "min_volume_h1_usd": 20000,
            "h1_min": 30, "h1_max": 150, "m5_min": 0, "m5_max": 8, "h6_min": -100, "min_buy_ratio": 1.2,
            "min_txns_m5": 30, "confirm_scans": 1, "confirm_max_run_pct": 100, "confirm_min_run_pct": -100,
            "stop_loss_pct": 8, "take_profit_pct": 6, "take_profit_sell_pct": 100,
            "trail_activate_pct": 100, "trailing_stop_pct": 100,
            "breakeven_trigger_pct": 4, "breakeven_lock_pct": 0.5,
            "stale_minutes": 45, "stale_min_gain_pct": 100, "profit_lock_keep_pct": 0,
            "max_position_pct": 15,
        },
        # regular-coin strategies: smaller moves, tighter stops, quicker targets (like an exchange trader)
        "maj_momentum": {"enabled": True, "min_liquidity_usd": 60000, "min_volume_h1_usd": 10000,
                         "h1_min": 1.5, "h1_max": 12, "m5_min": -1.0, "m5_max": 2.5, "h6_min": 0, "min_buy_ratio": 1.2,
                         "min_txns_m5": 20, "confirm_scans": 2, "confirm_max_run_pct": 2, "confirm_min_run_pct": -1.5,
                         "stop_loss_pct": 3.0, "take_profit_pct": 5.0, "take_profit_sell_pct": 50,
                         "trail_activate_pct": 2.0, "trailing_stop_pct": 2.0, "breakeven_trigger_pct": 2.5, "breakeven_lock_pct": 0.3,
                         "stale_minutes": 120, "stale_min_gain_pct": 1.0, "max_position_pct": 25},
        "maj_dip": {"enabled": True, "min_liquidity_usd": 60000, "min_volume_h1_usd": 10000,
                    "h1_min": 3, "h1_max": 20, "h6_min": 4, "m5_min": -4, "m5_max": -0.8, "min_buy_ratio": 0.9,
                    "min_txns_m5": 20, "confirm_scans": 2, "confirm_max_run_pct": 1.5, "confirm_min_run_pct": -0.6,
                    "stop_loss_pct": 3.0, "take_profit_pct": 4.0, "take_profit_sell_pct": 50,
                    "trail_activate_pct": 1.5, "trailing_stop_pct": 1.5, "breakeven_trigger_pct": 2.0, "breakeven_lock_pct": 0.2,
                    "stale_minutes": 90, "stale_min_gain_pct": 1.0, "max_position_pct": 25},
        "maj_breakout": {"enabled": True, "min_liquidity_usd": 60000, "min_volume_h1_usd": 10000, "volume_accel": 3.0,
                         "m5_min": 0.8, "m5_max": 4, "h1_min": -3, "h1_max": 10, "h6_min": -100, "min_buy_ratio": 1.5,
                         "min_txns_m5": 25, "confirm_scans": 1, "confirm_max_run_pct": 100, "confirm_min_run_pct": -100,
                         "stop_loss_pct": 2.5, "take_profit_pct": 5.0, "take_profit_sell_pct": 50,
                         "trail_activate_pct": 1.5, "trailing_stop_pct": 1.5, "breakeven_trigger_pct": 2.0, "breakeven_lock_pct": 0.2,
                         "stale_minutes": 45, "stale_min_gain_pct": 0.8, "max_position_pct": 25},
        "rocket": {
            # catches an explosion in its first minutes: price jumping, volume 4x its normal pace,
            # buyers swamping sellers. Checked every 5 s on hot coins. Buys at once, exits fast.
            "enabled": True,
            "min_liquidity_usd": 30000, "min_volume_h1_usd": 20000,
            "volume_accel": 4.0, "m5_min": 8, "m5_max": 40, "h1_min": -20, "h1_max": 150, "h6_min": -100,
            "min_buy_ratio": 1.8,
            "confirm_scans": 1, "confirm_max_run_pct": 100, "confirm_min_run_pct": -100,
            "stop_loss_pct": 8, "take_profit_pct": 30, "take_profit_sell_pct": 50,
            "trail_activate_pct": 6, "trailing_stop_pct": 8,
            "breakeven_trigger_pct": 10, "breakeven_lock_pct": 1,
            "stale_minutes": 10, "stale_min_gain_pct": 4,
            "max_position_pct": 12,
        },
        "hype": {
            # bought when a watched VIP (Elon, Trump, ...) posts a word matching the coin, and it passes
            # every filter + safety check; skips coins that already exploded in the last 5 minutes
            "enabled": True,
            "max_m5_pct": 25, "min_liquidity_usd": 40000,
            "stop_loss_pct": 12, "take_profit_pct": 25, "take_profit_sell_pct": 50,
            "trail_activate_pct": 8, "trailing_stop_pct": 10,
            "breakeven_trigger_pct": 12, "breakeven_lock_pct": 1,
            "stale_minutes": 30, "stale_min_gain_pct": 5,
            "max_position_pct": 10,
        },
        "runner_new": {"light_safety": True, "enabled": False, "min_liquidity_usd": 20000, "min_volume_h1_usd": 20000, "m5_min": 4, "m5_max": 12, "h1_min": 20, "h1_max": 220, "h6_min": -20, "min_buy_ratio": 1.2, "volume_accel": 2.0, "min_txns_m5": 0, "confirm_scans": 1, "confirm_max_run_pct": 100, "confirm_min_run_pct": -100, "stop_loss_pct": 12, "take_profit_pct": 30, "take_profit_sell_pct": 100, "trail_activate_pct": 15, "trailing_stop_pct": 10, "breakeven_trigger_pct": 1000, "breakeven_lock_pct": 0, "profit_lock_keep_pct": 0, "stale_minutes": 60, "stale_min_gain_pct": 1000, "max_position_pct": 8, "vol_stop": False, "base": {"min_mcap_usd": 30000, "max_mcap_usd": 100000, "min_age_minutes": 5, "max_age_hours": 1, "min_liq_mcap_ratio": 0.5}},
        "surge": {"enabled": True, "min_liquidity_usd": 80000, "min_volume_h1_usd": 20000, "m5_min": 0, "m5_max": 12, "h1_min": 40, "h1_max": 120, "h6_min": 20, "min_buy_ratio": 1.2, "volume_accel": 0.8, "min_txns_m5": 0, "confirm_scans": 1, "confirm_max_run_pct": 100, "confirm_min_run_pct": -100, "stop_loss_pct": 12, "take_profit_pct": 20, "take_profit_sell_pct": 100, "trail_activate_pct": 1000, "trailing_stop_pct": 1000, "breakeven_trigger_pct": 1000, "breakeven_lock_pct": 0, "profit_lock_keep_pct": 0, "stale_minutes": 5, "stale_min_gain_pct": 1000, "max_position_pct": 10, "vol_stop": False},
        "runner": {"enabled": True, "min_liquidity_usd": 20000, "min_volume_h1_usd": 20000, "m5_min": 4, "m5_max": 12, "h1_min": 20, "h1_max": 220, "h6_min": -20, "min_buy_ratio": 1.2, "volume_accel": 2.0, "min_txns_m5": 0, "confirm_scans": 1, "confirm_max_run_pct": 100, "confirm_min_run_pct": -100, "stop_loss_pct": 12, "take_profit_pct": 20, "take_profit_sell_pct": 100, "trail_activate_pct": 1000, "trailing_stop_pct": 1000, "breakeven_trigger_pct": 1000, "breakeven_lock_pct": 0, "profit_lock_keep_pct": 0, "stale_minutes": 30, "stale_min_gain_pct": 1000, "max_position_pct": 12, "vol_stop": False},
        "smart": {
            # bought when 2+ proven early-buyer wallets (smart money) bought the same coin in the last 20 min
            "enabled": True, "min_liquidity_usd": 30000, "max_m5_pct": 20, "max_h1_pct": 300,
            "stop_loss_pct": 12, "take_profit_pct": 25, "take_profit_sell_pct": 50,
            "trail_activate_pct": 8, "trailing_stop_pct": 10,
            "breakeven_trigger_pct": 10, "breakeven_lock_pct": 1,
            "stale_minutes": 30, "stale_min_gain_pct": 5,
            "max_position_pct": 10,
        },
        "predict": {
            # bought only when the look-ahead model (model.py) is active and confident;
            # exits mirror what the model predicts: +8% before -6% within 15 minutes
            "enabled": True,
            "stop_loss_pct": 6, "take_profit_pct": 8, "take_profit_sell_pct": 50,
            "trail_activate_pct": 4, "trailing_stop_pct": 5,
            "breakeven_trigger_pct": 6, "breakeven_lock_pct": 1,
            "stale_minutes": 15, "stale_min_gain_pct": 2,
            "max_position_pct": 15,       # unproven: smaller cap than the rule strategies
            "min_history_minutes": 10,    # needs 10 minutes of its own price history before trusting a score
        },
    },
    # who owns the coin (RugCheck full report): dev bag, insider/bundled wallets, concentration
    "holder_check": {"enabled": True, "max_creator_pct": 10, "max_insider_pct": 15,
                     "max_single_holder_pct": 12, "max_top10_pct": 40, "min_holders": 50},
    # smart money: wallets that bought several winners early; the bot follows when 2+ of them buy the same coin
    "smart_money": {"enabled": True, "min_wins": 2, "max_wallets": 40, "min_wallets_to_buy": 2,
                    "window_min": 20, "learn_min_h6_pct": 150, "learn_min_liq_usd": 20000,
                    "boost": 0.15, "rpc_pause_s": 0.35},
    "vol_stops": {"enabled": True, "noise_mult": 2.5, "min_frac": 0.7, "max_frac": 1.4},
    "model_gate": True,            # when the model is active, skip rule-based buys it rates in its bottom 40%
    "strategy_priority": ["surge", "runner", "scalp", "rocket", "breakout", "dip", "momentum"],

    # REGULAR TRADING inside the same bot: established Solana coins (wrapped BTC/ETH, DeFi blue chips,
    # the biggest long-lived memes). Same wallet, same balance, same Jupiter swaps, same edge gate.
    "majors": {
        "enabled": True,
        "tokens": {
            "cbBTC": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij", "WETH": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
            "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
            "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3", "JTO": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
            "ORCA": "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE", "RENDER": "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof",
            "HNT": "hntyVP6YFm1Hg25TN9WGLqM12b8TQmcknKrdu1oxWux", "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
            "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", "POPCAT": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
            "PENGU": "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv", "FARTCOIN": "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",
        },
        "base": {"min_liquidity_usd": 60000, "min_volume_h1_usd": 5000, "min_mcap_usd": 0, "max_mcap_usd": 1e15,
                 "min_age_minutes": 0, "max_age_hours": 1e9, "min_liq_mcap_ratio": 0},
        "priority": ["maj_breakout", "maj_dip", "maj_momentum"],
    },
    "fast_scan_seconds": 5,

    "ntfy_topic": "",
    "record_data": True,
}


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def set_path(cfg, path, value):
    d = cfg
    keys = path.split(".")
    for k in keys[:-1]:
        d = d[k]
    d[keys[-1]] = value


# ============================================================================ features

def features(pair, now_ms=None):
    ch = pair.get("priceChange") or {}
    vol = pair.get("volume") or {}
    tx = (pair.get("txns") or {}).get("m5") or {}
    liq = (pair.get("liquidity") or {}).get("usd") or 0
    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    buys, sells = tx.get("buys", 0) or 0, tx.get("sells", 0) or 0
    vh1, vm5 = vol.get("h1") or 0, vol.get("m5") or 0
    created = pair.get("pairCreatedAt")
    now_ms = now_ms or time.time() * 1000
    return {
        "sym": (pair.get("baseToken") or {}).get("symbol", "?"),
        "quote": (pair.get("quoteToken") or {}).get("address"),
        "price": float(pair.get("priceUsd") or 0),
        "liq": liq, "mcap": mcap, "vol_h1": vh1, "vol_m5": vm5,
        "m5": ch.get("m5"), "h1": ch.get("h1"), "h6": ch.get("h6"),
        "buys": buys, "sells": sells,
        "ratio": buys / sells if sells else (99.0 if buys else 0.0),
        "accel": (vm5 * 12 / vh1) if vh1 else 0.0,
        "age_min": (now_ms - created) / 60000 if created else None,
    }


def base_ok(cfg, f):
    b = cfg["base"]
    if f["quote"] != SOL_MINT and f["quote"] not in STABLES:
        return False, "not paired with SOL/USDC/USDT"
    if f["price"] <= 0:
        return False, "no price"
    if f["liq"] < b["min_liquidity_usd"]:
        return False, f"liquidity ${f['liq']:,.0f} too low"
    if f["vol_h1"] < b["min_volume_h1_usd"]:
        return False, f"1h volume ${f['vol_h1']:,.0f} too low"
    if not (b["min_mcap_usd"] <= f["mcap"] <= b["max_mcap_usd"]):
        return False, f"market cap ${f['mcap']:,.0f} out of range"
    if f["age_min"] is None or not (b["min_age_minutes"] <= f["age_min"] <= b["max_age_hours"] * 60):
        return False, f"age {f['age_min'] or 0:.0f} min out of range"
    if f["mcap"] and f["vol_h1"] / f["mcap"] > b.get("max_vol_mcap_h1", 5):
        return False, f"1h volume {f['vol_h1'] / f['mcap']:.0f}x its market cap (wash trading)"
    if f["mcap"] and f["liq"] / f["mcap"] < b["min_liq_mcap_ratio"]:
        return False, f"pool thin vs market cap ({f['liq'] / f['mcap']:.1%})"
    return True, ""


def signal(name, s, f):
    """Does strategy `name` with settings `s` fire on features `f`?"""
    if f["liq"] < s["min_liquidity_usd"] or f["vol_h1"] < s["min_volume_h1_usd"]:
        return False
    m5, h1, h6 = f["m5"], f["h1"], f["h6"]
    if m5 is None or h1 is None:
        return False
    if not (s["m5_min"] <= m5 <= s["m5_max"] and s["h1_min"] <= h1 <= s["h1_max"]):
        return False
    if h6 is not None and h6 < s["h6_min"]:
        return False
    if f["ratio"] < s["min_buy_ratio"] or f["buys"] < 10:
        return False
    # a "trend" made of a handful of trades can be faked by a few wallets: require real activity
    if f["buys"] + f["sells"] < s.get("min_txns_m5", 40):
        return False
    if "volume_accel" in s and f["accel"] < s["volume_accel"]:
        return False
    if s.get("learned"):
        import lessons
        if lessons.check_learned(s, f):
            return False
    return True


def describe(name, f):
    extra = f", vol x{f['accel']:.1f}" if name == "breakout" else ""
    return (f"[{name}] {f['sym']}: liq ${f['liq']:,.0f}, vol1h ${f['vol_h1']:,.0f}, mcap ${f['mcap']:,.0f}, "
            f"1h {f['h1']:+.0f}%, 5m {f['m5']:+.0f}%, {f['buys']}/{f['sells']} buys/sells{extra}")


def entry_step(cfg, pair, w, now_s, allowed, base=None, priority=None):
    """One scan's decision for one coin. `base`/`priority` override the filters and strategy order
    (used for regular coins). Returns (action, strategy, reason, new_watch): BUY / WATCH / WAIT / DROP / NONE."""
    f = features(pair, now_s * 1000)
    ok, why = base_ok(dict(cfg, base=base) if base else cfg, f)
    own = [n for n in (priority or cfg["strategy_priority"])
           if not base and (cfg["strategies"].get(n) or {}).get("base") and n in allowed]
    if not ok and not own:
        return "NONE", None, f"{f['sym']}: {why}", None
    for name in (priority or cfg["strategy_priority"]):
        s = cfg["strategies"].get(name)
        if not s or not s.get("enabled") or name not in allowed:
            continue
        if s.get("base") and not base:          # this strategy has its own coin filters (e.g. fresh small coins)
            if not base_ok(dict(cfg, base={**cfg["base"], **s["base"]}), f)[0]:
                continue
        elif not ok:
            continue
        if not signal(name, s, f):
            continue
        reason = describe(name, f)
        if s["confirm_scans"] <= 1:
            return "BUY", name, reason, None
        if not w or w.get("strategy") != name or now_s - w["first"] > 15 * 60:
            return "WATCH", name, reason, {"strategy": name, "first": now_s, "last": now_s, "price": f["price"], "hits": 1}
        if now_s - w["last"] < 45:
            return "WAIT", name, reason, w
        run = (f["price"] / w["price"] - 1) * 100 if w["price"] else 0
        if not (s["confirm_min_run_pct"] <= run <= s["confirm_max_run_pct"]):
            return "DROP", name, f"{f['sym']}: moved {run:+.0f}% while confirming", None
        w = dict(w, hits=w["hits"] + 1, last=now_s)
        if w["hits"] >= s["confirm_scans"]:
            return "BUY", name, f"{reason}, confirmed over {(now_s - w['first']) / 60:.0f} min ({run:+.0f}%)", None
        return "WATCH", name, reason, w
    return "NONE", None, f"{f['sym']}: no strategy fits", None


# ============================================================================ exits

def exit_step(cfg, s, p, value, liq, now_s):
    """p: entry (cost basis of what's held), peak, opened (epoch s), took_profit, entry_liq.
    Returns (fraction_to_sell, reason) — (0, None) to hold."""
    entry, peak = p["entry"], max(p["peak"], value)
    chg = (value / entry - 1) * 100 if entry else 0
    peak_gain = (peak / entry - 1) * 100 if entry else 0
    from_peak = (value / peak - 1) * 100 if peak else 0
    held_min = (now_s - p["opened"]) / 60
    if liq is not None and p.get("entry_liq") and liq < p["entry_liq"] * (1 - cfg["liquidity_drop_exit_pct"] / 100):
        return 1.0, f"liquidity pulled ({p['entry_liq']:,.0f} → {liq:,.0f} USD)"
    if chg <= -s["stop_loss_pct"]:
        return 1.0, f"stop-loss {chg:.0f}%"
    jackpot = s.get("jackpot_pct", cfg.get("jackpot_pct", 50))
    if jackpot and chg >= jackpot:
        # a huge fast gain on a meme coin is where rug pulls happen: bank all of it
        return 1.0, f"jackpot +{chg:.0f}% — banked before a rug can take it"
    if peak_gain >= min(s["breakeven_trigger_pct"], s["trail_activate_pct"]) and chg <= s["breakeven_lock_pct"]:
        # once the trailing stop is armed, a winner may not turn into a loss
        return 1.0, f"breakeven stop (was +{peak_gain:.0f}%, now {chg:+.0f}%)"
    keep = s.get("profit_lock_keep_pct", 50)
    if keep and peak_gain >= s["trail_activate_pct"] and chg <= peak_gain * keep / 100 and chg > s["breakeven_lock_pct"]:
        # profit lock: once it has run, always keep at least half of the best gain
        return 1.0, f"profit lock (was +{peak_gain:.1f}%, kept {chg:+.1f}%)"
    if not p["took_profit"] and chg >= s["take_profit_pct"]:
        return s["take_profit_sell_pct"] / 100, f"take-profit +{chg:.0f}%"
    if peak_gain >= s["trail_activate_pct"] and from_peak <= -s["trailing_stop_pct"]:
        return 1.0, f"trailing stop ({from_peak:.0f}% from peak, {chg:+.0f}% overall)"
    if held_min >= s["stale_minutes"] and chg < s["stale_min_gain_pct"] and not p["took_profit"]:
        return 1.0, f"no momentum after {held_min:.0f} min ({chg:+.0f}%)"
    if held_min >= cfg["max_hold_hours"] * 60:
        return 1.0, f"held {held_min / 60:.1f}h ({chg:+.0f}%)"
    return 0.0, None


# ============================================================================ regime + sizing

def regime(cfg, pairs: dict, sol_pair=None):
    active = [p for p in pairs.values() if ((p.get("volume") or {}).get("h1") or 0) >= 20000
              and (p.get("priceChange") or {}).get("h1") is not None]
    breadth = sum(1 for p in active if p["priceChange"]["h1"] > 0) / len(active) if active else 0.5
    sol_h1 = ((sol_pair or {}).get("priceChange") or {}).get("h1")
    risk_off = breadth < cfg["regime_min_breadth"] or (sol_h1 is not None and sol_h1 < cfg["regime_min_sol_h1_pct"])
    return {"risk_off": risk_off, "breadth": breadth, "sol_h1": sol_h1, "active": len(active)}


def strategy_stats(cfg, results, name):
    r = [x["ret_pct"] for x in results if x["strategy"] == name][-cfg["adapt_window"]:]
    n = len(r)
    return {"n": n, "expectancy": sum(r) / n if n else 0.0, "win_rate": sum(1 for x in r if x > 0) / n if n else 0.0}


def size_mult(cfg, stats):
    """Allocate to what works."""
    if stats["n"] < cfg["adapt_min_trades"]:
        return 1.0
    e = stats["expectancy"]
    if e < 0:
        return 0.5
    if e > 3:
        return 1.5
    return 1.0


def benched(cfg, stats):
    return stats["n"] >= cfg["adapt_bench_after"] and stats["expectancy"] < cfg["adapt_bench_expectancy_pct"]


def vol_stop(cfg, s, vol10):
    """Stop-loss fitted to the coin's own noise: ~2.5x its typical 5-minute swing, kept within 70-140% of the
    strategy's stop. Calm coins get a tighter stop, wild ones a wider stop (and a smaller size). None = keep default."""
    v = cfg.get("vol_stops", {})
    if not v.get("enabled", True) or not vol10 or s.get("vol_stop") is False:
        return None
    base = s["stop_loss_pct"]
    want = v.get("noise_mult", 2.5) * vol10 * 100 * 5 ** 0.5
    return round(max(base * v.get("min_frac", 0.7), min(base * v.get("max_frac", 1.4), want)), 1)


def position_size(cfg, s, equity, cash, mult):
    """Risk-based sizing: lose ~risk_per_trade_pct of equity if the stop hits."""
    raw = equity * cfg["risk_per_trade_pct"] / 100 / (s["stop_loss_pct"] / 100) * mult
    cap = equity * min(cfg["max_position_pct"], s.get("max_position_pct", 100)) / 100
    return max(0.0, min(raw, cap, cfg["max_position_sol"], cash - cfg["sol_reserve"]))


# ============================================================================ conviction sizing

def conviction(cfg, f, name, breadth, vol10=None, prob=None, model=None, buzz=False):
    """How much to bet on this particular trade: 0.4x (risky, bet small) … 1.6x (strong, bet big).
    Returns (multiplier, short reasons). Applied on top of risk-based sizing; max-per-coin caps still apply."""
    if not cfg.get("conviction_sizing", True):
        return 1.0, []
    score, why = 0.0, []

    def add(x, text):
        nonlocal score
        score += x
        why.append(f"{'+' if x > 0 else '−'}{text}")

    if f["liq"] >= 150000: add(0.15, "deep pool")
    elif f["liq"] < 40000: add(-0.20, "thin pool")
    if f["vol_h1"] >= 200000: add(0.10, "heavy volume")
    elif f["vol_h1"] < 30000: add(-0.15, "light volume")
    if name != "dip":
        if f["ratio"] >= 2.0: add(0.15, "strong buying")
        elif f["ratio"] < 1.2: add(-0.10, "weak buying")
    if f["mcap"] and f["mcap"] < 1e9:                 # big coins trade across many venues: pool vs mcap means little
        r = f["liq"] / f["mcap"]
        if r >= 0.15: add(0.10, "pool big vs mcap")
        elif r < 0.06: add(-0.10, "pool small vs mcap")
    if f["age_min"] is not None:
        if f["age_min"] < 120: add(-0.15, "very new coin")
        elif f["age_min"] > 24 * 60: add(0.05, "survived a day")
    if f["h1"] is not None and f["h1"] > 40: add(-0.15, "already ran hard")
    if breadth >= 0.6: add(0.10, "market rising")
    elif breadth < 0.4: add(-0.15, "market weak")
    if vol10 is not None:
        if vol10 > 0.06: add(-0.20, "very jumpy")
        elif vol10 < 0.02: add(0.05, "steady")
    if buzz:
        add(0.15, "social buzz")
    if prob is not None and model:
        edge = prob - model.get("base_rate", 0.4)
        if edge > 0.1: add(min(0.25, edge), "model likes it")
        elif edge < -0.05: add(max(-0.25, edge), "model doubts it")
    mult = max(0.4, min(cfg.get("conviction_max", 1.6), 1.0 + score))
    return mult, why


# ============================================================================ live guard

LIVE_LIMITS = [
    # (path, kind, limit, why) — kind "max": value is capped; "min": value is raised
    ("risk_per_trade_pct", "max", 2.0, "risk per trade"),
    ("max_position_pct", "max", 20.0, "max in one coin"),
    ("max_positions", "max", 4, "coins at once"),
    ("max_price_impact_pct", "max", 1.5, "price impact"),
    ("slippage_bps", "max", 200, "slippage"),
    ("max_consecutive_losses", "max", 3, "losing-streak pause"),
    ("max_round_trip_loss_pct", "max", 6.0, "honeypot / tax check"),
    ("max_drawdown_pct", "max", 30.0, "kill switch"),
    ("regime_min_breadth", "min", 0.35, "market-mood filter"),
    ("base.min_liquidity_usd", "min", 40000, "min pool"),
    ("base.min_volume_h1_usd", "min", 25000, "min volume"),
    ("base.min_age_minutes", "min", 30, "min coin age"),
    ("base.min_liq_mcap_ratio", "min", 0.05, "pool vs market cap"),
    ("strategies.momentum.confirm_scans", "min", 2, "momentum confirmation"),
    ("strategies.momentum.min_txns_m5", "min", 80, "momentum min trades per 5 min"),
    ("strategies.breakout.min_txns_m5", "min", 60, "breakout min trades per 5 min"),
    ("strategies.rocket.min_txns_m5", "min", 60, "rocket min trades per 5 min"),
    ("strategies.momentum.h1_max", "max", 60, "momentum max 1h run"),
    ("strategies.breakout.h1_max", "max", 50, "breakout max 1h run"),
    ("strategies.predict.max_position_pct", "max", 10, "model trade size"),
    ("strategies.hype.max_position_pct", "max", 8, "VIP-post trade size"),
    ("strategies.rocket.max_position_pct", "max", 10, "rocket trade size"),
    ("strategies.rocket.min_liquidity_usd", "min", 40000, "rocket min pool"),
]


def _get(cfg, path):
    d = cfg
    for k in path.split("."):
        d = d[k]
    return d


def apply_live_guard(cfg):
    """Real money: clamp every risky setting to a safe range, whatever config.json says.
    Returns the list of changes so the bot can log them."""
    changes = []
    for path, kind, lim, label in LIVE_LIMITS:
        try:
            v = _get(cfg, path)
        except KeyError:
            continue
        if (kind == "max" and v > lim) or (kind == "min" and v < lim):
            set_path(cfg, path, lim)
            changes.append(f"{label}: {v} → {lim}")
    # daily loss limit: never more than 10% of the live budget
    budget = cfg.get("live_budget_sol") or 0
    if budget and cfg["max_daily_loss_sol"] > budget * 0.10:
        changes.append(f"daily loss limit: {cfg['max_daily_loss_sol']} → {budget * 0.10:.4f} SOL (10% of budget)")
        cfg["max_daily_loss_sol"] = round(budget * 0.10, 4)
    if cfg.get("conviction_max", 1.6) > 1.25:
        cfg["conviction_max"] = 1.25
        changes.append("smart sizing max: 1.6× → 1.25×")
    return changes
