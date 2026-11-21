"""
Narrative radar — spots hyped themes (a game launch like GTA 6, a holiday, a viral moment) two ways:

  1. EVENTS: your list in events.json. Active from days_before until days_after each date.
  2. LIVE NARRATIVES: the same word suddenly appears in many different coins (new pump.fun launches
     and trending coins), e.g. 12 new coins named "GTA..." within an hour. If those coins are also
     rising on average, the narrative is hot.

Used by memebot.py (bigger smart sizing on narrative coins) and sniper.py (narrative launches).
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # bundled Python does not add the script folder
import json
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from social import COMMON

HERE = Path(__file__).resolve().parent
EVENTS_PATH = HERE / "events.json"
WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{2,15}")
NOISE = COMMON | {"COIN", "TOKEN", "INU", "PUMP", "MOON", "MEME", "CAT", "DOG", "SOLANA", "OFFICIAL", "THE", "CTO",
                  "BABY", "MINI", "SUPER", "KING", "LORD", "MR", "MRS", "REAL", "FIRST", "ONLY", "WORLD"}


def words_of(*texts):
    out = set()
    for t in texts:
        t = (t or "").upper()
        out |= {w for w in WORD.findall(t) if w not in NOISE}
        joined = re.sub(r"[^A-Z0-9]", "", t)          # "GTA 6" → "GTA6"
        if 3 <= len(joined) <= 15:
            out.add(joined)
    return out


class NarrativeRadar:
    def __init__(self, log=None, notify=None, min_coins=6, window_min=60, min_avg_h1=20.0):
        self.log, self.notify = log, notify
        self.min_coins, self.window, self.min_avg_h1 = min_coins, window_min * 60, min_avg_h1
        self.seen = defaultdict(dict)                 # WORD → {mint: ts}
        self.perf = defaultdict(dict)                 # WORD → {mint: h1}
        self.alert_path = HERE / "data" / "narrative_alerts.json"
        try:
            self.alerted = json.loads(self.alert_path.read_text())       # survives restarts: no re-alert spam
        except Exception:
            self.alerted = {}
        self.max_alerts_per_hour = 3
        self._events, self._events_mtime = [], 0

    # ---------------------------------------------------------------- events
    def events(self):
        try:
            mt = EVENTS_PATH.stat().st_mtime
            if mt != self._events_mtime:
                self._events = json.loads(EVENTS_PATH.read_text()).get("events", [])
                self._events_mtime = mt
        except Exception:
            pass
        return self._events

    def active_events(self, now=None):
        now = now or time.time()
        out = []
        for e in self.events():
            if not e.get("enabled", True):
                continue
            try:
                d = datetime.strptime(e["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                continue
            if d - e.get("days_before", 3) * 86400 <= now <= d + (e.get("days_after", 1) + 1) * 86400:
                out.append(e)
        return out

    def event_keywords(self, now=None):
        kw = {}
        for e in self.active_events(now):
            for k in e.get("keywords", []):
                kw[k.upper()] = e["name"]
        return kw

    # ---------------------------------------------------------------- live narratives
    def observe(self, mint, symbol, name, h1=None, now=None):
        now = now or time.time()
        for w in words_of(symbol, name):
            self.seen[w][mint] = now
            if h1 is not None:
                self.perf[w][mint] = h1

    def hot_live(self, now=None):
        now = now or time.time()
        out = {}
        for w, coins in list(self.seen.items()):
            for m in [m for m, t in coins.items() if now - t > self.window]:
                coins.pop(m)
                self.perf[w].pop(m, None)
            if not coins:
                self.seen.pop(w, None)
                self.perf.pop(w, None)
                continue
            n = len(coins)
            if n < self.min_coins:
                continue
            # median of clipped moves: brand-new coins report absurd 1h % from a near-zero launch price
            p = sorted(max(-100.0, min(1000.0, v)) for v in self.perf[w].values() if v is not None)
            avg = p[len(p) // 2] if p else None
            if avg is not None and avg < self.min_avg_h1:
                continue                                  # coins sharing a name but falling = not a hot theme
            out[w] = f"{n} coins named '{w}' in the last hour" + (f", median {avg:+.0f}% (1h)" if avg is not None else "")
        # one theme = one set of coins: keep the longest word for each identical/contained coin group
        groups = sorted(out, key=len, reverse=True)
        keep = []
        for w in groups:
            ms = set(self.seen[w])
            if any(ms <= set(self.seen[k]) or (w in k) for k in keep):
                continue
            keep.append(w)
        out = {w: out[w] for w in keep}
        recent_alerts = sum(1 for t in self.alerted.values() if now - t < 3600)
        changed = False
        for w, why in sorted(out.items(), key=lambda kv: -len(self.seen.get(kv[0], {}))):
            if self.alerted.get(w, 0) < now - 6 * 3600:
                self.alerted[w] = now
                changed = True
                if self.log:
                    self.log.warning("hot narrative: %s", why)
                if self.notify and recent_alerts < self.max_alerts_per_hour:
                    self.notify(f"Hot narrative: {w}", why, "fire")
                    recent_alerts += 1
        if changed:
            try:
                self.alerted = {k: v for k, v in self.alerted.items() if now - v < 86400}
                self.alert_path.parent.mkdir(exist_ok=True)
                self.alert_path.write_text(json.dumps(self.alerted))
            except Exception:
                pass
        return out

    def themes(self, symbol, name, now=None):
        """Theme tags for a coin: matching event keywords (prefix match, so GTAPEPE → GTA),
        hot narrative words, and the coin's own words."""
        ws = words_of(symbol, name)
        tags = set(ws)
        for k in self.event_keywords(now):
            if any(w == k or (len(k) >= 3 and w.startswith(k)) for w in ws):
                tags.add(k)
        for w in self.hot_live(now):
            if any(x == w or (len(w) >= 3 and x.startswith(w)) for x in ws):
                tags.add(w)
        return tags

    # ---------------------------------------------------------------- combined
    def match(self, symbol, name, now=None):
        """→ reason string if this coin belongs to an active event or a hot live narrative, else None."""
        ws = words_of(symbol, name)
        now = now or time.time()
        for e in self.active_events(now):
            for k in (x.upper() for x in e.get("keywords", [])):
                if any(w == k or (len(k) >= 3 and w.startswith(k)) for w in ws):
                    d = datetime.strptime(e["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
                    # hype builds BEFORE an event; after it, "sell the news" usually wins
                    return f"event: {e['name']}" if now < d + 86400 else f"after-event: {e['name']}"
        live = self.hot_live(now)
        for w in ws:
            if w in live:
                return f"narrative: {live[w]}"
        return None
