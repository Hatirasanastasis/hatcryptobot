#!/usr/bin/env python3
"""
Look-ahead model: predicts, from what a coin is doing *now*, the chance that over the
next 15 minutes it rises +8% before it falls -6% (a "triple-barrier" label, the
standard way quant funds label trades).

Trained hourly on the bot's own recorded market data with a time split (train on the
older 70%, judge on the newest 30% it never saw). It only switches on if it proves
it can pick winners on that unseen data well enough to beat fees.

  python model.py           train + print the report (also run by the bot every hour)
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # bundled Python does not add the script folder
import json
import math
import time
from collections import defaultdict, deque
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
MODEL_PATH = HERE / "data" / "model.json"

HORIZON_MIN = 15
UP, DOWN = 0.08, 0.06
FEES = 0.012                                   # round trip pool fees + slippage
BREAKEVEN_PRECISION = (DOWN + FEES) / (UP + DOWN)   # ~0.51

NAMES = ["m5", "h1", "h6", "log_liq", "log_mcap", "liq_mcap", "log_vol_h1", "accel", "ratio", "log_tx",
         "log_age_h", "r1", "r3", "r10", "vol_trend", "ratio_trend", "from_high30", "up_minutes", "breadth"]


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def vector(f, hist, breadth):
    """f: strategy.features(pair); hist: list of (minute_ts, price, vol_h1, ratio), oldest→newest,
    NOT including the current minute; breadth: share of active coins up over 1h."""
    p = f["price"] or 0

    def ret(n):
        if len(hist) >= n and hist[-n][1]:
            return _clip(p / hist[-n][1] - 1, -0.5, 1.0)
        return 0.0

    vt = _clip(f["vol_h1"] / hist[-5][2] - 1, -1, 3) if len(hist) >= 5 and hist[-5][2] else 0.0
    rt = _clip(f["ratio"] - hist[-3][3], -5, 5) if len(hist) >= 3 else 0.0
    window = [h[1] for h in hist[-30:]] + [p]
    hi30 = max(window) if window else p
    ups = 0
    seq = [h[1] for h in hist[-10:]] + [p]
    for a, b in zip(seq[::-1][1:], seq[::-1][:-1]):
        if b > a:
            ups += 1
        else:
            break
    return [
        _clip(f["m5"] or 0, -50, 100) / 10, _clip(f["h1"] or 0, -90, 500) / 50, _clip(f["h6"] or 0, -95, 1000) / 100,
        math.log10(max(f["liq"], 1)), math.log10(max(f["mcap"], 1)), _clip(f["liq"] / f["mcap"], 0, 2) if f["mcap"] else 0,
        math.log10(max(f["vol_h1"], 1)), _clip(f["accel"], 0, 10), _clip(f["ratio"], 0, 5),
        math.log10(max(f["buys"] + f["sells"], 1)), math.log10(max((f["age_min"] or 60) / 60, 0.1)),
        ret(1) * 10, ret(3) * 10, ret(10) * 5, vt, rt, (p / hi30 - 1) * 10 if hi30 else 0, ups / 5, breadth,
    ]


# ============================================================================ dataset

def build(ticks, cfg=None):
    """ticks: {minute_ts: {mint: snapshot_row}} → X, y, t, ids, outcome%.
    With cfg, only coin-minutes that pass the bot's own filters are used: the model must be judged on
    coins the bot can actually trade (brand-new junk with absurd price jumps made the old score look great)."""
    import strategy as S
    from replay import as_pair
    series = defaultdict(list)
    minutes = list(ticks)
    breadth_at = {}
    for t in minutes:
        snap = ticks[t]
        act = [r for r in snap.values() if ((r.get("vol") or {}).get("h1") or 0) >= 20000 and (r.get("chg") or {}).get("h1") is not None]
        breadth_at[t] = sum(1 for r in act if r["chg"]["h1"] > 0) / len(act) if act else 0.5
        for m, r in snap.items():
            if r.get("kind") in ("scan", "position", None):
                series[m].append((t, r))
    X, y, ts, ids, outc = [], [], [], [], []
    for m, rows in series.items():
        hist = deque(maxlen=31)
        prices = [(t, float(r["price"])) for t, r in rows]
        for i, (t, r) in enumerate(rows):
            f = S.features(as_pair(r), t * 1000)
            if f["price"] <= 0 or f["liq"] < 10000 or (cfg is not None and not S.base_ok(cfg, f)[0]):
                hist.append((t, f["price"], f["vol_h1"], f["ratio"]))
                continue
            # label: first barrier hit within horizon (needs future data inside the horizon)
            label, seen_end, value = None, False, 0.0
            for t2, p2 in prices[i + 1:]:
                if t2 - t > HORIZON_MIN * 60:
                    seen_end = True
                    break
                ch = p2 / f["price"] - 1
                value = ch
                if ch >= UP:
                    label, value = 1, UP
                    break
                if ch <= -DOWN:
                    label, value = 0, max(ch, -1.0)          # gaps count: real fills can be worse than the stop
                    break
            if label is None and seen_end:
                label = 0
            if label is not None and len(hist) >= 3:
                X.append(vector(f, list(hist), breadth_at.get(t, 0.5)))
                y.append(label)
                ts.append(t)
                ids.append(m)
                outc.append(value * 100 - FEES * 100)
            hist.append((t, f["price"], f["vol_h1"], f["ratio"]))
    return np.array(X, dtype=float), np.array(y, dtype=float), np.array(ts), np.array(ids), np.array(outc)


# ============================================================================ model

def fit_logistic(X, y, l2=1.0, iters=600, lr=0.1):
    n, d = X.shape
    w, b = np.zeros(d), 0.0
    pos = y.mean() if len(y) else 0.5
    sw = np.where(y == 1, 0.5 / max(pos, 1e-3), 0.5 / max(1 - pos, 1e-3))   # balance classes
    for _ in range(iters):
        z = X @ w + b
        p = 1 / (1 + np.exp(-np.clip(z, -30, 30)))
        g = (p - y) * sw
        w -= lr * (X.T @ g / n + l2 * w / n)
        b -= lr * g.mean()
    return w, b


def auc(scores, y):
    order = np.argsort(scores)
    ranks = np.empty(len(scores))
    ranks[order] = np.arange(1, len(scores) + 1)
    npos, nneg = y.sum(), len(y) - y.sum()
    if npos == 0 or nneg == 0:
        return 0.5
    return (ranks[y == 1].sum() - npos * (npos + 1) / 2) / (npos * nneg)


def train(ticks, cfg=None, min_rows=1500, min_pos=100, min_hours=3.0, min_coins=6, min_picks=20, verbose=True):
    X, y, ts, ids, oc = build(ticks, cfg)
    report = {"time": int(time.time()), "rows": int(len(y)), "positives": int(y.sum()) if len(y) else 0, "active": False}
    span = (ts.max() - ts.min()) / 3600 if len(ts) else 0
    report["hours"] = round(float(span), 1)
    if span < min_hours:
        report["why"] = f"only {span:.1f} h of data; needs {min_hours} h so one hot hour can't fool it"
        _save(report, verbose)
        return report
    if len(y) < min_rows or y.sum() < min_pos:
        report["why"] = f"not enough data yet ({len(y)} samples, {int(y.sum()) if len(y) else 0} winners; need {min_rows}/{min_pos})"
        _save(report, verbose)
        return report
    order = np.argsort(ts)
    X, y, ts, ids, oc = X[order], y[order], ts[order], ids[order], oc[order]
    cut = int(len(y) * 0.7)
    mu, sd = X[:cut].mean(0), X[:cut].std(0) + 1e-9
    Xs = (X - mu) / sd
    w, b = fit_logistic(Xs[:cut], y[:cut])
    val_s = 1 / (1 + np.exp(-(Xs[cut:] @ w + b)))
    yv = y[cut:]
    idv = ids[cut:]
    tv = ts[cut:]
    ocv = oc[cut:]
    a = auc(val_s, yv)
    base = yv.mean()
    # pick the threshold with the best precision that still has enough signals on unseen data
    best = None
    for q in (0.80, 0.85, 0.90, 0.93, 0.95, 0.97):
        th = float(np.quantile(val_s, q))
        # trade like the bot would: one pick per coin per 15 minutes, judged on money after costs
        picks, last = [], {}
        for k in np.argsort(tv):
            if val_s[k] >= th and tv[k] - last.get(idv[k], -1e18) >= 900:
                last[idv[k]] = tv[k]
                picks.append(k)
        if len(picks) >= min_picks and len(set(idv[picks])) >= min_coins:
            prec = float(yv[picks].mean())
            avg = float(ocv[picks].mean())
            if best is None or avg > best[4]:
                best = (th, prec, len(picks), q, avg)
    report.update({"auc": round(float(a), 3), "base_rate": round(float(base), 3), "names": NAMES,
                   "w": w.tolist(), "b": float(b), "mu": mu.tolist(), "sd": sd.tolist()})
    if best:
        report.update({"threshold": best[0], "precision": round(best[1], 3), "signals": best[2],
                       "top_pct": round(1 - best[3], 2), "avg_pick_pct": round(best[4], 2)})
        report["gate"] = float(np.quantile(val_s, 0.4))   # bottom 40% = "model says no"
        report["active"] = bool(a >= 0.6 and best[4] >= 1.0 and best[1] >= base * 1.3)
        if not report["active"]:
            report["why"] = (f"no proven edge on tradeable coins: unseen AUC {a:.2f} (need 0.60), its picks averaged "
                             f"{best[4]:+.1f}% after costs (need +1.0%), hit rate {best[1]:.0%} vs {base:.0%} base")
    else:
        report["why"] = f"too few distinct picks on unseen tradeable data (needs {min_picks} on {min_coins}+ coins)"
    # retrain on everything for live use, same normalisation
    if report["active"]:
        mu, sd = X.mean(0), X.std(0) + 1e-9
        w, b = fit_logistic((X - mu) / sd, y)
        report.update({"w": w.tolist(), "b": float(b), "mu": mu.tolist(), "sd": sd.tolist()})
    _save(report, verbose)
    return report


def _save(report, verbose):
    MODEL_PATH.parent.mkdir(exist_ok=True)
    MODEL_PATH.write_text(json.dumps(report))
    if verbose:
        if report.get("auc") is not None:
            print(f"model: {report['rows']} samples, {report['positives']} winners, unseen AUC {report['auc']}, "
                  f"base hit rate {report['base_rate']:.0%}, top picks {report.get('precision', 0):.0%} "
                  + (f", picks avg {report['avg_pick_pct']:+.1f}% after costs" if report.get('avg_pick_pct') is not None else "")
                  + f" → {'ACTIVE' if report['active'] else 'inactive'}" + (f" ({report['why']})" if report.get('why') else ""))
        else:
            print("model: " + report.get("why", ""))


class Predictor:
    """Live scoring; reloads data/model.json when it changes."""

    def __init__(self):
        self.mtime, self.m = 0, None

    def refresh(self):
        try:
            mt = MODEL_PATH.stat().st_mtime
        except OSError:
            return
        if mt != self.mtime:
            self.mtime = mt
            try:
                self.m = json.loads(MODEL_PATH.read_text())
            except Exception:
                self.m = None

    @property
    def active(self):
        return bool(self.m and self.m.get("active") and self.m.get("w"))

    def score(self, vec):
        m = self.m
        z = sum((x - mu) / sd * w for x, mu, sd, w in zip(vec, m["mu"], m["sd"], m["w"])) + m["b"]
        return 1 / (1 + math.exp(-max(-30, min(30, z))))


if __name__ == "__main__":
    from replay import load, load_cfg
    ticks, _ = load(2)
    train(ticks, load_cfg())
