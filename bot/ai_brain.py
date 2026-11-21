"""AI brain: Claude reads what the bot sees (coins, VIP posts, hot themes) and rates each coin.

Every few minutes (sooner when a VIP posts) it sends one compact snapshot to Claude and gets back, per coin:
  hype   0-10  how strong the real attention / story behind the coin is right now
  danger 0-10  rug / pump-and-dump / copycat / dead-story risk
  reason       one short line, shown in the log and in the trade note

The bot uses it as one more opinion, never as the only one:
  danger >= ai.veto_danger  -> the coin is not bought (logged as "AI veto")
  hype   >= ai.boost_hype   -> bigger size (+ai.boost conviction, still capped by conviction_max)
Hard rules (stop-loss, budget, rug checks) always stay in charge.

Needs ANTHROPIC_API_KEY in .env (console.anthropic.com). Without a key the bot runs exactly as before.
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # bundled Python does not add the script folder

import json
import os
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "data" / "ai_verdicts.json"

DEFAULT_AI = {
    "enabled": True,
    "model": "claude-opus-5-5",
    "effort": "low",               # one quick read every few minutes; low keeps it fast and cheap
    "interval_s": 600,             # normal rhythm
    "min_gap_s": 90,               # never more often than this, even when VIPs post
    "max_coins": 25,
    "veto_danger": 7,
    "boost_hype": 7,
    "boost": 0.2,
    "ttl_s": 1800,                 # verdicts older than this are ignored
}

SYSTEM = (
    "You are the risk and hype analyst inside an automated Solana meme-coin trading bot that trades a small, "
    "fixed budget. Each request is a snapshot of coins the bot is considering, recent posts from influential "
    "accounts, and trending themes. Rate every coin listed:\n"
    "- hype (0-10): how strong and fresh the real attention or story behind THIS coin is right now. "
    "A real, current catalyst tied to the coin's name scores high; generic or stale buzz scores low.\n"
    "- danger (0-10): risk of rug pull, pump-and-dump, copycat of a bigger token, the story already being over "
    "(sell-the-news), or the move being driven only by a few wallets. Very young coins with thin liquidity "
    "and a vertical chart score high.\n"
    "- reason: one short plain sentence (max 20 words) a non-expert can read.\n"
    "- sell_now: only for coins marked held=true (the bot owns them): true when the story behind the coin is "
    "dying or turning dangerous (the hyping account went quiet or denied it, copycat exposed, buyers gone, "
    "the event already happened). Normal dips are NOT a reason; the bot has its own stop-loss. "
    "Always false for coins that are not held.\n"
    "Judge only from the data given; when the data says little, use middle scores (4-6) instead of guessing. "
    "Return every coin in the list, using its mint exactly as given."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "coins": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "mint": {"type": "string"},
                    "hype": {"type": "integer"},
                    "danger": {"type": "integer"},
                    "reason": {"type": "string"},
                    "sell_now": {"type": "boolean"},
                },
                "required": ["mint", "hype", "danger", "reason", "sell_now"],
                "additionalProperties": False,
            },
        },
        "market_note": {"type": "string"},
    },
    "required": ["coins", "market_note"],
    "additionalProperties": False,
}


class AIBrain:
    def __init__(self, cfg, log):
        self.cfg = {**DEFAULT_AI, **(cfg.get("ai") or {})}
        self.log = log
        self.lock = threading.Lock()
        self.snapshot = None
        self.snap_vip = 0
        self.last_call = 0.0
        self.last_vip_seen = 0
        self.verdicts = {}
        self.note = ""
        self.status = "off"
        self.calls = 0
        self.client = None
        try:
            if OUT.exists():
                d = json.loads(OUT.read_text())
                self.verdicts, self.note = d.get("coins", {}), d.get("note", "")
        except Exception:
            pass
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not self.cfg["enabled"]:
            self.status = "switched off in settings"
        elif not key:
            self.status = "no Anthropic API key (add it in Wallet & keys)"
        else:
            try:
                import anthropic
                self.anthropic = anthropic
                self.client = anthropic.Anthropic(api_key=key, timeout=90.0, max_retries=2)
                self.status = "on"
            except Exception as e:  # SDK missing in an old install
                self.status = f"AI library missing ({e})"

    @property
    def on(self):
        return self.client is not None

    def start(self):
        if self.on:
            threading.Thread(target=self._loop, daemon=True, name="ai-brain").start()
            self.log.info("AI brain on: %s rates coins for hype and danger every %d min",
                          self.cfg["model"], self.cfg["interval_s"] // 60)
        else:
            self.log.info("AI brain off: %s", self.status)

    # ------------------------------------------------------------------ data in
    def observe(self, coins, vip_posts, themes, regime):
        """Called by the bot each scan with what it currently sees. Cheap: just stores the latest snapshot."""
        if not self.on:
            return
        newest_vip = max((p[0] for p in vip_posts), default=0)
        with self.lock:
            held = [c for c in coins if c.get("held")]
            rest = [c for c in coins if not c.get("held")]
            self.snapshot = {"coins": held + rest[: max(0, self.cfg["max_coins"] - len(held))], "vip": vip_posts[-15:], "themes": themes[:12],
                             "regime": regime}
            self.snap_vip = newest_vip

    # ------------------------------------------------------------------ data out
    def get(self, mint):
        v = self.verdicts.get(mint)
        if v and time.time() - v.get("t", 0) <= self.cfg["ttl_s"]:
            return v
        return None

    def veto(self, mint):
        v = self.get(mint)
        return v if v and v["danger"] >= self.cfg["veto_danger"] else None

    def bonus(self, mint):
        v = self.get(mint)
        if v and v["hype"] >= self.cfg["boost_hype"] and v["danger"] < self.cfg["veto_danger"] - 2:
            return self.cfg["boost"], v
        return 0.0, v

    # ------------------------------------------------------------------ loop
    def _loop(self):
        while True:
            time.sleep(5)
            with self.lock:
                snap, vip = self.snapshot, self.snap_vip
            if not snap or not snap["coins"]:
                continue
            since = time.time() - self.last_call
            fresh_vip = vip > self.last_vip_seen
            if since < self.cfg["min_gap_s"] or (since < self.cfg["interval_s"] and not fresh_vip):
                continue
            self.last_call = time.time()
            self.last_vip_seen = vip
            try:
                self._ask(snap)
            except Exception as e:
                self.status = f"error: {str(e)[:120]}"
                self.log.warning("AI brain: %s", self.status)

    def _ask(self, snap):
        A = self.anthropic
        now = time.time()
        lines = ["MARKET: " + json.dumps(snap["regime"]), "", "COINS (mint | data):"]
        for c in snap["coins"]:
            lines.append(json.dumps(c, separators=(",", ":")))
        lines += ["", "RECENT POSTS FROM INFLUENTIAL ACCOUNTS (minutes ago | who | text):"]
        for ts, who, text, _url in snap["vip"]:
            lines.append(f"{(now - ts) / 60:.0f} | {who} | {text[:280]}")
        if not snap["vip"]:
            lines.append("(none in the last hours)")
        lines += ["", "TRENDING THEMES: " + (", ".join(snap["themes"]) or "(none)")]
        try:
            r = self.client.beta.messages.create(
                model=self.cfg["model"],
                max_tokens=8000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=SYSTEM,
                output_config={"effort": self.cfg["effort"], "format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": "\n".join(lines)}],
            )
        except A.AuthenticationError:
            self.status = "Anthropic rejected the API key"
            self.log.warning("AI brain: %s — check it in Wallet & keys", self.status)
            return
        except A.RateLimitError:
            self.status = "rate limited, retrying later"
            return
        except A.APIStatusError as e:
            self.status = f"API error {e.status_code}"
            self.log.warning("AI brain: %s %s", self.status, str(e)[:150])
            return
        except A.APIConnectionError:
            self.status = "no connection to Anthropic"
            return
        if r.stop_reason == "refusal":
            self.status = "declined this snapshot"
            return
        if r.stop_reason == "max_tokens":
            self.status = "answer cut off"
            return
        text = next((b.text for b in r.content if b.type == "text"), "")
        data = json.loads(text)
        valid = {c["mint"] for c in snap["coins"]}
        out = {}
        for c in data.get("coins", []):
            if c["mint"] in valid:
                out[c["mint"]] = {"hype": max(0, min(10, int(c["hype"]))), "danger": max(0, min(10, int(c["danger"]))),
                                  "reason": str(c["reason"])[:160], "t": now,
                                  "sell_now": bool(c.get("sell_now")) and any(x["mint"] == c["mint"] and x.get("held")
                                                                              for x in snap["coins"]),
                                  "sym": next((x.get("sym") for x in snap["coins"] if x["mint"] == c["mint"]), "?")}
        self.verdicts = {**{k: v for k, v in self.verdicts.items() if now - v.get("t", 0) < self.cfg["ttl_s"]}, **out}
        self.note = str(data.get("market_note", ""))[:300]
        self.calls += 1
        self.status = "on"
        try:
            OUT.parent.mkdir(exist_ok=True)
            OUT.write_text(json.dumps({"time": now, "note": self.note, "coins": self.verdicts}, indent=1))
        except Exception:
            pass
        hot = sorted(out.values(), key=lambda v: -v["hype"])[:3]
        bad = [v for v in out.values() if v["danger"] >= self.cfg["veto_danger"]]
        self.log.info("AI brain: rated %d coins — hottest: %s | blocked: %s", len(out),
                      ", ".join(f"{v['sym']} hype {v['hype']}" for v in hot) or "-",
                      ", ".join(v["sym"] for v in bad) or "none")
