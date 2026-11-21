# Going live with real money

Two bots, two kinds of account. Both trade **inside your own account**. Your money never leaves it, and nobody else (me included) can see your keys.

| You trade on | Bot | Connects with |
|---|---|---|
| Solana wallet: Phantom, Solflare, Backpack, or Fomo (if it lets you export the key) | `meme/memebot.py` (v3: three strategies, smart sizing, self-tuning) | the wallet's private key |
| Exchange: Binance, Bybit, OKX, Kraken, Coinbase, Bitget, Gate and 95+ more | `bot.py` (trend bot for BTC/ETH and other major coins) | an API key with **trading only, withdrawals off** |

## Before real money
1. Run paper mode for at least **1–2 days** (meme bot) or **1 week** (exchange bot). Check `--status`.
2. Exchange bot: run **demo mode** on the exchange's test account first (see README).
3. Go live only if paper results are positive after fees. Start with money you can lose completely.

## Where it runs
Only on **your own computer or your own server**, never in someone else's cloud. The private key and API keys stay in the `.env` file on that machine. A $5/month server (Hetzner, DigitalOcean, Vultr) keeps it running 24/7.

```bash
pip install -r requirements.txt
```

## Meme bot (Solana wallet) → live
1. **Use a separate wallet for the bot.** Create a new wallet in Phantom, and send it only the amount you want the bot to trade. Even if something goes wrong, your main wallet is untouched.
2. Export that wallet's private key (Phantom: Settings → your account → Show private key). Put it in `meme/.env`:
   ```
   SOLANA_PRIVATE_KEY=...
   JUPITER_API_KEY=...        # free at portal.jup.ag
   RPC_URL=...                # free at helius.dev: much more reliable than the public one
   ```
3. In `meme/config.json`:
   ```json
   "mode": "live",
   "live_budget_sol": 0.68,
   "risk_per_trade_pct": 2.0
   ```
   `live_budget_sol` is the most the bot will ever use, even if the wallet holds more. For real money, keep risk at 2%, not the 4% used for testing.
4. Delete `state.json` (paper history), then start the bot:
   ```bash
   cd meme && python memebot.py --live
   ```
   It shows the wallet and the budget and asks you to type `YES`.

## Exchange bot → live
1. On the exchange: API Management → new key → enable **Spot trading** only. **Leave withdrawals off.** If the exchange offers IP whitelisting, lock the key to your server's IP.
2. `.env`: `API_KEY=...`, `API_SECRET=...` (and `API_PASSWORD=...` for OKX, Bitget and KuCoin).
3. `config.json`: `"mode": "live"`, `"exchange": "binance"` (or yours), `"symbol": "BTC/USDT"`, `"live_budget": 50`.
4. Delete `state.json`, then:
   ```bash
   python bot.py
   ```
   Type `YES` when asked.

## Safety built in
- **Budget cap:** the bot never uses more than `live_budget` or `live_budget_sol`.
- **Hard limits:** a stop-loss on every trade, a daily loss limit, a losing-streak pause, and a kill switch at 40% (meme bot) or 25% (exchange bot) below the peak.
- **Two switches for live:** live needs both `"mode": "live"` and the confirmation (`--live` and `YES` for the meme bot, `YES` for the exchange bot).
- **Alerts:** every trade goes to your phone through ntfy.
- **Stopping:** Ctrl+C stops it. Open coins stay in your wallet or account, and you can sell them yourself in Phantom, Fomo or the exchange app.

## Stop immediately if
- the balance falls faster than paper testing suggested
- you see errors repeating in `memebot.log` or `bot.log`
- a trade happens that you don't understand

## Live guard (automatic)
In live mode the meme bot clamps every risky setting, whatever `config.json` says:
- risk per trade: at most 2%
- max in one coin: at most 20%
- coins at once: at most 4
- price impact: at most 1.5%
- slippage: at most 2%
- losing-streak pause after 3 losses
- kill switch at 30% below the peak
- daily loss limit: at most 10% of the budget
- smart sizing: at most 1.25×
- model trades: at most 10% of the balance
- market-mood filter: always on
- coins must have a pool of at least $40k, volume of at least $25k, an age of at least 30 minutes, and a pool of at least 5% of market cap
- momentum needs 2 confirmation checks
- momentum and breakout skip coins already up more than 60% or 50% in the hour

It also re-checks the price right before signing and skips the buy if the price moved more than 5% since the scan. If a sell fails, it retries up to 3 times with wider slippage and alerts your phone, because getting out matters more than the exit price. Every clamp is printed in the log at startup.
