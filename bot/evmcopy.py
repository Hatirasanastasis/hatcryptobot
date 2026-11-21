"""Copy-trading on EVM chains (Base, Ethereum, BNB Chain) — for wallets that start with 0x.

The same wallet address is watched on every chain. A watched wallet's buys are seen from the chain itself
(ERC-20 Transfer events into the wallet, in transactions the wallet sent). Paper money in USD for now:
prices and pools come from DexScreener, and every copy first passes a honeypot / sell-tax check.

THEY BUY  → copy (small fixed USD size) if the pool is big enough, the token can be sold back with low tax,
            and the price hasn't already run away. 2+ watched wallets in the same token within 30 min = bigger copy.
THEY SELL → sell too. Own exits as well: stop-loss, take-profit, trailing stop, profit lock, time limit.
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # bundled Python does not add the script folder

import time
from collections import defaultdict

import requests

import strategy as S

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
CHAINS = {
    # name: (rpc, dexscreener chain id, honeypot.is chain id, wrapped native, block seconds)
    "base": (["https://base.drpc.org", "https://mainnet.base.org"], "base", 8453, "0x4200000000000000000000000000000000000006", 2),
    "ethereum": (["https://eth.drpc.org", "https://eth.llamarpc.com"], "ethereum", 1, "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", 12),
    "bsc": (["https://bsc.drpc.org", "https://bsc-dataseed.binance.org", "https://bsc-dataseed1.defibit.io"], "bsc", 56, "0xbb4cdb9cbd36b01bd1cbef880a3c2d55c095f39c", 3),
}
QUOTES = {  # what a "buy" pays with: native wrappers and stablecoins (lowercase)
    "0x4200000000000000000000000000000000000006", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # base WETH, USDC
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # eth WETH, USDC
    "0xdac17f958d2ee523a2206206994597c13d831ec7",                                               # eth USDT
    "0xbb4cdb9cbd36b01bd1cbef880a3c2d55c095f39c", "0x55d398326f99059ff775485246999027b3197955",  # bsc WBNB, USDT
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",                                               # bsc USDC
}
EVM_DEFAULTS = {"evm_enabled": True, "evm_paper_start_usd": 500.0, "evm_copy_usd": 25.0, "evm_consensus_usd": 50.0,
                "evm_min_liq_usd": 30000, "evm_max_tax_pct": 10, "evm_max_open": 5, "evm_max_run_pct": 25}


def topic_addr(a):
    return "0x" + "0" * 24 + a.lower()[2:]


class EvmCopy:
    def __init__(self, cfg, st, log, notify):
        self.c = {**EVM_DEFAULTS, **cfg}
        self.st, self.log, self.notify = st, log, notify
        st.setdefault("evm", {"usd": self.c["evm_paper_start_usd"], "pos": {}, "results": [], "last_block": {}})
        self.e = st["evm"]
        self.http = requests.Session()
        self.recent = defaultdict(dict)       # (chain, token) -> {wallet: ts}
        self.down = {}

    # ------------------------------------------------------------------ chain
    def rpc(self, chain, method, params):
        """Free public servers; a private one can be set with EVM_RPC_BASE / EVM_RPC_ETHEREUM / EVM_RPC_BSC.
        Tries each server in turn and remembers the one that works."""
        import os
        urls = ([os.getenv(f"EVM_RPC_{chain.upper()}")] if os.getenv(f"EVM_RPC_{chain.upper()}") else []) + CHAINS[chain][0]
        good = self.__dict__.setdefault("_good", {}).get(chain)
        if good in urls:
            urls = [good] + [u for u in urls if u != good]
        err = None
        for url in urls:
            try:
                r = self.http.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
                r.raise_for_status()
                j = r.json()
                if "error" in j:
                    raise RuntimeError(f"{chain} {method}: {j['error']}")
                self._good[chain] = url
                return j["result"]
            except Exception as e:
                err = e
        raise err

    def poll(self, wallets):
        """Check every watched 0x wallet on every chain for new token transfers since the last look."""
        if not self.c["evm_enabled"] or not wallets:
            return
        for chain in CHAINS:
            if time.time() < self.down.get(chain, 0):
                continue
            try:
                head = int(self.rpc(chain, "eth_blockNumber", []), 16)
                last = self.e["last_block"].get(chain)
                if last is None or head - last > 600:
                    self.e["last_block"][chain] = head          # first look (or long gap): start from now
                    continue
                if head <= last:
                    continue
                frm, to = last + 1, min(head, last + 100)
                for w in wallets:
                    a = w["address"].lower()
                    inc = self.rpc(chain, "eth_getLogs", [{"fromBlock": hex(frm), "toBlock": hex(to),
                                                           "topics": [TRANSFER, None, topic_addr(a)]}])
                    out = self.rpc(chain, "eth_getLogs", [{"fromBlock": hex(frm), "toBlock": hex(to),
                                                           "topics": [TRANSFER, topic_addr(a)]}])
                    self.handle(chain, w, a, inc, out)
                self.e["last_block"][chain] = to
            except Exception as e:
                self.log.debug("evm %s poll failed: %s", chain, e)
                self.down[chain] = time.time() + 60

    def handle(self, chain, w, a, inc, out):
        name = w.get("name") or a[:8]
        bought = {}
        for lg in inc:
            tok = lg["address"].lower()
            if tok not in QUOTES and len(lg.get("topics", [])) == 3:   # ERC-20 (not NFT: those have 4 topics)
                bought.setdefault(lg["transactionHash"], set()).add(tok)
        for tx, toks in bought.items():
            try:
                t = self.rpc(chain, "eth_getTransactionByHash", [tx])
            except Exception:
                continue
            if not t or (t.get("from") or "").lower() != a:
                continue                                    # an airdrop / transfer to them, not their own buy
            for tok in toks:
                self.recent[(chain, tok)][a] = time.time()
                self.copy_buy(chain, tok, name)
        for lg in out:
            tok = lg["address"].lower()
            p = self.e["pos"].get(f"{chain}:{tok}")
            if p and a in p["wallets"]:
                self.copy_sell(f"{chain}:{tok}", f"{name} sold")

    # ------------------------------------------------------------------ market
    def pair(self, chain, tok):
        r = self.http.get(f"https://api.dexscreener.com/latest/dex/tokens/{tok}", timeout=15).json()
        ps = [p for p in (r.get("pairs") or []) if p.get("chainId") == CHAINS[chain][1]]
        return max(ps, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0) if ps else None

    def honeypot(self, chain, tok):
        try:
            r = self.http.get("https://api.honeypot.is/v2/IsHoneypot",
                              params={"address": tok, "chainID": CHAINS[chain][2]}, timeout=15).json()
        except Exception:
            return False, "honeypot check unavailable"
        hp = (r.get("honeypotResult") or {}).get("isHoneypot")
        sim = r.get("simulationResult") or {}
        tax = max(float(sim.get("buyTax") or 0), float(sim.get("sellTax") or 0))
        if hp:
            return False, "honeypot (can't be sold)"
        if tax > self.c["evm_max_tax_pct"]:
            return False, f"{tax:.0f}% tax"
        return True, "ok"

    # ------------------------------------------------------------------ trading (paper, USD)
    def copy_buy(self, chain, tok, name):
        key = f"{chain}:{tok}"
        if key in self.e["pos"]:
            if name not in self.e["pos"][key]["leaders"]:
                self.e["pos"][key]["leaders"].append(name)
            return
        if len(self.e["pos"]) >= self.c["evm_max_open"]:
            return
        pr = self.pair(chain, tok)
        if not pr:
            self.log.info("evm skip %s %s: no pool on DexScreener", chain, tok[:8])
            return
        sym = (pr.get("baseToken") or {}).get("symbol") or tok[:8]
        liq = (pr.get("liquidity") or {}).get("usd") or 0
        if liq < self.c["evm_min_liq_usd"]:
            self.log.info("evm skip %s on %s (%s bought): pool only $%s", sym, chain, name, f"{liq:,.0f}")
            return
        m5 = ((pr.get("priceChange") or {}).get("m5")) or 0
        if m5 > self.c["evm_max_run_pct"]:
            self.log.info("evm skip %s on %s: already %+.0f%% in 5 min", sym, chain, m5)
            return
        ok, why = self.honeypot(chain, tok)
        if not ok:
            self.log.info("evm skip %s on %s (%s bought): %s", sym, chain, name, why)
            return
        buyers = [w for w, t in self.recent[(chain, tok)].items() if time.time() - t < 1800]
        size = self.c["evm_consensus_usd"] if len(buyers) >= 2 else self.c["evm_copy_usd"]
        size = min(size, self.e["usd"])
        if size < 5:
            return
        px = float(pr.get("priceUsd") or 0)
        if px <= 0:
            return
        cost = size * 1.01                                   # ~1% swap fee + slippage
        self.e["usd"] -= cost
        self.e["pos"][key] = {"sym": sym, "chain": chain, "qty": size / px, "cost0": cost, "entry": cost, "peak": cost,
                              "opened": time.time(), "took": False, "leaders": [name], "wallets": buyers or [],
                              "url": pr.get("url")}
        tag = f"CONSENSUS ({len(buyers)} wallets)" if len(buyers) >= 2 else f"copying {name}"
        self.log.info("COPY BUY %s on %s $%.0f — %s, pool $%s [paper]", sym, chain, size, tag, f"{liq:,.0f}")
        self.notify(f"COPY {sym} ({chain}, paper)", f"${size:.0f} — {tag}\n{pr.get('url', '')}", "busts_in_silhouette")

    def value(self, p, key):
        pr = self.pair(p["chain"], key.split(":", 1)[1])
        return p["qty"] * float((pr or {}).get("priceUsd") or 0) * 0.99 if pr else None

    def copy_sell(self, key, why, frac=1.0):
        p = self.e["pos"][key]
        v = self.value(p, key)
        if v is None:
            return
        got = v * frac
        self.e["usd"] += got
        cost = p["entry"] * frac
        self.log.info("COPY SELL %s on %s %d%% → $%.2f (P&L %+.2f, %+.0f%%) — %s [paper]", p["sym"], p["chain"],
                      round(frac * 100), got, got - cost, (got / cost - 1) * 100 if cost else 0, why)
        if frac >= 0.999:
            pnl = got + p.get("realized", 0) - p["cost0"]
            self.e["results"].append({"sym": p["sym"], "chain": p["chain"], "pnl": round(pnl, 2),
                                      "ret": round(pnl / p["cost0"] * 100, 1), "leaders": p["leaders"], "time": time.time()})
            del self.e["pos"][key]
        else:
            p["realized"] = p.get("realized", 0) + got
            p["qty"] *= 1 - frac
            p["entry"] -= cost
            p["peak"] *= 1 - frac
            p["took"] = True

    def manage(self, s):
        for key in list(self.e["pos"]):
            p = self.e["pos"][key]
            v = self.value(p, key)
            if v is None:
                continue
            p["peak"] = max(p["peak"], v)
            frac, why = S.exit_step({"liquidity_drop_exit_pct": 100, "max_hold_hours": 48}, s,
                                    {"entry": p["entry"], "peak": p["peak"], "opened": p["opened"],
                                     "took_profit": p["took"], "entry_liq": None}, v, None, time.time())
            if frac:
                self.copy_sell(key, why, frac)

    def status_line(self):
        r = self.e["results"]
        inpos = sum(p["entry"] for p in self.e["pos"].values())
        return (f"evm       ${self.e['usd'] + inpos:,.2f} (cash ${self.e['usd']:,.2f}), {len(self.e['pos'])} open, "
                f"{len(r)} closed, {sum(1 for x in r if x['pnl'] > 0)} winners, total {sum(x['pnl'] for x in r):+.2f} USD")
