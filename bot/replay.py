#!/usr/bin/env python3
"""
Replay the v3 strategies on market data the bot recorded (data/snapshots-*.jsonl),
using the exact same entry/exit/sizing/regime code as the live bot (strategy.py).

  python replay.py                 test current config.json, list every trade
  python replay.py --optimize      random-search settings; tune on first 70%, verify on last 30%
  python replay.py --days 2        only use the last 2 days

Limits: snapshots are ~1/minute (the live bot checks exits every 5 s), safety checks
(RugCheck/on-chain) aren't replayed, fills cost 0.3% + impact estimated from liquidity.
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # bundled Python does not add the script folder
import argparse
import copy
import json
import random
import statistics
import time
from collections import defaultdict
from pathlib import Path

import strategy as S

HERE = Path(__file__).resolve().parent


def load_cfg():
    p = HERE / "config.json"
    return S.deep_merge(S.DEFAULTS, json.loads(p.read_text()) if p.exists() else {})


def load(days):
    cutoff = time.time() - days * 86400 if days else 0
    ticks, sol = defaultdict(dict), {}
    for f in sorted((HERE / "data").glob("snapshots-*.jsonl")):
        for line in f.open():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r["ts"] < cutoff or not r.get("price"):
                continue
            t = r["ts"] // 60 * 60
            if r.get("kind") == "sol":
                sol[t] = float(r["price"])
            else:
                ticks[t][r["mint"]] = r
    return dict(sorted(ticks.items())), sol


def as_pair(r):
    return {"baseToken": {"address": r["mint"], "symbol": r["symbol"]}, "quoteToken": {"address": r["quote"]},
            "priceUsd": r["price"], "liquidity": {"usd": r["liq"]}, "marketCap": r["mcap"], "fdv": r["mcap"],
            "volume": r["vol"], "priceChange": r["chg"], "txns": r["txns"], "pairCreatedAt": r["created"]}


def cost(usd, liq):
    return 0.003 + min(usd / max(liq or 1, 1) * 2, 0.5)


def simulate(cfg, ticks, sol_px, start_sol=None, verbose=False):
    start_sol = start_sol or cfg["paper_start_sol"]
    # work in USD; convert SOL-denominated settings at the median SOL price of the data
    k = statistics.median(sol_px.values()) if sol_px else 150.0
    c = copy.deepcopy(cfg)
    for key in ("sol_reserve", "min_position_sol", "max_position_sol", "max_daily_loss_sol"):
        c[key] = cfg[key] * k
    start = start_sol * k
    cash, pos, results, watch, cooldown, bench = start, {}, [], {}, {}, {}
    peak_eq, day, day_start, halted, pause, streak = start, None, start, None, 0, 0
    last = {}

    def equity():
        return cash + sum(p["qty"] * float(last[m]["price"]) for m, p in pos.items())

    for ts, snap in ticks.items():
        last.update(snap)
        d = time.strftime("%Y%m%d", time.gmtime(ts))
        if d != day:
            day, day_start = d, equity()
            if halted and halted != d:
                halted = None

        for m in list(pos):
            p, r = pos[m], last[m]
            px = float(r["price"])
            val = p["qty"] * px
            p["peak"] = max(p["peak"], val)
            s = c["strategies"][p["strategy"]]
            frac, why = S.exit_step(c, s, p, val, r.get("liq"), ts)
            if not frac and ts - r["ts"] > 1800:
                frac, why = 1.0, "data ended"
            if not frac:
                continue
            q = p["qty"] * frac
            got = q * px * (1 - cost(q * px, r.get("liq")))
            cash += got
            p["realized"] += got
            if frac < 0.999:
                p["took_profit"] = True
                p["qty"] -= q
                p["entry"] *= (1 - frac)
                p["peak"] *= (1 - frac)
            else:
                ret = (p["realized"] / p["cost"] - 1) * 100
                results.append({"strategy": p["strategy"], "ret_pct": ret, "pnl": p["realized"] - p["cost"],
                                "symbol": r["symbol"]})
                streak = streak + 1 if ret < 0 else 0
                if streak >= c["max_consecutive_losses"]:
                    pause, streak = ts + c["loss_streak_pause_minutes"] * 60, 0
                st = S.strategy_stats(c, results, p["strategy"])
                if S.benched(c, st):
                    bench[p["strategy"]] = ts + c["adapt_bench_hours"] * 3600
                pos.pop(m)
                cooldown[m] = ts + c["rebuy_cooldown_hours"] * 3600
            if verbose:
                print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(ts))} SELL {r['symbol']:<12} [{p['strategy']}] {why}")

        eq = equity()
        peak_eq = max(peak_eq, eq)
        if eq <= peak_eq * (1 - c["max_drawdown_pct"] / 100):
            break
        if eq - day_start <= -c["max_daily_loss_sol"]:
            halted = d
        if halted or ts < pause:
            continue

        pairs = {m: as_pair(r) for m, r in snap.items() if r.get("kind") != "position"}
        sp = {"priceChange": {"h1": None}}
        rg = S.regime(c, pairs, sp)
        allowed = {n for n in c["strategies"] if bench.get(n, 0) <= ts}
        if rg["risk_off"]:
            allowed.discard("breakout")
        for m, pair in pairs.items():
            if m in pos or cooldown.get(m, 0) > ts:
                continue
            action, name, why, w = S.entry_step(c, pair, watch.get(m), ts, allowed)
            if action in ("NONE", "DROP"):
                watch.pop(m, None)
                continue
            if action == "WATCH":
                watch[m] = w
                continue
            if action != "BUY" or len(pos) >= c["max_positions"]:
                continue
            watch.pop(m, None)
            mult = S.size_mult(c, S.strategy_stats(c, results, name)) * (c["risk_off_size_mult"] if rg["risk_off"] else 1)
            spend = S.position_size(c, c["strategies"][name], equity(), cash, mult)
            if spend < c["min_position_sol"]:
                continue
            liq = (pair.get("liquidity") or {}).get("usd")
            px = float(pair["priceUsd"])
            qty = spend * (1 - cost(spend, liq)) / px
            cash -= spend
            pos[m] = {"qty": qty, "entry": spend, "cost": spend, "realized": 0.0, "peak": spend, "opened": ts,
                      "took_profit": False, "entry_liq": liq, "strategy": name}
            if verbose:
                print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(ts))} BUY  {pair['baseToken']['symbol']:<12} "
                      f"[{name}] ${spend:.2f}")

    final = equity()
    by = defaultdict(list)
    for r in results:
        by[r["strategy"]].append(r["ret_pct"])
    return {"ret": (final / start - 1) * 100, "final_sol": final / k, "trades": len(results),
            "wins": sum(1 for r in results if r["ret_pct"] > 0), "open": len(pos), "by": dict(by)}


SPACE = {
    "risk_per_trade_pct": [1.0, 1.5, 2.0, 3.0],
    "max_positions": [3, 5, 8],
    "base.min_liquidity_usd": [20000, 25000, 40000],
}
for strat in ("momentum", "dip", "breakout"):
    SPACE.update({
        f"strategies.{strat}.enabled": [True, True, False],
        f"strategies.{strat}.stop_loss_pct": [8, 10, 12, 15, 20],
        f"strategies.{strat}.take_profit_pct": [12, 20, 25, 40],
        f"strategies.{strat}.trailing_stop_pct": [6, 8, 10, 12, 15],
        f"strategies.{strat}.breakeven_trigger_pct": [8, 10, 15, 1000],
        f"strategies.{strat}.stale_minutes": [20, 30, 60, 90],
        f"strategies.{strat}.min_buy_ratio": [1.0, 1.2, 1.3, 1.5, 2.0],
    })
SPACE["strategies.breakout.volume_accel"] = [1.5, 2.0, 2.5, 3.5]
SPACE["strategies.dip.m5_max"] = [-2, -3, -5]
SPACE["strategies.momentum.h1_max"] = [40, 60, 100]


def summary(r):
    parts = [f"{r['ret']:+6.1f}%  {r['trades']:>3} trades  {r['wins']} wins"]
    for k, v in sorted(r["by"].items()):
        parts.append(f"{k} {len(v)}x avg {sum(v) / len(v):+.1f}%")
    return " | ".join(parts)


TUNABLE_PREFIXES = ("strategies.", "max_positions", "risk_per_trade_pct", "base.min_liquidity_usd")


def autotune(samples=150, days=1.0, min_hours=3.0, min_trades=10):
    """Search settings on recorded data; write data/tuned.json only if a candidate beats the
    current config on the unseen last 30% AND is profitable there AND traded enough.
    Never touches safety checks, loss limits or caps."""
    cfg = load_cfg()
    ticks, sol = load(days)
    try:
        import model
        model.train(ticks, cfg)
    except Exception as e:
        print(f"model training failed: {e}")
    try:
        import edge
        edge.run(cfg, ticks)
    except Exception as e:
        print(f"edge check failed: {e}")
    keys = list(ticks)
    hours = (keys[-1] - keys[0]) / 3600 if keys else 0
    out = HERE / "data" / "tuned.json"
    if hours < min_hours:
        print(f"autotune: only {hours:.1f} h of data, need {min_hours}")
        return
    cut = keys[int(len(keys) * 0.7)]
    train = {k: v for k, v in ticks.items() if k < cut}
    test = {k: v for k, v in ticks.items() if k >= cut}
    cur_tr, cur_te = simulate(cfg, train, sol), simulate(cfg, test, sol)
    random.seed(int(time.time()))
    best = None
    for _ in range(samples):
        choice = {k: random.choice(v) for k, v in SPACE.items() if k.startswith(TUNABLE_PREFIXES)}
        c = copy.deepcopy(cfg)
        for k, v in choice.items():
            S.set_path(c, k, v)
        r = simulate(c, train, sol)
        if r["trades"] >= max(4, min_trades * 0.7) and (best is None or r["ret"] > best[0]):
            best = (r["ret"], choice, r)
    if not best:
        print("autotune: no candidate traded enough")
        return
    c = copy.deepcopy(cfg)
    for k, v in best[1].items():
        S.set_path(c, k, v)
    te = simulate(c, test, sol)
    verdict = (te["ret"] > cur_te["ret"] and te["ret"] > 0 and best[2]["trades"] + te["trades"] >= min_trades)
    report = {"time": int(time.time()), "hours": round(hours, 1), "adopt": verdict, "settings": best[1],
              "train": {"ret": best[0], "trades": best[2]["trades"]}, "unseen": {"ret": te["ret"], "trades": te["trades"]},
              "current": {"train": cur_tr["ret"], "unseen": cur_te["ret"], "trades": cur_tr["trades"] + cur_te["trades"]}}
    out.write_text(json.dumps(report, indent=1))
    print(f"autotune: candidate train {best[0]:+.1f}% / unseen {te['ret']:+.1f}% vs current unseen {cur_te['ret']:+.1f}%"
          f" → {'ADOPT' if verdict else 'keep current'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--autotune", action="store_true", help="used by the bot every hour")
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--days", type=float, default=0)
    ap.add_argument("--samples", type=int, default=300)
    a = ap.parse_args()
    if a.autotune:
        autotune(samples=a.samples if a.samples != 300 else 150, days=a.days or 1.0)
        return
    cfg = load_cfg()
    ticks, sol = load(a.days)
    if len(ticks) < 60:
        raise SystemExit(f"only {len(ticks)} minutes recorded — let the bot run longer (a day or more is best)")
    keys = list(ticks)
    print(f"data: {(keys[-1] - keys[0]) / 3600:.1f} h, {len(keys)} minutes, "
          f"{len({m for s in ticks.values() for m in s})} coins\n")

    if not a.optimize:
        r = simulate(cfg, ticks, sol, verbose=True)
        print("\ncurrent config: " + summary(r) + f", {r['open']} still open")
        return

    cut = keys[int(len(keys) * 0.7)]
    train = {k: v for k, v in ticks.items() if k < cut}
    test = {k: v for k, v in ticks.items() if k >= cut}
    base_tr, base_te = simulate(cfg, train, sol), simulate(cfg, test, sol)
    print("current   train " + summary(base_tr) + "\n          unseen " + summary(base_te) + "\n")
    random.seed(7)
    found = []
    for i in range(a.samples):
        c = copy.deepcopy(cfg)
        choice = {k: random.choice(v) for k, v in SPACE.items()}
        for k, v in choice.items():
            S.set_path(c, k, v)
        r = simulate(c, train, sol)
        if r["trades"] >= 8:
            found.append((r["ret"], choice, r))
        print(f"  {i + 1}/{a.samples}", end="\r")
    found.sort(key=lambda x: -x[0])
    print("\nbest 5 on training data, then checked on the unseen last 30%:")
    for ret, choice, r in found[:5]:
        c = copy.deepcopy(cfg)
        for k, v in choice.items():
            S.set_path(c, k, v)
        t = simulate(c, test, sol)
        print(f"\n  train  {summary(r)}\n  unseen {summary(t)}")
        print("  " + json.dumps(choice))
    print("\nOnly adopt settings that also win on the unseen part, and beat the current config there.")


if __name__ == "__main__":
    main()
