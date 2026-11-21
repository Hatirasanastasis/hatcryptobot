"""Smart money: find wallets that keep buying winners early, then follow them.

LEARN  When a coin the bot watches becomes a big winner (+150% in 6h with real liquidity left), read its first
       20 minutes of trades on-chain and note which wallets bought early. A wallet that did that on 2+ different
       winners is "smart money" (one lucky hit is not).
WATCH  Every minute, look at the newest transactions of each smart wallet and record what they buy.
USE    When 2+ smart wallets bought the same coin in the last 20 minutes, the bot treats it as a signal
       ("smart" strategy); when 1 bought a coin the bot already likes, the buy gets a bigger size.
Wallets from wallets.json (copy-trading list) are followed too.

Runs in a background thread on the bot's RPC (RPC_URL). A free Helius RPC makes it far faster than the public one.
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # bundled Python does not add the script folder

import json
import threading
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
STORE = HERE / "data" / "smart_money.json"
WALLETS = HERE / "wallets.json"
SOL_MINT = "So11111111111111111111111111111111111111112"
SKIP_MINTS = {SOL_MINT, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
WEEK = 7 * 86400


class SmartMoney:
    def __init__(self, cfg, chain, log):
        self.cfg = cfg.get("smart_money", {})
        self.chain, self.log = chain, log
        self.lock = threading.Lock()
        self.queue = {}                      # mint -> (created_s, sym)
        self.wallets = {}                    # addr -> {"wins": {mint: ts}}
        self.done = {}                       # mint -> ts learned
        self.last_sig = {}
        self.recent = defaultdict(dict)      # mint -> {wallet: ts of buy}
        self.status = "starting"
        self.errors = 0
        try:
            d = json.loads(STORE.read_text())
            self.wallets, self.done = d.get("wallets", {}), d.get("done", {})
        except Exception:
            pass

    # ------------------------------------------------------------------ public
    @property
    def on(self):
        return bool(self.cfg.get("enabled", True))

    def start(self):
        if self.on:
            threading.Thread(target=self._loop, daemon=True, name="smart-money").start()

    def queue_learn(self, mint, created_s, sym):
        if mint in self.done or not created_s:
            return
        with self.lock:
            self.queue.setdefault(mint, (created_s, sym))

    def smart_list(self):
        now = time.time()
        rows = []
        for a, w in self.wallets.items():
            n = sum(1 for t in w["wins"].values() if now - t < WEEK)
            if n >= self.cfg.get("min_wins", 2):
                rows.append((n, a))
        rows.sort(reverse=True)
        out = [a for _, a in rows[: self.cfg.get("max_wallets", 40)]]
        try:
            for w in json.loads(WALLETS.read_text()).get("wallets", []):
                a = w.get("address") or ""
                if w.get("enabled", True) and len(a) > 30 and not a.startswith("PASTE") and a not in out:
                    out.append(a)
        except Exception:
            pass
        return out

    def buyers(self, mint, window_s=None):
        window_s = window_s or self.cfg.get("window_min", 20) * 60
        now = time.time()
        return [w for w, t in self.recent.get(mint, {}).items() if now - t <= window_s]

    def hot(self):
        need = self.cfg.get("min_wallets_to_buy", 2)
        out = {}
        for m in list(self.recent):
            b = self.buyers(m)
            if len(b) >= need:
                out[m] = b
        return out

    # ------------------------------------------------------------------ background
    def _rpc(self, method, params):
        time.sleep(self.cfg.get("rpc_pause_s", 0.35))
        try:
            r = self.chain.call(method, params)
            self.errors = 0
            return r
        except Exception as e:
            self.errors += 1
            if self.errors in (5, 50):
                self.log.warning("smart money: RPC keeps failing (%s) — a free Helius RPC URL in Wallet & keys fixes this",
                                 str(e)[:80])
            time.sleep(min(30, 2 * self.errors))
            return None

    def _loop(self):
        while True:
            try:
                self._watch()
                for _ in range(2):            # learn up to 2 winners between watch rounds
                    with self.lock:
                        item = next(iter(self.queue.items()), None)
                        if item:
                            self.queue.pop(item[0])
                    if not item:
                        break
                    self._learn(item[0], *item[1])
                n = len(self.smart_list())
                self.status = (f"following {n} smart wallets, learned from {len(self.done)} winners"
                               if n else f"learning: {len(self.done)} winners studied, no repeat early buyers yet")
            except Exception as e:
                self.status = f"error: {str(e)[:80]}"
                time.sleep(10)
            time.sleep(20)

    def _learn(self, mint, created_s, sym):
        """Find wallets that bought `mint` in its first 20 minutes."""
        self.done[mint] = time.time()
        lo, hi = created_s + 20, created_s + 20 * 60
        before, early, reached = None, [], False
        for _ in range(self.cfg.get("learn_max_pages", 40)):   # up to 40k transactions back (cheap calls)
            p = {"limit": 1000}
            if before:
                p["before"] = before
            sigs = None
            for _try in range(4):             # the public RPC rate-limits: wait and retry instead of giving up
                sigs = self._rpc("getSignaturesForAddress", [mint, p])
                if sigs is not None:
                    break
            if not sigs:
                break
            for s in sigs:
                bt = s.get("blockTime") or 0
                if lo <= bt <= hi and s.get("err") is None:
                    early.append(s["signature"])
            before = sigs[-1]["signature"]
            if (sigs[-1].get("blockTime") or 0) <= lo or len(sigs) < 1000:
                reached = True
                break
        if not reached or not early:
            self._save()
            return
        early = early[::-1]                   # oldest first
        step = max(1, len(early) // 60)
        found = set()
        for sig in early[::step][:60]:
            tx = self._rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            if not tx:
                tx = self._rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
            if not tx:
                continue
            for w, m, dt, dsol in parse_buys(tx):
                if m == mint and dt > 0 and dsol < 0:
                    found.add(w)
        now = time.time()
        for w in found:
            self.wallets.setdefault(w, {"wins": {}})["wins"][mint] = now
        # forget wallets with a single old hit so the file stays small
        self.wallets = {a: w for a, w in self.wallets.items()
                        if len(w["wins"]) >= 2 or max(w["wins"].values()) > now - 2 * WEEK}
        self.done = {m: t for m, t in self.done.items() if now - t < 2 * WEEK}
        repeat = [w for w in found if len(self.wallets[w]["wins"]) >= self.cfg.get("min_wins", 2)]
        self.log.info("smart money: studied winner %s — %d early buyers, %d of them already early on another winner",
                      sym, len(found), len(repeat))
        self._save()

    def _watch(self):
        now = time.time()
        for w in self.smart_list():
            sigs = self._rpc("getSignaturesForAddress", [w, {"limit": 10}])
            if not sigs:
                continue
            last = self.last_sig.get(w)
            self.last_sig[w] = sigs[0]["signature"]
            if last is None:
                continue                      # first look: just remember where we are
            fresh = []
            for s in sigs:
                if s["signature"] == last:
                    break
                if s.get("err") is None and now - (s.get("blockTime") or now) < 1800:
                    fresh.append(s["signature"])
            for sig in fresh[:5]:
                tx = self._rpc("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
                if not tx:
                    continue
                for ww, m, dt, dsol in parse_buys(tx):
                    if ww == w and dt > 0 and dsol < 0 and m not in SKIP_MINTS:
                        self.recent[m][w] = tx.get("blockTime") or now
        for m in [m for m, d in self.recent.items() if all(now - t > 3600 for t in d.values())]:
            self.recent.pop(m)

    def _save(self):
        try:
            STORE.parent.mkdir(exist_ok=True)
            STORE.write_text(json.dumps({"wallets": self.wallets, "done": self.done}))
        except Exception:
            pass


def parse_buys(tx):
    """(fee payer, mint, token change, SOL change incl. wrapped SOL) for the transaction's signer."""
    meta = tx.get("meta") or {}
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
    if not keys:
        return []
    w = keys[0]
    try:
        dsol = (meta["postBalances"][0] - meta["preBalances"][0]) / 1e9
    except (KeyError, IndexError):
        dsol = 0.0

    def bal(lst):
        d = defaultdict(float)
        for b in lst or []:
            if b.get("owner") == w:
                d[b["mint"]] += float(b["uiTokenAmount"].get("uiAmount") or 0)
        return d
    pre, post = bal(meta.get("preTokenBalances")), bal(meta.get("postTokenBalances"))
    dsol += post.pop(SOL_MINT, 0) - pre.pop(SOL_MINT, 0)
    return [(w, m, post.get(m, 0) - pre.get(m, 0), dsol) for m in set(pre) | set(post) if post.get(m, 0) != pre.get(m, 0)]
