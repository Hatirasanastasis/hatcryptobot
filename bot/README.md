# Lunyx meme bot (Solana)

Trades Solana meme coins on its own from a wallet you give it. Swaps go through Jupiter, the same route most Solana apps use. Run it on your Fomo wallet's key and every trade shows up in Fomo.

## What it does, every minute
1. **Finds** trending Solana tokens on DexScreener (boosted + newly listed).
2. **Screens** each one. All must pass:
   - liquidity ≥ $30k, 1h volume ≥ $40k, market cap $150k–$25M
   - pair 30 min – 72 h old
   - up 5–120% in the last hour, not dumping in the last 5 min, buys ≥ 1.2× sells
   - mint authority and freeze authority revoked (dev can't print or freeze)
   - no "danger" flags on RugCheck, top holder ≤ 25%
   - Jupiter price impact ≤ 3%
3. **Buys** up to 3 at once, 25% of the balance each, keeping 0.02 SOL for fees.
4. **Sells** (checked every 15 s), first of:
   - +50% → sells half, lets the rest run
   - drops 20% from its peak → sells the rest
   - −20% from entry → stop-loss
   - liquidity drops 40% → sells immediately (rug signal)
   - held 8 h → sells
5. **Limits:** no new buys for the day after losing 0.07 SOL (≈$10). If the wallet falls 40% from its peak, it sells everything and shuts off.

All of it is in `config.json`.

## Setup
```bash
pip install -r requirements.txt
cp .env.example .env
```
Get a free Jupiter key at portal.jup.ag and put it in `.env` as `JUPITER_API_KEY`. Paper mode needs it too, for quotes.

## 1. See what it would pick right now
```bash
python memebot.py --scan
```
Add `-v` to see every rejected token and why.

## 2. Paper trade (real market, fake 0.35 SOL)
```bash
python memebot.py
```
`python memebot.py --status` shows P&L. Everything goes to `memebot.log`.

## 3. Go live
Either:
- **Fomo wallet:** if Fomo lets you export the wallet's private key, put it in `.env` as `SOLANA_PRIVATE_KEY`. The bot then trades the same balance you see in Fomo.
- **Separate wallet:** create a new Phantom wallet, export its private key into `.env`, then send your SOL from Fomo to it.

Then set `"mode": "live"` in `config.json`, delete `state.json`, and run `python memebot.py`.

A free RPC from Helius or QuickNode in `RPC_URL` makes it much more reliable than the public one.

## Notes
- Only Solana. Fomo's Base and BNB coins aren't covered.
- The bot only manages coins it bought itself. Anything you buy by hand in Fomo, it ignores.
- Stopping it with Ctrl+C leaves open coins in the wallet. Run it again and it picks them back up.

## v2 upgrades
- **Confirmation:** a coin must pass the filters on 2 scans at least 45 s apart, and can't jump more than 15% or drop more than 10% in between. That avoids buying the top of a one-candle spike.
- **Better entries:** skips coins mid-spike or dumping (5-minute move outside −8% to +8%), with a falling 6-hour trend, a pool under 5% of market cap, or under 80% of liquidity locked or burned.
- **Better exits:**
  - checks every 5 s instead of 15 s
  - −15% stop
  - sells half at +25%
  - trailing stop of 12% from the peak, active once a coin has been up 8%
  - breakeven stop: once a coin has been up 15%, it can't turn into a loss
  - sells after 90 minutes if the coin isn't up at least 5%
- **Learning:** every scan is recorded to `data/`. `python replay.py` replays the strategy on that data, and `python replay.py --optimize` tests hundreds of settings, tuning on the first 70% of the data and checking on the last 30%.

## v3: multi-strategy
Files: `strategy.py` (the decision logic, shared with backtests), `memebot.py` (trading engine), `replay.py` (backtest and optimizer).

- **Wider coverage:** coins come from DexScreener and GeckoTerminal (trending and top-volume pools). Each coin is followed for 6 hours, up to 300 at once.
- **Three strategies,** each with its own exits:
  - `breakout`: trading volume suddenly 2.5× its hourly pace, price rising, buyers ahead. Buys immediately, 10% stop.
  - `dip`: a strong uptrend (1h +20% or more, 6h +20% or more) pulling back 3–15% in the last 5 minutes. Buys once the drop stabilises, 12% stop.
  - `momentum`: the v2 rules, confirmed over 2 scans, 15% stop.
- **Risk-based sizing:** each trade risks about 2% of equity if it hits its stop. No more than 20% of equity, or 0.5 SOL, in any one coin. Up to 5 coins at once.
- **Market regime:** if fewer than 35% of active coins are up over the last hour, or SOL is down 2.5% or more in that hour, it halves trade sizes and turns off breakouts.
- **Adapts:** each strategy's trade size follows its last 20 results (half size if losing, 1.5× if winning). A strategy averaging worse than −3% over 10 or more trades is paused for 6 hours.
- **Loss-streak pause:** after 4 losses in a row, no new buys for 30 minutes.
- **Live mode needs two switches:** `"mode": "live"` in `config.json` and `--live` on the command line.

## Social radar (`social.py`)
Watches what big accounts and crypto communities post:
- **Reddit:** CryptoCurrency, solana, memecoins, SatoshiStreetBets, CryptoMoonShots, pumpfun. Free.
- **X (Twitter):** Elon Musk, Donald Trump, CZ, Saylor, Vitalik, Toly. Needs a paid X API key.
- **Truth Social:** Trump's posts. No key needed.
- **CryptoPanic** news. Free key.
- **YouTube:** alerts when a channel you choose goes live. Free key.

What it does with them:
- **VIP post → `hype` strategy:** when Elon, Trump or CZ posts a word or $cashtag matching a coin, the bot considers buying that coin.
  - If several tokens copy the same name, it only takes the one with the **deepest pool**.
  - It **skips** a coin already up more than 25% in the 5 minutes since the post.
  - It still needs every normal safety check.
  - Small size: 10% max (8% live). Exits: −12% stop, sells half at +25%, 10% trailing stop.
- **Buzz:** a ticker mentioned 3× more than normal (5+ mentions in 15 minutes) gets bigger smart sizing ("social buzz").
- **Discovery:** buzzing cashtags the bot isn't tracking get looked up on DexScreener and added.
- **Phone alerts** for any VIP post about crypto, and for watched channels going live.

Limits: it reads posts, not live TV or podcasts. Sniper bots react to famous posts within milliseconds, so the bot's edge is filtering which coin is real and skipping coins that already ran, not being first.

## Launch sniper (`sniper.py`)
Sees every new pump.fun coin the second it is created (PumpPortal live feed) and snipes only the launches with a reason to explode:
- **First second:** the name or ticker matches something Elon, Trump or CZ just posted, a buzzing ticker, an **active event keyword** (`events.json`), or your own `watch_words`.
- **First ~20 seconds (crowd):** 15+ different wallets buy, at least 3 SOL flows in, the creator hasn't sold, and no single wallet holds more than 25%. Coins in a hot narrative need only 8 buyers and 1.5 SOL.
- **Skips:** creators who bought more than 8% at launch, creators launching coin after coin, and blacklisted rug creators (added automatically).
- **Tiny size:** 0.02 SOL per snipe, at most 3 open and 10 per hour, with a 0.1 SOL daily loss limit.
- **Fast exits:** sells half at +50%, 25% trailing stop after +30%, −30% stop, sells at once if the creator sells, and sells after 5 minutes if the coin hasn't lifted off.

```bash
python sniper.py            # paper
python sniper.py --status
```
Live: in `config.json` set `"sniper": {"mode": "live", "live_budget_sol": 0.2}`, then run `python sniper.py --live`. Check PumpPortal's current fees and parameters at pumpportal.fun before going live.

## Narratives and events (`narrative.py`, `events.json`)
- **Events:** coins named after an upcoming or just-happened hyped event get attention from `days_before` until `days_after` the date. GTA 6 (released 19 Nov 2026) is preloaded. Add your own: elections, game and movie releases, Apple events, ETF decisions.
- **Live narratives:** when 5+ different coins share a word within an hour and are rising on average, that word is a hot narrative. You get a phone alert, the main bot sizes those coins bigger, and the sniper relaxes its crowd rule for them.

## Lessons from losses (`lessons.py`)
After every losing trade, the bot compares that strategy's recent losers with its winners, looking at what each coin looked like at the moment of buying: 1h, 5m and 6h moves, buyers vs sellers, volume speed-up, pool size, market cap, pool vs market cap, and coin age. If one clear difference shows up, it becomes a new entry rule for that strategy, for example "momentum: skip coins already up more than 46% in the hour". It's written to `lessons.md` and sent to your phone.

Guardrails:
- needs at least 6 losses and 4 wins for that strategy
- the rule must block at least 60% of the losers and at most 20% of the winners
- rules only make the bot stricter, with at most 4 per strategy
- each rule expires after 48 hours unless the data confirms it again

`--status` lists the active lessons.

## Edge gate: only proven strategies trade (`edge.py`)
Every 30 minutes the bot replays every moment in its recorded market data where momentum, dip, breakout or rocket would have bought. It measures what really happened next with that strategy's own stop-loss, take-profit and time limit, after fees.

A strategy trades only if it fired at least **8 times on 4+ coins**, made at least **+0.5% on average after costs**, and **also made money in the most recent half** of the data. Otherwise it's in **shadow**: the log shows "shadow (not bought): ...", and nothing is bought. Strategies switch on and off by themselves as the evidence changes. `--status` shows each verdict and why.

The prediction model works the same way. It's trained and judged only on coins the bot could actually trade, and it switches on only if its picks made **+1% or more on average after costs** on data it hadn't seen.

Risk defaults:
- 2% risk per trade
- at most 20% in one coin
- 3 coins at once
- daily loss limit 5% of the start balance
- kill switch 25% below the peak

## Regular coins in the same bot
Besides meme coins, the bot trades **14 established Solana coins** from the same wallet and balance:
- **Wrapped Bitcoin and Ethereum:** cbBTC, WETH
- **DeFi and infrastructure:** JUP, RAY, PYTH, JTO, ORCA, RENDER, HNT
- **The biggest long-lived memes:** BONK, WIF, POPCAT, PENGU, FARTCOIN

They use exchange-style strategies (`maj_momentum`, `maj_dip`, `maj_breakout`): stops of 2.5–3%, targets of 4–5%, and up to 25% per coin. They skip the meme rug checks, since cbBTC legitimately has a mint authority, but keep the honeypot round-trip check, and they go through the same edge gate. Edit the list under `"majors" → "tokens"` in `config.json`.
