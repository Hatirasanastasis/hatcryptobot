"""
Social radar — watches what big accounts and crypto communities say, and turns it into
signals the meme bot can use.

Sources (each switches on when its key is in .env; free ones marked):
  * Reddit (free app keys)          REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET   — falls back to public JSON
  * X / Twitter (paid API)          X_BEARER_TOKEN
  * Truth Social (no key)           public Mastodon-style API
  * CryptoPanic news (free key)     CRYPTOPANIC_KEY
  * YouTube live alerts (free key)  YOUTUBE_API_KEY — tells you when a watched channel goes live

What it produces:
  * VIP hits      — a watched person (Elon, Trump, ...) posted a word that matches a coin's ticker/name
  * buzz          — a ticker is suddenly mentioned much more than its normal rate
  * discoveries   — cashtags people are talking about that the bot isn't tracking yet
  * phone alerts  — any VIP post about crypto, and VIP live streams

It cannot listen to TV or podcasts in real time; posts and live-stream starts are what it sees.
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # bundled Python does not add the script folder
import html
import math
import os
import re
import threading
import time
from collections import defaultdict, deque

import requests

COMMON = {  # words that look like tickers but mean nothing on their own
    "THE", "AND", "FOR", "YOU", "ARE", "NOT", "BUT", "ALL", "NEW", "NOW", "ONE", "GET", "OUT", "WHO", "HOW", "WHY",
    "CAN", "WILL", "JUST", "THIS", "THAT", "WITH", "FROM", "HAVE", "WHAT", "WHEN", "YOUR", "ABOUT", "GOOD", "GREAT",
    "BIG", "LOL", "OMG", "WOW", "YES", "USA", "AMERICA", "PEOPLE", "TODAY", "TIME", "MAKE", "LIKE", "THANK", "VERY",
    "SOL", "BTC", "ETH", "USD", "USDC", "USDT", "CEO", "AI", "IT", "IS", "TO", "OF", "IN", "ON", "MY", "ME",
}
CRYPTO_WORDS = re.compile(r"\b(crypto|coins?|tokens?|doge|bitcoin|btc|solana|memecoins?|meme coins?|blockchain)\b|\$[a-z]{2,10}\b", re.I)
CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b")
WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{2,15}")
TAGS = re.compile(r"<[^>]+>")

DEFAULT_SOCIAL = {
    "enabled": True,
    "poll_seconds": 60,
    "vip_poll_seconds": 15,          # Truth Social (free) checked every 15 s; X every 60 s to stay inside paid-API quotas
    "x_poll_seconds": 60,
    "reddit_subs": ["CryptoCurrency", "solana", "memecoins", "SatoshiStreetBets", "CryptoMoonShots", "pumpfun"],
    "x_accounts": ["elonmusk", "realDonaldTrump", "cz_binance", "saylor", "VitalikButerin", "aeyakovenko"],
    "truth_accounts": ["realDonaldTrump"],
    "youtube_channels": [],              # channel IDs to alert on when they go live
    "vip_names": ["elonmusk", "realDonaldTrump", "cz_binance"],   # posts from these can trigger the hype strategy
    "buzz_window_min": 15,
    "buzz_min_mentions": 5,
    "buzz_ratio": 3.0,
    "vip_window_min": 15,
}


class SocialRadar:
    def __init__(self, cfg, log, notify):
        self.cfg = {**DEFAULT_SOCIAL, **(cfg.get("social") or {})}
        self.log, self.notify = log, notify
        self.http = requests.Session()
        self.http.headers["User-Agent"] = "memebot-social/1.0 (personal trading bot)"
        self.lock = threading.Lock()
        self.mentions = defaultdict(deque)      # TERM -> deque[(ts, weight, source)]
        self.vip_posts = deque(maxlen=200)      # (ts, who, text, url)
        self.seen = set()
        self.ids = {}
        self.reddit_token = (None, 0)
        self.disabled = {}
        self.status = {}

    # ------------------------------------------------------------------ public
    def start(self):
        if not self.cfg["enabled"]:
            return
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def vip_hits(self, coins: dict, now=None):
        """coins: {mint: (symbol, name)} → {mint: (who, text, url, ts)} for VIP posts in the window that name the coin."""
        now = now or time.time()
        out = {}
        with self.lock:
            posts = [p for p in self.vip_posts if now - p[0] <= self.cfg["vip_window_min"] * 60]
        for ts, who, text, url in posts:
            if who not in self.cfg["vip_names"]:
                continue
            up = text.upper()
            tags = {t.upper() for t in CASHTAG.findall(text)}
            words = set(WORD.findall(up))
            for mint, (sym, name) in coins.items():
                s = (sym or "").upper()
                hit = s in tags or (len(s) >= 3 and s not in COMMON and s in words)
                if not hit and name:
                    nm = name.upper()
                    hit = len(nm) >= 4 and nm not in COMMON and re.search(rf"\b{re.escape(nm)}\b", up) is not None
                if hit and (mint not in out or ts > out[mint][3]):
                    out[mint] = (who, text[:200], url, ts)
        return out

    def buzz(self, symbol, now=None):
        """(mentions in window, ratio vs normal rate). Ratio > buzz_ratio with enough mentions = buzzing."""
        now = now or time.time()
        w = self.cfg["buzz_window_min"] * 60
        with self.lock:
            dq = self.mentions.get((symbol or "").upper())
            if not dq:
                return 0, 0.0
            recent = sum(wt for ts, wt, _ in dq if now - ts <= w)
            older = sum(wt for ts, wt, _ in dq if w < now - ts <= 5 * w)
        base = older / 4 if older else 0.5
        return recent, recent / base

    def buzzing(self, symbol, now=None):
        n, r = self.buzz(symbol, now)
        return n >= self.cfg["buzz_min_mentions"] and r >= self.cfg["buzz_ratio"]

    def discoveries(self, tracked_symbols: set, now=None):
        """Cashtags with real buzz that the bot isn't tracking — the bot looks them up on DexScreener."""
        now = now or time.time()
        with self.lock:
            terms = list(self.mentions)
        return [t for t in terms if t not in tracked_symbols and t not in COMMON and self.buzzing(t, now)][:10]

    # ------------------------------------------------------------------ ingest
    def ingest(self, source, who, text, ts, weight=1.0, url="", vip=False):
        key = (source, who, text[:80], int(ts))
        if key in self.seen:
            return
        self.seen.add(key)
        if len(self.seen) > 20000:
            self.seen = set(list(self.seen)[-10000:])
        terms = {t.upper() for t in CASHTAG.findall(text)}
        if vip:
            terms |= {w for w in WORD.findall(text.upper()) if w not in COMMON}
        with self.lock:
            for t in terms:
                dq = self.mentions[t]
                dq.append((ts, weight * (10 if vip else 1), source))
                while dq and time.time() - dq[0][0] > 6 * 3600:
                    dq.popleft()
            if vip:
                self.vip_posts.append((ts, who, text, url))
        if vip and CRYPTO_WORDS.search(text) and time.time() - ts < 3600:
            self.log.warning("VIP post — %s: %s", who, text[:160])
            self.notify(f"{who} posted about crypto", f"{text[:300]}\n{url}", "loudspeaker")

    # ------------------------------------------------------------------ loop
    def _loop(self):
        last = defaultdict(float)
        every = {"truth": "vip_poll_seconds", "x": "x_poll_seconds"}
        while True:
            for name, fn in (("reddit", self._reddit), ("x", self._x), ("truth", self._truth),
                             ("cryptopanic", self._cryptopanic), ("youtube", self._youtube)):
                if self.disabled.get(name, 0) > time.time():
                    continue
                if time.time() - last[name] < self.cfg[every.get(name, "poll_seconds")]:
                    continue
                last[name] = time.time()
                try:
                    n = fn()
                    if n is not None:
                        self.status[name] = f"ok ({n} items)"
                except Exception as e:
                    self.status[name] = f"error: {type(e).__name__}"
                    self.disabled[name] = time.time() + 600     # back off 10 min on failure
                    self.log.debug("social %s failed: %s", name, e)
            time.sleep(5)

    def _get(self, url, **kw):
        r = self.http.get(url, timeout=15, **kw)
        if r.status_code in (401, 403, 429):
            raise RuntimeError(f"{url.split('/')[2]} returned {r.status_code}")
        r.raise_for_status()
        return r.json()

    def _reddit(self):
        cid, sec = os.getenv("REDDIT_CLIENT_ID"), os.getenv("REDDIT_CLIENT_SECRET")
        headers, base = {}, "https://www.reddit.com"
        if cid and sec:
            tok, exp = self.reddit_token
            if not tok or time.time() > exp:
                r = self.http.post("https://www.reddit.com/api/v1/access_token", auth=(cid, sec),
                                   data={"grant_type": "client_credentials"}, timeout=15)
                r.raise_for_status()
                j = r.json()
                tok, exp = j["access_token"], time.time() + j.get("expires_in", 3600) - 60
                self.reddit_token = (tok, exp)
            headers, base = {"Authorization": f"bearer {tok}"}, "https://oauth.reddit.com"
        n = 0
        for sub in self.cfg["reddit_subs"]:
            path = f"/r/{sub}/new" + ("" if cid else ".json")
            j = self._get(base + path, params={"limit": 50}, headers=headers)
            for c in j.get("data", {}).get("children", []):
                d = c["data"]
                text = f"{d.get('title', '')} {d.get('selftext', '')[:500]}"
                weight = 1 + math.log1p(max(0, d.get("score", 0)) + d.get("num_comments", 0)) / 3
                self.ingest("reddit", d.get("author", "?"), text, d.get("created_utc", time.time()), weight,
                            "https://reddit.com" + d.get("permalink", ""))
                n += 1
            time.sleep(1)
        return n

    def _x(self):
        tok = os.getenv("X_BEARER_TOKEN")
        if not tok:
            return None
        h = {"Authorization": f"Bearer {tok}"}
        n = 0
        for user in self.cfg["x_accounts"]:
            uid = self.ids.get(("x", user))
            if not uid:
                uid = self._get(f"https://api.x.com/2/users/by/username/{user}", headers=h)["data"]["id"]
                self.ids[("x", user)] = uid
            j = self._get(f"https://api.x.com/2/users/{uid}/tweets", headers=h,
                          params={"max_results": 5, "tweet.fields": "created_at", "exclude": "replies"})
            for t in j.get("data", []):
                ts = time.mktime(time.strptime(t["created_at"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
                self.ingest("x", user, t["text"], ts, 1, f"https://x.com/{user}/status/{t['id']}", vip=True)
                n += 1
        return n

    def _truth(self):
        n = 0
        for acct in self.cfg["truth_accounts"]:
            uid = self.ids.get(("truth", acct))
            if not uid:
                uid = self._get("https://truthsocial.com/api/v1/accounts/lookup", params={"acct": acct})["id"]
                self.ids[("truth", acct)] = uid
            for s in self._get(f"https://truthsocial.com/api/v1/accounts/{uid}/statuses", params={"limit": 10}):
                text = html.unescape(TAGS.sub(" ", s.get("content", ""))).strip()
                ts = time.mktime(time.strptime(s["created_at"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
                self.ingest("truth", acct, text, ts, 1, s.get("url", ""), vip=True)
                n += 1
        return n

    def _cryptopanic(self):
        key = os.getenv("CRYPTOPANIC_KEY")
        if not key:
            return None
        # FILL: if CryptoPanic moves the endpoint, update it here (developer docs: cryptopanic.com/developers/api)
        j = self._get("https://cryptopanic.com/api/v1/posts/", params={"auth_token": key, "public": "true", "kind": "all"})
        n = 0
        for p in j.get("results", []):
            ts = time.mktime(time.strptime(p["published_at"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
            text = p.get("title", "") + " " + " ".join("$" + c["code"] for c in p.get("currencies") or [])
            votes = p.get("votes") or {}
            self.ingest("news", p.get("source", {}).get("domain", "news"), text, ts,
                        1 + (votes.get("positive", 0) + votes.get("important", 0)) / 5, p.get("url", ""))
            n += 1
        return n

    def _youtube(self):
        key = os.getenv("YOUTUBE_API_KEY")
        if not key or not self.cfg["youtube_channels"]:
            return None
        n = 0
        for ch in self.cfg["youtube_channels"]:
            j = self._get("https://www.googleapis.com/youtube/v3/search", params={
                "part": "snippet", "channelId": ch, "eventType": "live", "type": "video", "key": key})
            for it in j.get("items", []):
                vid = it["id"]["videoId"]
                if ("yt", vid) in self.seen:
                    continue
                self.seen.add(("yt", vid))
                sn = it["snippet"]
                self.log.warning("LIVE now — %s: %s", sn["channelTitle"], sn["title"])
                self.notify(f"{sn['channelTitle']} is LIVE", f"{sn['title']}\nhttps://youtube.com/watch?v={vid}", "red_circle")
                self.ingest("youtube", sn["channelTitle"], sn["title"] + " " + sn.get("description", ""), time.time())
                n += 1
        return n
