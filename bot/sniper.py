#!/usr/bin/env python3
"""
Lunyx launch sniper — sees every new pump.fun coin the second it is created and buys
only the launches that have a reason to explode.

Feed: PumpPortal websocket (wss://pumpportal.fun/api/data): new coins, their trades, migrations.
Trades: PumpPortal local-transaction API (you sign with your own key; nothing leaves your machine
except the signed transaction).

Two ways a launch gets sniped:
  1. NARRATIVE  — first second. The coin's name/ticker matches what a VIP (Elon, Trump, ...) just
                  posted, a ticker that's buzzing on Reddit/X/news, or a word on your own watch list.
  2. CROWD      — first ~20 seconds. Real buyers pile in fast: many different wallets buying,
                  serious SOL flowing in, the creator hasn't sold, no single wallet hogging it.

Every snipe is tiny (default 0.02 SOL), there's a cap on open snipes and snipes per hour,
a daily loss limit, and fast exits (most launches die within minutes).

  python sniper.py            paper mode: live feed, fake SOL
  python sniper.py --live     real snipes (config "sniper.mode": "live" + live_budget)
  python sniper.py --status   results so far
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # bundled Python does not add the script folder
import argparse
import asyncio
import base64
import json
import logging
import os
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

import requests

import memebot as M
import narrative as NARR
import social as SOCIAL

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "sniper_state.json"
LOG_PATH = HERE / "sniper.log"
WS_URL = "wss://pumpportal.fun/api/data"
TRADE_URL = "https://pumpportal.fun/api/trade-local"   # FILL: check fees/params at pumpportal.fun/trading-api before going live
SUPPLY = 1_000_000_000

log = logging.getLogger("sniper")

DEFAULTS = {
    "mode": "paper",
    "paper_start_sol": 0.5,
    "live_budget_sol": 0.0,
    "snipe_size_sol": 0.02,
    "max_open": 3,
    "max_per_hour": 10,
    "max_daily_loss_sol": 0.1,
    "priority_fee_sol": 0.0005,
    "slippage_pct": 15,                  # first-second fills move fast
    "paper_cost_pct": 3.0,               # pump.fun 1% + PumpPortal 0.5% + slippage, each way (paper estimate)

    "narrative": True,
    "watch_words": [],                   # your own words to snipe on, e.g. ["TRUMP", "ELON", "DOGE"]
    "narrative_window_min": 30,
    "crowd": True,
    "crowd_window_sec": 20,
    "crowd_min_buyers": 15,
    "crowd_min_net_sol": 3.0,
    "crowd_max_top_buyer_pct": 25,
    "narrative_crowd_min_buyers": 8,     # coins in a hot narrative need fewer early buyers
    "narrative_crowd_min_net_sol": 1.5,
    "max_dev_buy_pct": 8.0,              # creator bought more than this at launch → skip (likely dump)
    "skip_if_creator_launched_today": 3,

    "take_profit_pct": 50, "take_profit_sell_pct": 50,
    "trail_activate_pct": 30, "trailing_stop_pct": 25,
    "stop_loss_pct": 30,
    "max_hold_sec": 300, "stale_min_gain_pct": 20,
    "exit_if_dev_sells_pct": 50,
}


def load_cfg():
    c = M.load_config()
    s = {**DEFAULTS, **(c.get("sniper") or {})}
    return c, s


class Sniper:
    def __init__(self, cfg, s, live_flag=False):
        self.cfg, self.s = cfg, s
        M.load_env()
        if s["mode"] == "live" and not live_flag:
            sys.exit('sniper mode is "live" — add --live to confirm real trading')
        if s["mode"] == "live" and not s["live_budget_sol"]:
            sys.exit('live sniping needs "sniper": {"live_budget_sol": ...} in config.json')
        self.kp = M.load_keypair() if s["mode"] == "live" else None
        self.owner = str(self.kp.pubkey()) if self.kp else None
        self.chain = M.Solana(os.getenv("RPC_URL") or cfg["rpc_url"])
        self.social = SOCIAL.SocialRadar(cfg, log, lambda t, m, tag="": M.notify(cfg, t, m, tag))
        self.narr = NARR.NarrativeRadar(log, lambda t, m, tag="": M.notify(cfg, t, m, tag))
        self.st = self.load()
        self.watch = {}                  # mint → crowd stats during the first seconds
        self.creators = defaultdict(deque)
        self.subs_wanted, self.subs_have = set(), set()

    # ------------------------------------------------------------------ state
    def load(self):
        st = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
        st.setdefault("sol", self.s["paper_start_sol"])
        for k, v in (("pos", {}), ("results", []), ("snipes", []), ("blacklist", [])):
            st.setdefault(k, v)
        st.setdefault("day", None)
        st.setdefault("day_pnl", 0.0)
        return st

    def save(self):
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.st, indent=1))
        tmp.replace(STATE_PATH)

    # ------------------------------------------------------------------ narrative words
    def hot_words(self, now):
        words = {w.upper() for w in self.s["watch_words"]}
        win = self.s["narrative_window_min"] * 60
        with self.social.lock:
            posts = [p for p in self.social.vip_posts if now - p[0] <= win]
            terms = list(self.social.mentions)
        for _, who, text, _ in posts:
            if who in self.social.cfg["vip_names"]:
                for w in SOCIAL.WORD.findall(text.upper()):
                    if w not in SOCIAL.COMMON and len(w) >= 3:
                        words.add(w)
                words |= {t.upper() for t in SOCIAL.CASHTAG.findall(text)}
        words |= {t for t in terms if self.social.buzzing(t, now)}
        words |= set(self.narr.event_keywords(now))
        return words

    # ------------------------------------------------------------------ events
    def on_event(self, e, now=None):
        now = now or time.time()
        tx = e.get("txType")
        if tx == "create":
            self.on_create(e, now)
        elif tx in ("buy", "sell"):
            self.on_trade(e, now)
        elif tx == "migrate" or e.get("pool") in ("pump-amm", "raydium") and "mint" in e and tx is None:
            if e["mint"] in self.st["pos"]:
                log.info("%s graduated from pump.fun to an AMM pool", self.st["pos"][e["mint"]]["sym"])

    def can_snipe(self, now):
        day = time.strftime("%Y%m%d", time.gmtime(now))
        if self.st["day"] != day:
            self.st["day"], self.st["day_pnl"] = day, 0.0
        if self.st["day_pnl"] <= -self.s["max_daily_loss_sol"]:
            return False, "daily loss limit"
        if len(self.st["pos"]) >= self.s["max_open"]:
            return False, "max open snipes"
        recent = [t for t in self.st["snipes"] if now - t < 3600]
        self.st["snipes"] = recent
        if len(recent) >= self.s["max_per_hour"]:
            return False, "max snipes this hour"
        if self.cash() < self.s["snipe_size_sol"] + 0.01:
            return False, "no SOL left"
        return True, ""

    def on_create(self, e, now):
        mint, sym, name = e.get("mint"), (e.get("symbol") or "").strip(), (e.get("name") or "").strip()
        creator = e.get("traderPublicKey")
        dev_pct = (e.get("initialBuy") or 0) / SUPPLY * 100
        cq = self.creators[creator]
        cq.append(now)
        while cq and now - cq[0] > 86400:
            cq.popleft()
        if creator in self.st["blacklist"]:
            return
        if len(cq) > self.s["skip_if_creator_launched_today"]:
            return
        if dev_pct > self.s["max_dev_buy_pct"]:
            log.debug("skip %s: dev bought %.1f%%", sym, dev_pct)
            return
        price = self.price(e)
        self.narr.observe(mint, sym, name, None, now)
        # 1) narrative: first second
        if self.s["narrative"]:
            words = self.hot_words(now)
            up_sym, up_name = sym.upper(), name.upper()
            cw = NARR.words_of(sym, name)
            hit = next((w for w in words if w in cw or (len(w) >= 3 and any(c.startswith(w) for c in cw))), None)
            if hit:
                ok, why = self.can_snipe(now)
                if ok:
                    self.buy(mint, sym, name, creator, price, f"narrative: name matches hot word '{hit}' (dev {dev_pct:.1f}%)", now)
                else:
                    log.info("narrative match %s ('%s') but %s", sym, hit, why)
                return
        # 2) crowd: watch the first seconds
        if self.s["crowd"]:
            self.watch[mint] = {"sym": sym, "name": name, "creator": creator, "t0": now, "buyers": defaultdict(float),
                                "net": 0.0, "dev_sold": False, "price": price, "dev_tokens": e.get("initialBuy") or 0,
                                "narr": self.narr.match(sym, name, now)}
            self.subs_wanted.add(mint)

    def on_trade(self, e, now):
        mint = e.get("mint")
        price = self.price(e)
        w = self.watch.get(mint)
        if w:
            sol = float(e.get("solAmount") or 0)
            if e["txType"] == "buy":
                w["buyers"][e.get("traderPublicKey")] += sol
                w["net"] += sol
            else:
                w["net"] -= sol
                if e.get("traderPublicKey") == w["creator"]:
                    w["dev_sold"] = True
            w["price"] = price or w["price"]
            age = now - w["t0"]
            if age >= 3:
                buyers = len(w["buyers"])
                top = max(w["buyers"].values()) if w["buyers"] else 0
                top_pct = top / max(sum(w["buyers"].values()), 1e-9) * 100
                need_b = self.s["narrative_crowd_min_buyers"] if w.get("narr") else self.s["crowd_min_buyers"]
                need_s = self.s["narrative_crowd_min_net_sol"] if w.get("narr") else self.s["crowd_min_net_sol"]
                if (buyers >= need_b and w["net"] >= need_s and not w["dev_sold"]
                        and top_pct <= self.s["crowd_max_top_buyer_pct"]):
                    ok, why = self.can_snipe(now)
                    self.watch.pop(mint)
                    if ok:
                        self.buy(mint, w["sym"], w["name"], w["creator"], w["price"],
                                 f"crowd: {buyers} buyers, +{w['net']:.1f} SOL in {age:.0f}s, top buyer {top_pct:.0f}%, dev holding"
                                 + (f" | {w['narr']}" if w.get("narr") else ""), now)
                    else:
                        log.info("crowd signal on %s but %s", w["sym"], why)
                    return
            if age > self.s["crowd_window_sec"] or w["dev_sold"]:
                self.watch.pop(mint, None)
                if mint not in self.st["pos"]:
                    self.subs_wanted.discard(mint)
        p = self.st["pos"].get(mint)
        if p:
            if e["txType"] == "sell" and e.get("traderPublicKey") == p["creator"]:
                p["dev_sold"] = p.get("dev_sold", 0) + float(e.get("tokenAmount") or 0)
            if price:
                self.manage(mint, price, now)

    # ------------------------------------------------------------------ positions
    @staticmethod
    def price(e):
        vs, vt = e.get("vSolInBondingCurve"), e.get("vTokensInBondingCurve")
        if vs and vt:
            return float(vs) / float(vt)
        mc = e.get("marketCapSol")
        return float(mc) / SUPPLY if mc else None

    def cash(self):
        if self.s["mode"] == "paper":
            return self.st["sol"]
        bal = self.chain.sol_balance(self.owner)
        realized = sum(r["pnl"] for r in self.st["results"])
        in_pos = sum(p["cost"] for p in self.st["pos"].values())
        return max(0.0, min(bal, self.s["live_budget_sol"] + realized - in_pos))

    def buy(self, mint, sym, name, creator, price, reason, now):
        size = self.s["snipe_size_sol"]
        if self.s["mode"] == "live":
            ok, ref = self.trade("buy", mint, size, denominated_sol=True)
            if not ok:
                log.warning("SNIPE %s failed: %s", sym, ref)
                return
        else:
            ref = "paper"
            self.st["sol"] -= size + self.s["priority_fee_sol"]
        eff = price * (1 + self.s["paper_cost_pct"] / 100) if price else None
        tokens = size / eff if eff else 0
        self.st["pos"][mint] = {"sym": sym, "name": name, "creator": creator, "cost": size, "cost0": size, "tokens": tokens,
                                "entry": eff, "peak": eff, "t0": now, "took": False, "realized": 0.0}
        self.st["snipes"].append(now)
        self.subs_wanted.add(mint)
        log.info("SNIPE BUY %s (%s) %.3f SOL @ %.10f — %s [%s]", sym, name[:24], size, eff or 0, reason, ref)
        M.notify(self.cfg, f"SNIPE {sym} ({self.s['mode']})", f"{size} SOL\n{reason}\nhttps://pump.fun/coin/{mint}", "dart")
        self.save()

    def manage(self, mint, price, now):
        s, p = self.s, self.st["pos"][mint]
        eff = price * (1 - s["paper_cost_pct"] / 100)
        p["peak"] = max(p["peak"], price)
        chg = (eff / p["entry"] - 1) * 100
        peak_gain = (p["peak"] / p["entry"] - 1) * 100
        from_peak = (price / p["peak"] - 1) * 100
        held = now - p["t0"]
        frac, why = 0, None
        if p.get("dev_sold", 0) and p["dev_sold"] >= 1 and s["exit_if_dev_sells_pct"]:
            frac, why = 1, "creator is selling"
        elif chg <= -s["stop_loss_pct"]:
            frac, why = 1, f"stop-loss {chg:.0f}%"
        elif not p["took"] and chg >= s["take_profit_pct"]:
            frac, why = s["take_profit_sell_pct"] / 100, f"take-profit +{chg:.0f}%"
        elif peak_gain >= s["trail_activate_pct"] and from_peak <= -s["trailing_stop_pct"]:
            frac, why = 1, f"trailing stop ({from_peak:.0f}% from peak, {chg:+.0f}%)"
        elif held >= s["max_hold_sec"] and chg < s["stale_min_gain_pct"] and not p["took"]:
            frac, why = 1, f"no lift-off after {held / 60:.0f} min ({chg:+.0f}%)"
        if frac:
            self.sell(mint, frac, eff, why, now)

    def sell(self, mint, frac, eff, why, now):
        p = self.st["pos"][mint]
        tokens = p["tokens"] * frac
        if self.s["mode"] == "live":
            ok, ref = self.trade("sell", mint, "100%" if frac >= 0.999 else f"{int(frac * 100)}%", denominated_sol=False)
            if not ok:
                log.warning("SNIPE SELL %s failed: %s — retrying on next trade", p["sym"], ref)
                return
        got = tokens * eff
        if self.s["mode"] == "paper":
            self.st["sol"] += got - self.s["priority_fee_sol"]
        p["realized"] += got
        cost_part = p["cost"] * frac
        log.info("SNIPE SELL %s %d%% → %.4f SOL (P&L %+.4f, %+.0f%%) — %s", p["sym"], round(frac * 100), got,
                 got - cost_part, (got / cost_part - 1) * 100, why)
        M.notify(self.cfg, f"SNIPE SELL {p['sym']} {got - cost_part:+.4f} SOL", why, "moneybag" if got > cost_part else "red_circle")
        if frac >= 0.999:
            c0 = p.get("cost0", p["cost"])
            pnl = p["realized"] - c0
            self.st["results"].append({"sym": p["sym"], "pnl": pnl, "ret": pnl / c0 * 100, "held_s": now - p["t0"]})
            self.st["day_pnl"] += pnl
            if pnl < 0 and (why == "creator is selling" or p["realized"] < c0 * 0.4):
                self.st["blacklist"] = (self.st["blacklist"] + [p["creator"]])[-500:]
            del self.st["pos"][mint]
            self.subs_wanted.discard(mint)
        else:
            p["took"] = True
            p["tokens"] -= tokens
            p["cost"] -= cost_part
        self.save()

    def trade(self, action, mint, amount, denominated_sol):
        r = requests.post(TRADE_URL, data={
            "publicKey": self.owner, "action": action, "mint": mint, "amount": amount,
            "denominatedInSol": "true" if denominated_sol else "false", "slippage": self.s["slippage_pct"],
            "priorityFee": self.s["priority_fee_sol"], "pool": "auto"}, timeout=10)
        if r.status_code != 200:
            return False, f"PumpPortal {r.status_code}: {r.text[:150]}"
        from solders.transaction import VersionedTransaction
        tx = VersionedTransaction.from_bytes(r.content)
        signed = VersionedTransaction(tx.message, [self.kp])
        sig = self.chain.send(base64.b64encode(bytes(signed)).decode())
        ok, err = self.chain.confirm(sig, timeout=30)
        return ok, sig if ok else f"{sig}: {err}"

    # ------------------------------------------------------------------ websocket
    async def run(self):
        import websockets
        self.social.start()
        log.info("sniper starting — %s mode, %.3f SOL, %.3f SOL per snipe", self.s["mode"].upper(), self.cash(),
                 self.s["snipe_size_sol"])
        if self.s["mode"] == "live" and sys.stdin.isatty() and not os.getenv("SNIPER_CONFIRMED"):
            if input(f"Snipe with REAL money from {self.owner}, budget {self.s['live_budget_sol']} SOL? Type YES: ").strip() != "YES":
                sys.exit("not confirmed")
        backoff = 2
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=20, max_size=2 ** 22) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    self.subs_have = set()
                    backoff = 2
                    last_save = time.time()
                    while True:
                        await self.sync_subs(ws)
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            msg = None
                        if msg:
                            try:
                                e = json.loads(msg)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(e, dict) and e.get("mint"):
                                self.on_event(e)
                        now = time.time()
                        for mint, p in list(self.st["pos"].items()):      # time exits even with no trades
                            if now - p["t0"] > self.s["max_hold_sec"] * 3:
                                self.sell(mint, 1.0, (p["peak"] or 0) * 0.5, "no trades for a long time", now)
                        if now - last_save > 30:
                            self.save()
                            last_save = now
            except KeyboardInterrupt:
                self.save()
                return
            except Exception as ex:
                log.warning("feed disconnected (%s), reconnecting in %ds", type(ex).__name__, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def sync_subs(self, ws):
        add = list(self.subs_wanted - self.subs_have)
        rem = list(self.subs_have - self.subs_wanted)
        if add:
            await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": add}))
            self.subs_have |= set(add)
        if rem:
            await ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": rem}))
            self.subs_have -= set(rem)

    def status(self):
        res = self.st["results"]
        wins = [r for r in res if r["pnl"] > 0]
        print(f"mode      {self.s['mode']}   cash {self.cash():.4f} SOL")
        print(f"snipes    {len(res)} closed, {len(wins)} winners, total {sum(r['pnl'] for r in res):+.4f} SOL")
        if res:
            best = max(res, key=lambda r: r["ret"])
            print(f"best      {best['sym']} {best['ret']:+.0f}%")
        for m, p in self.st["pos"].items():
            print(f"open      {p['sym']}  {p['cost']:.3f} SOL  https://pump.fun/coin/{m}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH)])
    cfg, s = load_cfg()
    sn = Sniper(cfg, s, a.live)
    if a.status:
        sn.status()
    else:
        asyncio.run(sn.run())


if __name__ == "__main__":
    main()
