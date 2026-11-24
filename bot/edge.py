"""
Edge gate — the bot only trades a strategy that has PROVEN it makes money after costs.

Every time the bot re-tests itself (every 30 min, in the background), this replays every moment in the
recorded market data where each rule-based strategy (momentum, dip, breakout, rocket) would have bought,
and measures what actually happened next with that strategy's own stop-loss, take-profit and time limit,
after fees and slippage.

A strategy may trade with real (or paper) money only if, over the recorded data:
  * it fired at least `edge_min_signals` times (enough evidence, spread over different coins),
  * it made money on average after costs (≥ `edge_min_avg_pct`), and
  * it ALSO made money in the most recent half of the data (the market changes; old wins don't count alone).

Otherwise it runs in SHADOW: the bot logs "would have bought X", but doesn't buy. When the data shows a
strategy works again, it switches back on by itself. Nothing proven = nothing traded.
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # bundled Python does not add the script folder
import json
import statistics as st
import time
from collections import defaultdict
from pathlib import Path

import strategy as S

HERE = Path(__file__).resolve().parent
EDGE_PATH = HERE / "data" / "edge.json"
RULES = ["runner", "runner_new", "scalp", "momentum", "dip", "breakout", "rocket", "maj_momentum", "maj_dip", "maj_breakout"]


def _as_pair(r):
    return {"baseToken": {"address": r["mint"], "symbol": r["symbol"]}, "quoteToken": {"address": r["quote"]},
            "priceUsd": r["price"], "liquidity": {"usd": r["liq"]}, "marketCap": r["mcap"], "fdv": r["mcap"],
            "volume": r["vol"], "priceChange": r["chg"], "txns": r["txns"], "pairCreatedAt": r["created"]}


def _barrier(rows, i, stop, tp, horizon_min):
    """What a trade with this stop / take-profit / time limit would have returned (%, before costs).
    Gaps count: if the price jumps past the stop, the loss is the real jump, not the stop level."""
    t, r = rows[i]
    p0 = float(r["price"])
    last = 0.0
    for t2, r2 in rows[i + 1:]:
        if t2 - t > horizon_min * 60:
            break
        ch = (float(r2["price"]) / p0 - 1) * 100
        last = ch
        if ch >= tp:
            return tp
        if ch <= -stop:
            return max(ch, -100.0)
    return last


SAFETY_PATH = HERE / "data" / "safety.json"


def safety_verdicts(cfg, mints, light=False):
    """The live bot only buys coins that pass its safety checks (RugCheck, mint/freeze authority, LP lock),
    so the evidence must skip coins that fail them — otherwise a rug the bot would never touch counts as a loss.
    Results are cached in data/safety.json."""
    try:
        cache = json.loads(SAFETY_PATH.read_text())
    except Exception:
        cache = {}
    pre = "light:" if light else ""
    todo = [m for m in mints if pre + m not in cache]
    if todo:
        try:
            import memebot as M
            chain = M.Solana(cfg["rpc_url"])
            for m in todo:
                ok, why = M._safety(cfg, chain, m, m[:6], light)
                cache[pre + m] = {"ok": ok, "why": why, "t": int(time.time())}
                time.sleep(0.3)
        except Exception as ex:
            print(f"safety lookups failed: {ex}")
        SAFETY_PATH.parent.mkdir(exist_ok=True)
        SAFETY_PATH.write_text(json.dumps(cache))
    return {m: cache.get(pre + m, {}).get("ok", True) for m in mints}


def measure(cfg, ticks, cost_pct=1.0, check_safety=True):
    """→ {strategy: {"n", "coins", "avg", "recent", "win", "examples"}} from recorded snapshots."""
    mins = list(ticks)
    if not mins:
        return {}
    t_end = mins[-1]
    half = (mins[0] + t_end) / 2
    series = defaultdict(list)
    for t in mins:
        for m, r in ticks[t].items():
            if r.get("kind") != "sol" and r.get("price"):
                series[m].append((t, r))
    out = {n: [] for n in RULES}
    last = {}
    maj = cfg.get("majors") or {}
    maj_mints = set((maj.get("tokens") or {}).values())
    maj_cfg = dict(cfg, base=maj.get("base") or cfg["base"])
    for m, rows in series.items():
        is_major = m in maj_mints
        for i, (t, r) in enumerate(rows):
            f = S.features(_as_pair(r), t * 1000)
            ok_base = S.base_ok(maj_cfg if is_major else cfg, f)[0]
            for n in RULES:
                if n.startswith("maj_") != is_major:          # regular-coin rules only on regular coins, and vice versa
                    continue
                s = cfg["strategies"].get(n)
                if not s or not s.get("enabled", True):
                    continue
                if s.get("base") and not is_major:
                    if not S.base_ok(dict(cfg, base={**cfg["base"], **s["base"]}), f)[0]:
                        continue
                elif not ok_base:
                    continue
                if t_end - t < s["stale_minutes"] * 60:          # outcome not known yet
                    continue
                if t - last.get((n, m), -1e18) < 1800:           # one signal per coin per 30 min
                    continue
                if S.signal(n, s, f):
                    last[(n, m)] = t
                    o = _barrier(rows, i, s["stop_loss_pct"], s["take_profit_pct"], s["stale_minutes"]) - cost_pct
                    out[n].append((t, m, o, f["sym"]))
    if check_safety:
        light_rules = {n for n in out if (cfg["strategies"].get(n) or {}).get("light_safety")}
        safe = safety_verdicts(cfg, sorted({x[1] for n, xs in out.items() if n not in light_rules for x in xs
                                            if x[1] not in maj_mints}))
        safe_l = safety_verdicts(cfg, sorted({x[1] for n, xs in out.items() if n in light_rules for x in xs
                                              if x[1] not in maj_mints}), light=True) if light_rules else {}
        out = {n: [x for x in xs if (safe_l if n in light_rules else safe).get(x[1], True)] for n, xs in out.items()}
    stats = {}
    for n, xs in out.items():
        rs = [x[2] for x in xs]
        rec = [x[2] for x in xs if x[0] >= half]
        stats[n] = {"n": len(rs), "coins": len({x[1] for x in xs}),
                    "avg": round(st.mean(rs), 2) if rs else None,
                    "sd": round(st.pstdev(rs), 2) if len(rs) > 1 else None,
                    "recent": round(st.mean(rec), 2) if rec else None, "recent_n": len(rec),
                    "win": round(sum(1 for v in rs if v > 0) / len(rs), 2) if rs else None,
                    "examples": [(x[3], round(x[2], 1)) for x in xs[-5:]]}
    return stats


def verdicts(cfg, stats):
    need_n = cfg.get("edge_min_signals", 8)
    need_avg = cfg.get("edge_min_avg_pct", 0.5)
    prob_n = cfg.get("edge_probation_min_signals", 3)
    v = {}
    for n, s in stats.items():
        if s["n"] < need_n or s["coins"] < max(3, need_n // 2):
            # PROBATION: early evidence is positive (or, for established regular coins, there is no evidence
            # against yet) → trade at half size until there are enough signals to decide
            if n.startswith("maj_") and (s["n"] < prob_n or (s["avg"] or 0) > 0):
                v[n] = ("probation", f"regular coins, half size while evidence builds ({s['n']} signals"
                                     + (f", {s['avg']:+.1f}% avg" if s["avg"] is not None else "") + ")")
            elif s["n"] >= prob_n and s["coins"] >= 2 and (s["avg"] or 0) > need_avg and (s["recent"] or 0) >= 0:
                v[n] = ("probation", f"promising, half size: {s['avg']:+.1f}% avg after costs over {s['n']} signals "
                                     f"({s['win']:.0%} wins); full size after {need_n}")
            elif s["avg"] is None or (s["avg"] >= 0 and (s["recent"] or 0) >= 0):
                v[n] = ("probation", f"early days, half size ({s['n']} signals"
                                     + (f", {s['avg']:+.1f}% avg" if s["avg"] is not None else "") + ")")
            else:
                v[n] = ("shadow", f"blocked: loses {s['avg']:+.1f}% on average over {s['n']} signals")
        elif s["avg"] < need_avg:
            v[n] = ("shadow", f"loses money: {s['avg']:+.1f}% average after costs over {s['n']} signals")
        elif s["recent"] is None or s["recent"] <= 0:
            v[n] = ("shadow", f"worked before ({s['avg']:+.1f}%) but not recently ({s['recent']}%)")
        else:
            v[n] = ("trade", f"proven: {s['avg']:+.1f}% average after costs over {s['n']} signals, {s['recent']:+.1f}% recently")
    return v


def run(cfg, ticks, verbose=True, long_ticks=None):
    stats = measure(cfg, ticks)
    v = verdicts(cfg, stats)
    if long_ticks:
        # a good last day is not proof: a strategy that loses over the last 3 days stays in shadow
        need_n = cfg.get("edge_min_signals", 8)
        lst = measure(cfg, long_ticks)
        for n, (verdict, _) in list(v.items()):
            ls = lst.get(n) or {}
            if verdict != "shadow" and ls.get("n", 0) >= need_n and (ls.get("avg") or 0) < 0:
                v[n] = ("shadow", f"good last day, but loses {ls['avg']:+.1f}% over 3 days ({ls['n']} signals, "
                                  f"{ls['win']:.0%} wins)")
    mins = list(ticks)
    rep = {"time": int(time.time()), "hours": round((mins[-1] - mins[0]) / 3600, 1) if mins else 0,
           "stats": stats, "verdict": {n: x[0] for n, x in v.items()}, "why": {n: x[1] for n, x in v.items()}}
    EDGE_PATH.parent.mkdir(exist_ok=True)
    EDGE_PATH.write_text(json.dumps(rep, indent=1))
    if verbose:
        for n in RULES:
            print(f"edge {n:<13} {v[n][0].upper():<9} {v[n][1]}")
    return rep


class EdgeGate:
    """Live side: which rule strategies may trade right now."""

    def __init__(self):
        self.mtime, self.rep = 0, None

    def refresh(self):
        try:
            mt = EDGE_PATH.stat().st_mtime
        except OSError:
            self.rep = None
            return
        if mt != self.mtime:
            self.mtime = mt
            try:
                self.rep = json.loads(EDGE_PATH.read_text())
            except Exception:
                self.rep = None

    def allows(self, cfg, name):
        """True = may trade. Non-rule strategies (hype, predict) have their own gates."""
        if not cfg.get("edge_gate", True) or name not in RULES:
            return True
        self.refresh()
        if not self.rep or time.time() - self.rep["time"] > 3 * 3600:
            return False                     # no fresh evidence = no trading
        return self.rep["verdict"].get(name) in ("trade", "probation")

    def size_mult(self, cfg, name):
        if not cfg.get("edge_gate", True) or name not in RULES:
            return 1.0
        self.refresh()
        rep = self.rep or {}
        if rep.get("verdict", {}).get(name) == "probation":
            return 0.5
        return self.kelly_mult(cfg, name)

    def kelly_mult(self, cfg, name):
        """Quarter-Kelly sizing from the measured edge: strong, steady strategies bet more, thin ones less.
        Kelly fraction of capital = average return / variance of returns; a quarter of it is the usual safe
        share. Compared with the normal risk-based size, kept within 0.5x - 1.5x (caps still apply after)."""
        k = cfg.get("kelly", {})
        s = ((self.rep or {}).get("stats") or {}).get(name) or {}
        if not k.get("enabled", True) or (s.get("n") or 0) < k.get("min_signals", 8) or not s.get("sd") or s.get("avg") is None:
            return 1.0
        avg, sd = s["avg"] / 100, s["sd"] / 100
        if avg <= 0:
            return 0.5
        kelly_pos = k.get("fraction", 0.25) * avg / (sd * sd)              # fraction of equity to put in
        st_cfg = cfg["strategies"].get(name) or {}
        base_pos = min(cfg["risk_per_trade_pct"] / st_cfg.get("stop_loss_pct", 10),
                       min(cfg["max_position_pct"], st_cfg.get("max_position_pct", 100)) / 100)
        return round(max(k.get("min_mult", 0.5), min(k.get("max_mult", 1.5), kelly_pos / base_pos)), 2)

    def why(self, name):
        return (self.rep or {}).get("why", {}).get(name, "no evidence measured yet")
