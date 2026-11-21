#!/usr/bin/env python3
"""
Lunyx copy-trader — follows the wallets of profitable traders (e.g. Fomo's top traders) on-chain
and copies their buys and sells, with the meme bot's safety checks in front of every copy.

Getting the wallets:
  Fomo shows PnL but hides the trading wallet. Look up a trader's real Solana wallet with a
  wallet-finder (e.g. paste their Fomo profile into Fomo Wallet Finder), or take wallets from
  GMGN / Birdeye / DexScreener "top traders". Put them in wallets.json:
    {"wallets": [{"address": "<solana address>", "name": "fomo: somebody", "enabled": true}]}
  Prefer traders who are profitable over the last 30 days, not just all-time.

What it does (every few seconds per wallet):
  * sees each new swap of a watched wallet straight from the Solana blockchain
  * THEY BUY  → it copies (small fixed size) if: the coin passes the bot's safety checks
               (no mint/freeze authority, RugCheck clean, honeypot/tax check, pool size),
               the price hasn't already run away since their buy, and their buy wasn't dust
  * 2+ WATCHED WALLETS buy the same coin within 30 min → "consensus" copy, bigger size
  * THEY SELL (half or more) → it sells too
  * its own safety exits as well: stop-loss, trailing stop, profit lock, time limit
  * wallets whose copies keep losing get dropped automatically

  python copytrade.py            paper (fake SOL, real wallets and prices)
  python copytrade.py --live     real money (config "copytrade": {"mode": "live", "live_budget_sol": ...})
  python copytrade.py --status
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # bundled Python does not add the script folder
import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import evmcopy as EVM
import memebot as M
import strategy as S
from strategy import SOL_MINT

HERE = Path(__file__).resolve().parent
WALLETS = HERE / "wallets.json"
STATE_PATH = HERE / "copy_state.json"
LOG_PATH = HERE / "copytrade.log"
log = logging.getLogger("copytrade")

DEFAULTS = {
    "mode": "paper",
    "paper_start_sol": 0.5,
    "live_budget_sol": 0.0,
    "poll_seconds": 8,
    "copy_size_sol": 0.05,
    "consensus_size_sol": 0.10,
    "consensus_wallets": 2,
    "consensus_window_min": 30,
    "min_their_buy_sol": 0.2,          # ignore dust buys / tests
    "max_price_run_pct": 25,           # skip if the price already ran this much since their buy
    "max_open": 5,
    "max_daily_loss_sol": 0.15,
    "follow_sells": True,
    "follow_sell_min_pct": 50,         # they sold at least this share of their tokens → we sell all
    "stop_loss_pct": 25, "take_profit_pct": 40, "take_profit_sell_pct": 50,
    "trail_activate_pct": 20, "trailing_stop_pct": 20, "breakeven_trigger_pct": 25, "breakeven_lock_pct": 2,
    "stale_minutes": 240, "stale_min_gain_pct": 5, "profit_lock_keep_pct": 50,
    "drop_wallet_after": 8,            # copies before judging a wallet
    "drop_wallet_expectancy_pct": -5,
}


def load():
    cfg = M.load_config()
    c = {**DEFAULTS, **(cfg.get("copytrade") or {})}
    return cfg, c


class Copier:
    def __init__(self, cfg, c, live_flag=False):
        self.cfg, self.c = cfg, c
        M.load_env()
        if c["mode"] == "live":
            if not live_flag:
                sys.exit('copytrade mode is "live" — add --live to confirm')
            if not c["live_budget_sol"]:
                sys.exit('live copy trading needs "copytrade": {"live_budget_sol": ...}')
        self.chain = M.Solana(os.getenv("RPC_URL") or cfg["rpc_url"])
        self.jup = M.Jupiter(cfg) if (c["mode"] == "live" or os.getenv("JUPITER_API_KEY")) else M.PaperQuotes(self.chain)
        self.kp = M.load_keypair() if c["mode"] == "live" else None
        self.owner = str(self.kp.pubkey()) if self.kp else None
        self.st = self.load_state()
        self.recent_buys = defaultdict(dict)       # mint → {wallet: ts}
        self.evm = EVM.EvmCopy(c, self.st, log, lambda t, msg, tag="": M.notify(self.cfg, t, msg, tag))

    # ------------------------------------------------------------------ state
    def load_state(self):
        st = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
        st.setdefault("sol", self.c["paper_start_sol"])
        for k, v in (("pos", {}), ("results", []), ("last_sig", {}), ("dropped", []), ("day", None), ("day_pnl", 0.0)):
            st.setdefault(k, v)
        return st

    def save(self):
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.st, indent=1))
        tmp.replace(STATE_PATH)

    def wallets(self):
        try:
            ws = json.loads(WALLETS.read_text()).get("wallets", [])
        except Exception:
            return []
        return [w for w in ws if w.get("enabled", True) and w.get("address") and w["address"] not in self.st["dropped"]]

    # ------------------------------------------------------------------ chain reading
    def new_swaps(self, wallet):
        """Yield (signature, mint, token_delta, sol_delta, ts) for the wallet's new swaps, oldest first."""
        sigs = self.chain.call("getSignaturesForAddress", [wallet, {"limit": 15}])
        last = self.st["last_sig"].get(wallet)
        fresh = []
        for s in sigs:
            if s["signature"] == last:
                break
            if s.get("err") is None:
                fresh.append(s)
        if sigs:
            self.st["last_sig"][wallet] = sigs[0]["signature"]
        if last is None:
            return []                                   # first look: just remember where we are
        out = []
        for s in reversed(fresh):
            tx = self.chain.call("getTransaction", [s["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                                                     "commitment": "confirmed"}])
            if not tx:
                continue
            out += self.parse_swap(tx, wallet, s["signature"], s.get("blockTime") or time.time())
        return out

    @staticmethod
    def parse_swap(tx, wallet, sig, ts):
        meta = tx.get("meta") or {}
        keys = [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]
        try:
            i = keys.index(wallet)
            sol_delta = (meta["postBalances"][i] - meta["preBalances"][i]) / M.LAMPORTS
        except (ValueError, KeyError, IndexError):
            sol_delta = 0.0
        def bal(lst):
            d = defaultdict(float)
            for b in lst or []:
                if b.get("owner") == wallet:
                    d[b["mint"]] += float(b["uiTokenAmount"].get("uiAmount") or 0)
            return d
        pre, post = bal(meta.get("preTokenBalances")), bal(meta.get("postTokenBalances"))
        # wrapped SOL counts as SOL
        wsol = post.pop(SOL_MINT, 0) - pre.pop(SOL_MINT, 0)
        sol_delta += wsol
        swaps = []
        for mint in set(pre) | set(post):
            dt = post.get(mint, 0) - pre.get(mint, 0)
            if abs(dt) > 0 and mint not in S.STABLES:
                swaps.append((sig, mint, dt, sol_delta, ts, pre.get(mint, 0)))
        return swaps

    # ------------------------------------------------------------------ decisions
    def on_swap(self, w, sig, mint, dt, dsol, ts, pre_amt):
        name = w.get("name") or w["address"][:6]
        now = time.time()
        if dt > 0 and dsol < 0:                                        # THEY BOUGHT
            spent = -dsol
            self.recent_buys[mint][w["address"]] = now
            if mint in self.st["pos"]:
                p = self.st["pos"][mint]
                if w["address"] not in p["wallets"]:
                    p["wallets"].append(w["address"])          # another smart wallet joined: follow its exits too
                    log.info("%s also bought %s (%d watched wallets in it now)", name, p["sym"], len(p["wallets"]))
                return
            if spent < self.c["min_their_buy_sol"]:
                return
            buyers = [a for a, t in self.recent_buys[mint].items() if now - t < self.c["consensus_window_min"] * 60]
            consensus = len(buyers) >= self.c["consensus_wallets"]
            their_px = spent / dt if dt else None                     # SOL per token they paid
            self.copy_buy(mint, name, spent, their_px, consensus, len(buyers))
        elif dt < 0 and mint in self.st["pos"] and self.c["follow_sells"]:   # THEY SOLD
            p = self.st["pos"][mint]
            if w["address"] not in p["wallets"]:
                return
            sold_pct = -dt / pre_amt * 100 if pre_amt else 100
            if sold_pct >= self.c["follow_sell_min_pct"]:
                self.copy_sell(mint, 1.0, f"{name} sold {sold_pct:.0f}% of their position")

    def can_buy(self):
        day = time.strftime("%Y%m%d", time.gmtime())
        if self.st["day"] != day:
            self.st["day"], self.st["day_pnl"] = day, 0.0
        if self.st["day_pnl"] <= -self.c["max_daily_loss_sol"]:
            return False, "daily loss limit"
        if len(self.st["pos"]) >= self.c["max_open"]:
            return False, "max open copies"
        return True, ""

    def cash(self):
        if self.c["mode"] == "paper":
            return self.st["sol"]
        bal = self.chain.sol_balance(self.owner)
        realized = sum(r["pnl"] for r in self.st["results"])
        in_pos = sum(p["cost0"] for p in self.st["pos"].values())
        return max(0.0, min(bal, self.c["live_budget_sol"] + realized - in_pos))

    def copy_buy(self, mint, name, their_sol, their_px, consensus, n_buyers):
        ok, why = self.can_buy()
        if not ok:
            log.info("%s bought %s but %s", name, mint[:6], why)
            return
        pairs = M.best_pairs([mint], max_age=0)
        pair = pairs.get(mint)
        if not pair:
            log.info("skip copy of %s's buy %s: no SOL/USD pool found", name, mint[:6])
            return
        sym = pair["baseToken"].get("symbol", mint[:6])
        f = S.features(pair)
        if f["liq"] < self.cfg["base"]["min_liquidity_usd"]:
            log.info("skip copy %s: pool only ${:,.0f}".format(f["liq"]), sym)
            return
        ok, swhy = M.safety_screen(self.cfg, self.chain, mint, sym)
        if not ok:
            log.info("skip copy %s (%s bought): %s", sym, name, swhy)
            return
        size = self.c["consensus_size_sol"] if consensus else self.c["copy_size_sol"]
        size = min(size, self.cash() - 0.01)
        if size < 0.005:
            return
        q = self.jup.quote(SOL_MINT, mint, int(size * M.LAMPORTS))
        tokens_raw = int(q["outAmount"])
        # did the price run away since they bought? compare SOL per token
        dec = self._dec(mint)
        our_px = size / (tokens_raw / 10 ** dec) if tokens_raw else None
        if their_px and our_px and (our_px / their_px - 1) * 100 > self.c["max_price_run_pct"]:
            log.info("skip copy %s: price already %+.0f%% above %s's buy", sym, (our_px / their_px - 1) * 100, name)
            return
        try:   # honeypot / hidden tax check
            back = self.jup.quote(mint, SOL_MINT, int(tokens_raw * 0.98))
            if int(back["outAmount"]) / M.LAMPORTS < size * 0.98 * (1 - self.cfg.get("max_round_trip_loss_pct", 8) / 100):
                log.info("skip copy %s: buy-then-sell would lose too much (tax/honeypot)", sym)
                return
        except Exception:
            log.info("skip copy %s: no sell route (honeypot?)", sym)
            return
        if self.c["mode"] == "live":
            ok, ref = self._execute(q)
            if not ok:
                log.warning("COPY BUY %s failed: %s", sym, ref)
                return
        else:
            ref = "paper"
            self.st["sol"] -= size + self.cfg.get("paper_tx_fee_sol", 0.00015)
        tag = f"CONSENSUS ({n_buyers} wallets)" if consensus else f"copying {name}"
        self.st["pos"][mint] = {"sym": sym, "raw": tokens_raw, "cost0": size, "cost": size, "realized": 0.0,
                                "entry": size, "peak": size, "opened": time.time(), "took": False,
                                "wallets": list(self.recent_buys[mint]), "leader": name}
        log.info("COPY BUY %s %.3f SOL — %s (they spent %.2f SOL) [%s]", sym, size, tag, their_sol, ref)
        M.notify(self.cfg, f"COPY {sym} ({self.c['mode']})", f"{size} SOL — {tag}\nthey spent {their_sol:.2f} SOL\n{pair.get('url', '')}", "busts_in_silhouette")
        self.save()

    def _dec(self, mint):
        info = self.chain.mint_info(mint)
        return int(info["decimals"]) if info else 6

    def _execute(self, q):
        import base64
        from solders.transaction import VersionedTransaction
        tx = VersionedTransaction.from_bytes(base64.b64decode(self.jup.swap_tx(q, self.owner)))
        signed = VersionedTransaction(tx.message, [self.kp])
        sig = self.chain.send(base64.b64encode(bytes(signed)).decode())
        ok, err = self.chain.confirm(sig)
        return ok, sig if ok else f"{sig}: {err}"

    def copy_sell(self, mint, frac, why):
        p = self.st["pos"][mint]
        raw = int(p["raw"] * frac) if frac < 0.999 else p["raw"]
        try:
            q = self.jup.quote(mint, SOL_MINT, raw)
        except Exception as e:
            log.warning("sell quote %s failed: %s", p["sym"], e)
            return
        got = int(q["outAmount"]) / M.LAMPORTS
        if self.c["mode"] == "live":
            ok, ref = self._execute(q)
            if not ok:
                log.warning("COPY SELL %s failed: %s", p["sym"], ref)
                return
        else:
            self.st["sol"] += got - self.cfg.get("paper_tx_fee_sol", 0.00015)
        cost_part = p["cost"] * frac
        p["realized"] += got
        log.info("COPY SELL %s %d%% → %.4f SOL (P&L %+.4f, %+.0f%%) — %s", p["sym"], round(frac * 100), got, got - cost_part,
                 (got / cost_part - 1) * 100 if cost_part else 0, why)
        M.notify(self.cfg, f"COPY SELL {p['sym']} {got - cost_part:+.4f} SOL", why, "moneybag" if got > cost_part else "red_circle")
        if frac >= 0.999:
            pnl = p["realized"] - p["cost0"]
            self.st["results"].append({"sym": p["sym"], "pnl": pnl, "ret": pnl / p["cost0"] * 100, "leader": p["leader"],
                                       "wallets": p["wallets"], "time": time.time()})
            self.st["day_pnl"] += pnl
            del self.st["pos"][mint]
            self.judge_wallets()
        else:
            p["took"] = True
            p["raw"] -= raw
            p["cost"] -= cost_part
            p["entry"] -= cost_part
            p["peak"] *= (1 - frac)
        self.save()

    def judge_wallets(self):
        """Drop wallets whose copies keep losing — past performance on Fomo doesn't guarantee they work for us."""
        by = defaultdict(list)
        for r in self.st["results"]:
            for wlt in r["wallets"]:
                by[wlt].append(r["ret"])
        for wlt, rets in by.items():
            rets = rets[-20:]
            if len(rets) >= self.c["drop_wallet_after"] and sum(rets) / len(rets) < self.c["drop_wallet_expectancy_pct"] \
                    and wlt not in self.st["dropped"]:
                self.st["dropped"].append(wlt)
                log.warning("dropping wallet %s: copies averaged %+.1f%% over %d trades", wlt[:8], sum(rets) / len(rets), len(rets))
                M.notify(self.cfg, "Copy-trader dropped a wallet", f"{wlt}\ncopies averaged {sum(rets) / len(rets):+.1f}%", "warning")

    def manage(self):
        s = {k: self.c[k] for k in ("stop_loss_pct", "take_profit_pct", "take_profit_sell_pct", "trail_activate_pct",
                                    "trailing_stop_pct", "breakeven_trigger_pct", "breakeven_lock_pct", "stale_minutes",
                                    "stale_min_gain_pct", "profit_lock_keep_pct")}
        for mint in list(self.st["pos"]):
            p = self.st["pos"][mint]
            try:
                val = int(self.jup.quote(mint, SOL_MINT, p["raw"])["outAmount"]) / M.LAMPORTS
            except Exception:
                continue
            p["peak"] = max(p["peak"], val)
            frac, why = S.exit_step({"liquidity_drop_exit_pct": 100, "max_hold_hours": 48}, s,
                                    {"entry": p["entry"], "peak": p["peak"], "opened": p["opened"], "took_profit": p["took"],
                                     "entry_liq": None}, val, None, time.time())
            if frac:
                self.copy_sell(mint, frac, why)

    # ------------------------------------------------------------------ loop
    def run(self):
        ws = self.wallets()
        log.info("copy-trader starting — %s mode, %.3f SOL, watching %d wallets", self.c["mode"].upper(), self.cash(), len(ws))
        if not ws:
            log.warning("wallets.json has no enabled wallets yet — add some (see the top of this file)")
        if self.c["mode"] == "live" and sys.stdin.isatty() and not os.getenv("COPY_CONFIRMED"):
            if input(f"Copy-trade with REAL money from {self.owner}, budget {self.c['live_budget_sol']} SOL? Type YES: ").strip() != "YES":
                sys.exit("not confirmed")
        while True:
            try:
                ws_all = self.wallets()
                self.evm.poll([w for w in ws_all if w["address"].lower().startswith("0x")])
                for w in [w for w in ws_all if not w["address"].lower().startswith("0x")]:
                    for sig, mint, dt, dsol, ts, pre_amt in self.new_swaps(w["address"]):
                        self.on_swap(w, sig, mint, dt, dsol, ts, pre_amt)
                    time.sleep(0.3)
                self.manage()
                self.evm.manage({k: self.c[k] for k in ("stop_loss_pct", "take_profit_pct", "take_profit_sell_pct",
                                                         "trail_activate_pct", "trailing_stop_pct", "breakeven_trigger_pct",
                                                         "breakeven_lock_pct", "stale_minutes", "stale_min_gain_pct",
                                                         "profit_lock_keep_pct")})
                self.save()
                time.sleep(self.c["poll_seconds"])
            except KeyboardInterrupt:
                self.save()
                return
            except Exception as e:
                log.warning("loop error (%s): %s", type(e).__name__, str(e)[:120])
                time.sleep(10)

    def status(self):
        res = self.st["results"]
        print(f"mode      {self.c['mode']}   cash {self.cash():.4f} SOL   watching {len(self.wallets())} wallets")
        print(f"copies    {len(res)} closed, {sum(1 for r in res if r['pnl'] > 0)} winners, total {sum(r['pnl'] for r in res):+.4f} SOL")
        for m, p in self.st["pos"].items():
            print(f"open      {p['sym']:<10} {p['cost0']:.3f} SOL  following {p['leader']}")
        print(self.evm.status_line())
        if self.st["dropped"]:
            print(f"dropped   {', '.join(w[:8] for w in self.st['dropped'])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH)])
    cfg, c = load()
    cp = Copier(cfg, c, a.live)
    cp.status() if a.status else cp.run()


if __name__ == "__main__":
    main()
