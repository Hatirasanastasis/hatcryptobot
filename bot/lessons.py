"""
Lessons from losses.

Every closed trade is stored with what the coin looked like when the bot bought it (1h/5m/6h move,
buyers vs sellers, volume speed-up, pool size, market cap, pool vs market cap, coin age).
After each loss, this compares losers with winners of the same strategy and looks for one clear
difference — e.g. "7 of 9 losers were already up more than 45% in the hour; only 1 of 8 winners was".
If it finds one, it adds that as a new entry rule for that strategy ("skip coins up more than 45% in
the hour"), writes it to lessons.md, and alerts your phone.

Guardrails:
  * needs at least 6 losses and 4 wins for that strategy
  * the rule must exclude ≥ 60% of the losers and ≤ 20% of the winners
  * rules only make the bot stricter, never looser; max 4 per strategy
  * each rule expires after 48 h unless the data confirms it again (markets change)
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # bundled Python does not add the script folder
import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LESSONS_MD = HERE / "lessons.md"

FEATURES = {
    # key: (label, unit, which directions make sense)
    "h1": ("1h move", "%", ("max", "min")),
    "m5": ("5m move", "%", ("max", "min")),
    "h6": ("6h move", "%", ("max", "min")),
    "ratio": ("buyers vs sellers", "×", ("min",)),
    "accel": ("volume speed-up", "×", ("min", "max")),
    "liq": ("pool size", "$", ("min",)),
    "mcap": ("market cap", "$", ("min", "max")),
    "liq_mcap": ("pool vs market cap", "", ("min",)),
    "age_h": ("coin age", "h", ("min",)),
}
MIN_LOSSES, MIN_WINS = 6, 4
MAX_RULES, TTL_H = 4, 48


def entry_snapshot(f):
    """What to remember about a coin at the moment of buying (f = strategy.features(pair))."""
    return {"h1": f.get("h1"), "m5": f.get("m5"), "h6": f.get("h6"), "ratio": round(f.get("ratio") or 0, 2),
            "accel": round(f.get("accel") or 0, 2), "liq": f.get("liq"), "mcap": f.get("mcap"),
            "liq_mcap": round(f["liq"] / f["mcap"], 3) if f.get("mcap") else None,
            "age_h": round(f["age_min"] / 60, 1) if f.get("age_min") is not None else None}


def rule_blocks(rule, f):
    v = f.get(rule["feature"])
    if v is None:
        return False
    return v > rule["value"] if rule["op"] == "max" else v < rule["value"]


def check_learned(s, f, now=None):
    """Called from strategy.signal(): True if a learned rule blocks this entry."""
    now = now or time.time()
    snap = entry_snapshot(f)
    for r in s.get("learned", []):
        if r.get("expires", 0) > now and rule_blocks(r, snap):
            return True
    return False


def learn(cfg, results, strategy, now=None):
    """Look for one clear difference between losers and winners of `strategy`. Returns a new rule or None."""
    now = now or time.time()
    rs = [r for r in results if r.get("strategy") == strategy and r.get("entry")][-60:]
    losers = [r["entry"] for r in rs if r["pnl_sol"] < 0]
    winners = [r["entry"] for r in rs if r["pnl_sol"] > 0]
    if len(losers) < MIN_LOSSES or len(winners) < MIN_WINS:
        return None
    s = cfg["strategies"][strategy]
    rules = [r for r in s.get("learned", []) if r.get("expires", 0) > now]
    best = None
    for feat, (_, _, dirs) in FEATURES.items():
        lv = sorted(x[feat] for x in losers if x.get(feat) is not None)
        wv = [x[feat] for x in winners if x.get(feat) is not None]
        if len(lv) < MIN_LOSSES or len(wv) < MIN_WINS:
            continue
        for op in dirs:
            # candidate thresholds: between loser values
            for q in (0.2, 0.3, 0.4):
                if op == "max":
                    th = lv[int(len(lv) * q)]           # block everything above this
                    lose_cut = sum(1 for v in lv if v > th) / len(lv)
                    win_cut = sum(1 for v in wv if v > th) / len(wv)
                else:
                    th = lv[int(len(lv) * (1 - q)) - 1]  # block everything below this
                    lose_cut = sum(1 for v in lv if v < th) / len(lv)
                    win_cut = sum(1 for v in wv if v < th) / len(wv)
                if lose_cut >= 0.6 and win_cut <= 0.2:
                    score = lose_cut - win_cut
                    if not best or score > best[0]:
                        best = (score, feat, op, th, lose_cut, win_cut)
    if not best:
        return None
    _, feat, op, th, lc, wc = best
    same = next((r for r in rules if r["feature"] == feat and r["op"] == op), None)
    if same:
        tighter = (op == "max" and th < same["value"]) or (op == "min" and th > same["value"])
        same["expires"] = now + TTL_H * 3600          # confirmed again
        if not tighter:
            return None
        same["value"] = th
        rule = same
    else:
        if len(rules) >= MAX_RULES:
            return None
        rule = {"feature": feat, "op": op, "value": th, "expires": now + TTL_H * 3600}
        rules.append(rule)
    s["learned"] = rules
    label, unit, _ = FEATURES[feat]
    rule["why"] = (f"{lc:.0%} of {len(losers)} recent {strategy} losers had {label} "
                   f"{'above' if op == 'max' else 'below'} {fmt(th, unit)}, vs {wc:.0%} of {len(winners)} winners")
    rule["text"] = f"{strategy}: skip coins with {label} {'above' if op == 'max' else 'below'} {fmt(th, unit)}"
    write_md(rule)
    return rule


def fmt(v, unit):
    if unit == "$":
        return f"${v:,.0f}"
    if unit == "%":
        return f"{v:+.0f}%"
    return f"{v:g}{unit}"


def write_md(rule):
    head = "" if LESSONS_MD.exists() else "# Lessons the bot learned from its losses\n\n"
    with open(LESSONS_MD, "a") as f:
        f.write(f"{head}- **{time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC**: {rule['text']}. "
                f"Why: {rule['why']}. Expires in {TTL_H} h unless confirmed again.\n")


def save_rules(config_path, cfg):
    """Persist learned rules into config.json so they survive restarts."""
    try:
        disk = json.loads(config_path.read_text())
    except Exception:
        disk = {}
    st = disk.setdefault("strategies", {})
    for name, s in cfg["strategies"].items():
        if s.get("learned"):
            st.setdefault(name, {})["learned"] = s["learned"]
    config_path.write_text(json.dumps(disk, indent=2))
