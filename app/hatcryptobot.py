"""
HatCryptoBot — one app for the trading bot.

Starts a private local server (only this PC can reach it) and opens the HatCryptoBot window in the browser.
Everything is controlled from that window: dashboard, start/stop, strategies, settings, wallet keys,
copy-trading wallets, live log, help. The bot itself runs in the background.

Writable files (settings, keys, history) live in %LOCALAPPDATA%\\HatCryptoBot\\bot (or ~/.hatcryptobot/bot elsewhere),
so the program folder can stay read-only.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parent
if not (APP_DIR / "ui.html").exists() and (APP_DIR.parent / "ui.html").exists():
    APP_DIR = APP_DIR.parent           # installed layout: code in pkgs\, ui.html and bot\ next to it
SRC_BOT = APP_DIR / "bot"                                   # shipped copy of meme/
if os.name == "nt":
    HOME = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HatCryptoBot"
else:
    HOME = Path.home() / ".hatcryptobot"
BOT = HOME / "bot"
PORT = int(os.environ.get("HCB_PORT", "8777"))
KEEP = {"config.json", ".env", "wallets.json", "events.json", "state.json", "sniper_state.json", "copy_state.json"}
PROCS = {}                                                  # name -> Popen
LIVE = {}                                                   # name -> started in live mode (for restarts)
APP_VERSION = "1.7.1"
UPDATE_URL = "https://raw.githubusercontent.com/Hatirasanastasis/hatcryptobot/main/"
UPDATE_STATUS = {"checked": None, "msg": "not checked yet", "latest": None}


def vtuple(v):
    try:
        return tuple(int(x) for x in str(v).strip().split("."))
    except Exception:
        return (0,)


def installed_version():
    try:
        return (BOT / "version.txt").read_text().strip()
    except Exception:
        return "0"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ============================================================================ files

def install_bot():
    """Copy the bot into the user's writable folder; keep their settings, keys and history on updates."""
    BOT.mkdir(parents=True, exist_ok=True)
    if not SRC_BOT.exists():
        return
    if vtuple(installed_version()) > vtuple(APP_VERSION):
        return                              # auto-update already installed newer bot files: keep them
    for p in SRC_BOT.rglob("*"):
        rel = p.relative_to(SRC_BOT)
        dst = BOT / rel
        if p.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        if rel.name in KEEP and dst.exists():
            continue
        if rel.parts[0] in ("data", "archive"):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
    (BOT / "version.txt").write_text(APP_VERSION)


def check_update(apply=True):
    """Download newer bot files from the HatCryptoBot repository. Settings, keys, wallet, history and
    everything learned (data/) are never touched. Files are checked against their SHA-256 before use."""
    import hashlib
    import urllib.request
    UPDATE_STATUS["checked"] = time.time()
    try:
        with urllib.request.urlopen(UPDATE_URL + "manifest.json", timeout=20) as r:
            man = json.loads(r.read())
    except Exception as e:
        UPDATE_STATUS["msg"] = f"could not check ({type(e).__name__})"
        return False
    latest = man.get("version", "0")
    UPDATE_STATUS["latest"] = latest
    if vtuple(latest) <= vtuple(installed_version()):
        UPDATE_STATUS["msg"] = f"up to date ({installed_version()})"
        return False
    if not apply:
        UPDATE_STATUS["msg"] = f"update {latest} available"
        return False
    staged = {}
    for rel, sha in (man.get("files") or {}).items():
        if ".." in rel or rel.startswith(("/", "\\")):
            continue
        if rel.startswith("bot/"):
            dst = BOT / rel[4:]
            if Path(rel).name in KEEP and dst.exists():
                continue                                  # never overwrite the user's settings/keys/state
            if Path(rel).parts[1:2] and Path(rel).parts[1] in ("data", "archive"):
                continue
        elif rel == "ui.html":
            dst = HOME / "ui.html"
        elif rel == "app/hatcryptobot.py":
            dst = HOME / "app" / "hatcryptobot.py"
        else:
            continue
        try:
            with urllib.request.urlopen(UPDATE_URL + rel, timeout=30) as r:
                data = r.read()
        except Exception as e:
            UPDATE_STATUS["msg"] = f"download failed: {rel} ({type(e).__name__})"
            return False
        if hashlib.sha256(data).hexdigest() != sha:
            UPDATE_STATUS["msg"] = f"update rejected: {rel} failed its integrity check"
            return False
        staged[dst] = data
    for dst, data in staged.items():
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".new")
        tmp.write_bytes(data)
        tmp.replace(dst)
    apply_patches(man.get("patches") or [])
    (BOT / "version.txt").write_text(latest)
    # restart running bots so they use the new code (their state is saved on disk)
    again = [n for n in list(PROCS) if running(n)]
    for n in again:
        stop(n)
    for n in again:
        start(n, LIVE.get(n, False))
    UPDATE_STATUS["msg"] = f"updated to {latest}" + (f", restarted: {', '.join(again)}" if again else "") + \
                           (" — restart the app to finish" if vtuple(latest) > vtuple(APP_VERSION)
                            and any(str(d).endswith("hatcryptobot.py") for d in staged) else "")
    print(f"  {UPDATE_STATUS['msg']}")
    return True


def apply_patches(patches):
    """Strategy improvements for the user's own config files (which updates never replace). Each patch runs once;
    only strategy internals are patched — never budgets, keys, risk or anything set on the Settings page."""
    done_f = BOT / "patches_done.json"
    try:
        done = set(json.loads(done_f.read_text()))
    except Exception:
        done = set()
    for p in patches:
        pid, path = p.get("id"), str(p.get("path", ""))
        if not pid or pid in done or not (path.startswith("strategies.") or path in ("autotune",)) or p.get("file") not in ("config.json", "market/config.json"):
            continue
        cfg = jread(p["file"], {}) or {}
        d = cfg
        parts = path.split(".")
        for k in parts[:-1]:
            d = d.setdefault(k, {})
        d[parts[-1]] = p.get("value")
        jwrite(p["file"], cfg)
        done.add(pid)
    done_f.write_text(json.dumps(sorted(done)))


def update_loop():
    time.sleep(60)
    while True:
        try:
            check_update()
        except Exception as e:
            UPDATE_STATUS["msg"] = f"update error: {e}"
        time.sleep(3600)


def jread(name, default=None):
    try:
        return json.loads((BOT / name).read_text(encoding="utf-8"))
    except Exception:
        return default


def jwrite(name, obj):
    (BOT / name).parent.mkdir(parents=True, exist_ok=True)
    tmp = BOT / (name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    tmp.replace(BOT / name)


def read_env():
    env = {}
    p = BOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


B58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def parse_keypair(raw):
    """Keypair from a private key string, or None. Checks the text first: solders crashes hard on bad input."""
    from solders.keypair import Keypair
    raw = (raw or "").strip()
    try:
        if raw.startswith("["):
            b = bytes(json.loads(raw))
            return Keypair.from_bytes(b) if len(b) == 64 else None
        if 80 <= len(raw) <= 90 and set(raw) <= B58:
            return Keypair.from_base58_string(raw)
    except BaseException:          # pyo3 PanicException is not an Exception
        return None
    return None


def wallet_address(env=None):
    raw = (env or read_env()).get("SOLANA_PRIVATE_KEY", "").strip()
    if not raw:
        return ""
    kp = parse_keypair(raw)
    return str(kp.pubkey()) if kp else "invalid key — this is not a Solana private key (wrong box?)"


def write_env(updates):
    env = read_env()
    for k, v in updates.items():
        if v is not None and v != "":
            env[k] = v
    lines = ["# HatCryptoBot keys — keep this file private. Never send it to anyone."] + [f"{k}={v}" for k, v in env.items()]
    (BOT / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def tail(path, n=200):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            return f.read().decode(errors="replace").splitlines()[-n:]
    except OSError:
        return []


# ============================================================================ processes

SCRIPTS = {"bot": "memebot.py", "sniper": "sniper.py", "copy": "copytrade.py", "okx": "market/marketbot.py"}


def pid_alive(pid):
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                             creationflags=NO_WINDOW).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def port_answers():
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/state", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def free_port():
    """Stop whatever process holds the app's port (a frozen old copy of the app)."""
    if os.name != "nt":
        return
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             creationflags=NO_WINDOW).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{PORT}") and parts[3].upper() == "LISTENING":
                pid = parts[4]
                if pid.isdigit() and int(pid) != os.getpid():
                    subprocess.run(["taskkill", "/PID", pid, "/F", "/T"], capture_output=True, creationflags=NO_WINDOW)
    except Exception:
        pass


def kill_leftovers():
    """A bot left running by an app that was closed unexpectedly is stopped, so two bots never trade one account."""
    for name in SCRIPTS:
        f = BOT / f"{name}.pid"
        if f.exists():
            try:
                pid = int(f.read_text())
                if pid_alive(pid):
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=NO_WINDOW)
                    else:
                        os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
            f.unlink(missing_ok=True)


def running(name):
    p = PROCS.get(name)
    return p is not None and p.poll() is None


def start(name, live=False):
    if running(name):
        return True, "already running"
    args = [sys.executable, SCRIPTS[name]] + (["--live"] if live else [])
    LIVE[name] = bool(live)
    env = dict(os.environ, MEMEBOT_CONFIRMED="1", SNIPER_CONFIRMED="1", COPY_CONFIRMED="1", PYTHONUTF8="1")
    PROCS[name] = subprocess.Popen(args, cwd=BOT, stdout=subprocess.DEVNULL, stderr=open(BOT / f"{name}-errors.log", "a"),
                                   stdin=subprocess.DEVNULL, env=env, creationflags=NO_WINDOW)
    (BOT / f"{name}.pid").write_text(str(PROCS[name].pid))
    time.sleep(1.5)
    if not running(name):
        err = "\n".join(tail(BOT / f"{name}-errors.log", 6))
        return False, err or "it stopped right away — check the log"
    return True, "started"


def stop(name):
    p = PROCS.get(name)
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
    (BOT / f"{name}.pid").unlink(missing_ok=True)
    return True, "stopped"


# ============================================================================ state for the UI

def sol_usd(st):
    return st.get("sol_usd") or 0


def snapshot():
    cfg = jread("config.json", {}) or {}
    st = jread("state.json", {}) or {}
    px = sol_usd(st)
    pos = st.get("positions", {})
    sol_free = st.get("sol_free", st.get("paper_sol", cfg.get("paper_start_sol", 0)))
    eq = sol_free + sum(p.get("last_value_sol", p.get("cost_sol", 0)) for p in pos.values())
    start_eq = st.get("start_equity") or cfg.get("paper_start_sol") or eq or 1
    res = st.get("results", [])
    wins = sum(1 for r in res if r.get("pnl_sol", 0) > 0)
    by = {}
    for r in res:
        b = by.setdefault(r["strategy"], {"n": 0, "w": 0, "pnl": 0.0})
        b["n"] += 1
        b["w"] += r["pnl_sol"] > 0
        b["pnl"] += r["pnl_sol"]
    edge = jread("data/edge.json", {}) or {}
    model = jread("data/model.json", {}) or {}
    env = read_env()
    return {
        "mode": cfg.get("mode", "paper"), "running": {n: running(n) for n in SCRIPTS},
        "sol_usd": px, "equity_sol": eq, "start_sol": start_eq, "cash_sol": sol_free,
        "positions": [{"sym": p.get("symbol"), "strategy": p.get("strategy"), "cost": p.get("cost_sol"),
                       "value": p.get("last_value_sol", p.get("cost_sol")), "opened": p.get("opened"),
                       "url": p.get("pair_url")} for p in pos.values()],
        "trades": list(reversed(st.get("trades", [])[-60:])),
        "closed": len(res), "wins": wins, "by_strategy": by,
        "edge": {"verdict": edge.get("verdict", {}), "why": edge.get("why", {}), "time": edge.get("time")},
        "model": {"active": model.get("active"), "why": model.get("why"), "auc": model.get("auc")},
        "equity_history": st.get("equity_history", [])[-720:],
        "killed": st.get("killed"), "halted": st.get("halted_until"),
        "keys": {"wallet": bool(env.get("SOLANA_PRIVATE_KEY")), "jupiter": bool(env.get("JUPITER_API_KEY")),
                 "rpc": bool(env.get("RPC_URL"))},
        "settings": {k: cfg.get(k) for k in SETTINGS},
        "ntfy": cfg.get("ntfy_topic", ""), "home": str(BOT),
        "okx": okx_snapshot(),
        "okx_error": ("" if running("okx") else next((l for l in reversed(tail(BOT / "okx-errors.log", 40))
                                                       if l.startswith("OKX ")), "")[:300]),
        "ai_key": bool(env.get("ANTHROPIC_API_KEY") or env.get("XAI_API_KEY")),
        "grok_key": bool(env.get("XAI_API_KEY")),
        "wallet_address": wallet_address(env),
        "okx_keys": bool(env.get("DEMO_API_KEY") and env.get("DEMO_API_SECRET") and env.get("DEMO_API_PASSWORD")),
    }


def brain_stats():
    """What the AI, smart-money and safety layers did in the last 24 hours (from the bot's log)."""
    cut = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 86400))
    kinds = {"holder": ("holds", "holders", "the dev still holds", "rugged"), "ai_veto": ("AI veto ",),
             "model_veto": ("AI model veto",), "ai_exit": ("AI exit:",), "smart_buy": ("[smart]",),
             "rug": ("RugCheck", "can still mint", "can freeze", "liquidity locked")}
    counts = {k: 0 for k in kinds}
    examples = {k: [] for k in kinds}
    for line in tail(BOT / "memebot.log", 20000):
        if line[:19] < cut:
            continue
        for k, keys in kinds.items():
            if any(x in line for x in keys) and not ("BUY" in line and k != "smart_buy"):
                if k == "holder" and not any(x in line for x in (": one wallet", ": top 10", ": only ", "dev still holds",
                                                                  "insider", "rugged")):
                    continue
                if k == "smart_buy" and "BUY" not in line:
                    continue
                counts[k] += 1
                if len(examples[k]) < 200:
                    examples[k].append(line[11:19] + "  " + line[21:].strip()[:150])
                break
    ai = jread("data/ai_verdicts.json", {}) or {}
    coins = sorted((ai.get("coins") or {}).values(), key=lambda v: -v.get("t", 0))
    sm = jread("data/smart_money.json", {}) or {}
    smart = sum(1 for w in (sm.get("wallets") or {}).values() if len(w.get("wins", {})) >= 2)
    st = jread("state.json", {}) or {}
    res = st.get("results", [])
    ai_trades = [r for r in res if r.get("strategy") == "smart"]
    return {"counts": counts, "examples": {k: v[-5:][::-1] for k, v in examples.items()},
            "ai_time": ai.get("time"), "ai_note": ai.get("note", ""),
            "ai_coins": [{k: v.get(k) for k in ("sym", "hype", "danger", "reason", "sell_now")} for v in coins[:12]],
            "smart_wallets": smart, "winners_studied": len(sm.get("done") or {}),
            "smart_trades": len(ai_trades), "smart_wins": sum(1 for r in ai_trades if r.get("pnl_sol", 0) > 0),
            "ai_key": bool(read_env().get("ANTHROPIC_API_KEY") or read_env().get("XAI_API_KEY"))}


def okx_snapshot():
    st = jread("market/state.json", {}) or {}
    pos = st.get("pos", {})
    cash = st.get("cash", 0) if not st.get("start_equity") else None
    hist = st.get("equity_history", [])
    eq = hist[-1][1] if hist else None
    res = st.get("results", [])
    quote = (jread("market/config.json", {}) or {}).get("quote", "USDT")
    return {"quote": quote, "equity": eq, "start": st.get("start_equity"), "open": len(pos), "closed": len(res),
            "wins": sum(1 for r in res if r.get("pnl", 0) > 0),
            "positions": [{"sym": k, "strategy": p.get("strategy"), "cost": p.get("cost"),
                           "value": p.get("qty", 0) * p.get("last", p.get("entry_px", 0))} for k, p in pos.items()],
            "trades": list(reversed(st.get("trades", [])[-100:])),
            "history": hist[-1440:], "blocked": sorted(st.get("blocked", {})),
            "pnl_closed": round(sum(r.get("pnl", 0) for r in res), 2)}


SETTINGS = ["paper_start_sol", "live_budget_sol", "risk_per_trade_pct", "max_positions", "max_position_pct",
            "max_position_sol", "max_daily_loss_sol", "max_drawdown_pct", "ntfy_topic", "edge_gate"]
NUMERIC = {"paper_start_sol", "live_budget_sol", "risk_per_trade_pct", "max_positions", "max_position_pct",
           "max_position_sol", "max_daily_loss_sol", "max_drawdown_pct"}


def reset_demo(usd):
    stop("bot")
    st = jread("state.json", {}) or {}
    px = sol_usd(st)
    if not px:
        try:
            import urllib.request
            with urllib.request.urlopen("https://lite-api.jup.ag/price/v3?ids=So11111111111111111111111111111111111111112", timeout=10) as r:
                px = float(json.loads(r.read())["So11111111111111111111111111111111111111112"]["usdPrice"])
        except Exception:
            px = 0
    if not px:
        return False, "couldn't get the SOL price — check the internet connection"
    cfg = jread("config.json", {})
    cfg["mode"] = "paper"
    cfg["paper_start_sol"] = round(usd / px, 4)
    cfg["max_daily_loss_sol"] = round(cfg["paper_start_sol"] * 0.05, 4)
    jwrite("config.json", cfg)
    (BOT / "archive").mkdir(exist_ok=True)
    if (BOT / "state.json").exists():
        (BOT / "state.json").replace(BOT / "archive" / f"state-{int(time.time())}.json")
    return start("bot")


# ============================================================================ http

PHONE_PORT = PORT + 1
_phone_srv = None


def phone_cfg():
    try:
        return json.loads((HOME / "phone.json").read_text())
    except Exception:
        return {}


def lan_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))           # no packet is sent: only picks the Wi-Fi/LAN interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def phone_url():
    p = phone_cfg()
    return f"http://{lan_ip()}:{PHONE_PORT}/?pin={p.get('pin', '')}" if p.get("enabled") else ""


def start_phone_server():
    """Second listener on the home network, for the phone. Every request needs the PIN."""
    global _phone_srv
    if _phone_srv or not phone_cfg().get("enabled"):
        return
    try:
        _phone_srv = ThreadingHTTPServer(("0.0.0.0", PHONE_PORT), H)
        threading.Thread(target=_phone_srv.serve_forever, daemon=True).start()
        print(f"  Phone access on: {phone_url()}")
    except OSError as e:
        print(f"  Phone access could not start: {e}")
        _phone_srv = None


def stop_phone_server():
    global _phone_srv
    if _phone_srv:
        threading.Thread(target=_phone_srv.shutdown, daemon=True).start()
        _phone_srv = None


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _local(self):
        return self.client_address[0] in ("127.0.0.1", "::1") and self.server.server_address[1] == PORT

    def _authorized(self):
        """The PC window is always allowed; the phone must carry the PIN (link or cookie)."""
        if self._local():
            return True
        pin = str(phone_cfg().get("pin") or "")
        if not pin or not phone_cfg().get("enabled"):
            return False
        q = parse_qs(urlparse(self.path).query).get("pin", [""])[0]
        ck = self.headers.get("Cookie", "")
        return q == pin or f"hcb_pin={pin}" in ck.replace(" ", "").split(";")

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        if not self._authorized():
            return self._send(403, b"<h2 style='font-family:sans-serif'>HatCryptoBot: open the phone link shown in the app "
                                   b"(Help &rarr; Phone app). It contains your PIN.</h2>", "text/html")
        if u.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if not self._local():
                self.send_header("Set-Cookie", f"hcb_pin={phone_cfg().get('pin')}; Path=/; Max-Age=31536000; SameSite=Strict")
            self.end_headers()
            ui = HOME / "ui.html" if (HOME / "ui.html").exists() and vtuple(installed_version()) >= vtuple(APP_VERSION) \
                else APP_DIR / "ui.html"
            self.wfile.write(ui.read_bytes())
            return
        if u.path == "/api/version":
            return self._send(200, {"app": APP_VERSION, "bot": installed_version(), **UPDATE_STATUS})
        if u.path == "/api/phone":
            p = phone_cfg()
            return self._send(200, {"enabled": bool(p.get("enabled")), "url": phone_url(), "running": bool(_phone_srv)})
        if u.path == "/api/state":
            try:
                return self._send(200, snapshot())
            except BaseException as e:
                import traceback
                try:
                    (HOME / "crash.log").write_text(traceback.format_exc(), encoding="utf-8")
                except Exception:
                    pass
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        if u.path == "/api/log":
            which = parse_qs(u.query).get("which", ["bot"])[0]
            f = {"bot": "memebot.log", "sniper": "sniper.log", "copy": "copytrade.log",
                 "okx": "market/marketbot.log"}.get(which, "memebot.log")
            return self._send(200, {"lines": tail(BOT / f, 300)})
        if u.path == "/api/brain":
            return self._send(200, brain_stats())
        if u.path == "/api/wallets":
            return self._send(200, jread("wallets.json", {"wallets": []}))
        if u.path == "/api/events":
            return self._send(200, jread("events.json", {"events": []}))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        # only accept requests from this app's own page (blocks other websites from driving the bot)
        origin = self.headers.get("Origin", "")
        if not self._authorized():
            return self._send(403, {"error": "forbidden"})
        allowed = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
        if not self._local():
            allowed.add(f"http://{self.headers.get('Host', '')}")
        if origin and origin not in allowed:
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        b = self._body()
        if u.path == "/api/update":
            threading.Thread(target=check_update, daemon=True).start()
            return self._send(200, {"ok": True, "msg": "checking for updates…"})
        if u.path == "/api/phone":
            if not self._local():
                return self._send(403, {"ok": False, "msg": "phone access can only be changed on the PC"})
            import secrets
            if b.get("enabled"):
                p = phone_cfg()
                p = {"enabled": True, "pin": p.get("pin") if b.get("keep_pin") and p.get("pin") else
                     "".join(secrets.choice("0123456789") for _ in range(8))}
                (HOME / "phone.json").write_text(json.dumps(p))
                stop_phone_server()
                time.sleep(0.3)
                start_phone_server()
                return self._send(200, {"ok": bool(_phone_srv), "url": phone_url(),
                                        "msg": "phone access on" if _phone_srv else "could not open the phone port"})
            (HOME / "phone.json").write_text(json.dumps({"enabled": False}))
            stop_phone_server()
            return self._send(200, {"ok": True, "url": "", "msg": "phone access off"})
        try:
            if u.path == "/api/test_order":
                env = read_env()
                if not (env.get("DEMO_API_KEY") and env.get("DEMO_API_SECRET") and env.get("DEMO_API_PASSWORD")):
                    return self._send(400, {"ok": False, "msg": "add your OKX demo API key, secret and passphrase in Wallet & keys first"})
                mc = jread("market/config.json", {})
                mc["mode"] = "demo"
                jwrite("market/config.json", mc)
                r = subprocess.run([sys.executable, SCRIPTS["okx"], "--test-order"], cwd=BOT, capture_output=True,
                                   text=True, timeout=90, env=dict(os.environ, PYTHONUTF8="1"), creationflags=NO_WINDOW)
                lines = [l for l in (r.stdout or "").splitlines() if l.strip()]
                if r.returncode and not lines:
                    lines = [l for l in (r.stderr or "").splitlines() if l.strip()][-1:] or ["test order failed"]
                return self._send(200, {"ok": r.returncode == 0, "lines": lines})
            if u.path == "/api/start" and b.get("name") == "okx":
                env = read_env()
                if not (env.get("DEMO_API_KEY") and env.get("DEMO_API_SECRET") and env.get("DEMO_API_PASSWORD")):
                    return self._send(400, {"ok": False, "msg": "add your OKX demo API key, secret and passphrase in Wallet & keys first"})
                mc = jread("market/config.json", {})
                mc["mode"] = "demo"
                mc["ntfy_topic"] = (jread("config.json", {}) or {}).get("ntfy_topic", "")
                jwrite("market/config.json", mc)
                ok, msg = start("okx")
                return self._send(200, {"ok": ok, "msg": msg if ok else "OKX: " + msg})
            if u.path == "/api/start":
                name = b.get("name", "bot")
                live = b.get("live", False)
                cfg = jread("config.json", {})
                if live:
                    if b.get("confirm") != "YES":
                        return self._send(400, {"ok": False, "msg": "type YES to confirm real money"})
                    env = read_env()
                    if not parse_keypair(env.get("SOLANA_PRIVATE_KEY")):
                        return self._send(400, {"ok": False, "msg": "no valid bot wallet — use Create bot wallet in Wallet & keys"})
                    if not env.get("SOLANA_PRIVATE_KEY") or not env.get("JUPITER_API_KEY"):
                        return self._send(400, {"ok": False, "msg": "add your wallet key and Jupiter key in Wallet & keys first"})
                    if not cfg.get("live_budget_sol"):
                        return self._send(400, {"ok": False, "msg": "set a live budget in Settings first"})
                    cfg["mode"] = "live"
                    if name == "sniper":
                        cfg.setdefault("sniper", {}).update(mode="live", live_budget_sol=b.get("budget") or cfg.get("live_budget_sol"))
                    if name == "copy":
                        cfg.setdefault("copytrade", {}).update(mode="live", live_budget_sol=b.get("budget") or cfg.get("live_budget_sol"))
                else:
                    if name == "bot":
                        cfg["mode"] = "paper"
                    if name == "sniper":
                        cfg.setdefault("sniper", {})["mode"] = "paper"
                    if name == "copy":
                        cfg.setdefault("copytrade", {})["mode"] = "paper"
                jwrite("config.json", cfg)
                ok, msg = start(name, live)
                return self._send(200, {"ok": ok, "msg": msg})
            if u.path == "/api/stop":
                ok, msg = stop(b.get("name", "bot"))
                return self._send(200, {"ok": ok, "msg": msg})
            if u.path == "/api/reset_demo":
                ok, msg = reset_demo(float(b.get("usd", 1000)))
                return self._send(200, {"ok": ok, "msg": msg})
            if u.path == "/api/settings":
                cfg = jread("config.json", {})
                for k, v in (b or {}).items():
                    if k in SETTINGS:
                        cfg[k] = float(v) if k in NUMERIC and v not in ("", None) else (v == "true" or v is True) if k == "edge_gate" else v
                if "max_positions" in b:
                    cfg["max_positions"] = int(float(b["max_positions"]))
                jwrite("config.json", cfg)
                return self._send(200, {"ok": True, "msg": "saved — restart the bot to apply"})
            if u.path == "/api/new_wallet":
                env = read_env()
                if parse_keypair(env.get("SOLANA_PRIVATE_KEY")) and b.get("confirm") != "REPLACE":
                    return self._send(400, {"ok": False, "msg": "a wallet is already set — type REPLACE to make a new one "
                                                                 "(move its money out first)"})
                from solders.keypair import Keypair
                kp = Keypair()
                key = str(kp)                      # base58 secret, the format Phantom's "Import Private Key" accepts
                addr = str(kp.pubkey())
                # backup next to the bot, so the money is never stuck if the app is reinstalled
                stamp = time.strftime("%Y%m%d-%H%M%S")
                (BOT / f"wallet-backup-{stamp}.txt").write_text(
                    "HatCryptoBot bot wallet — keep this file private.\n"
                    f"Address (send SOL here): {addr}\nPrivate key (Phantom > Add account > Import Private Key): {key}\n",
                    encoding="utf-8")
                write_env({"SOLANA_PRIVATE_KEY": key})
                return self._send(200, {"ok": True, "address": addr, "msg": "bot wallet created"})
            if u.path == "/api/show_wallet_key":
                if b.get("confirm") != "SHOW":
                    return self._send(400, {"ok": False, "msg": "type SHOW to confirm"})
                return self._send(200, {"ok": True, "key": read_env().get("SOLANA_PRIVATE_KEY", "")})
            if u.path == "/api/keys":
                if b.get("wallet") and not parse_keypair(b["wallet"]):
                    return self._send(400, {"ok": False, "msg": "that is not a Solana private key — nothing saved. "
                                                                 "(The Jupiter key goes in the Jupiter box.)"})
                write_env({"SOLANA_PRIVATE_KEY": b.get("wallet"), "JUPITER_API_KEY": b.get("jupiter"), "RPC_URL": b.get("rpc"),
                           "REDDIT_CLIENT_ID": b.get("reddit_id"), "REDDIT_CLIENT_SECRET": b.get("reddit_secret"),
                           "X_BEARER_TOKEN": b.get("x_token"), "CRYPTOPANIC_KEY": b.get("cryptopanic"),
                           "ANTHROPIC_API_KEY": b.get("anthropic"), "XAI_API_KEY": b.get("xai"),
                           "DEMO_API_KEY": b.get("okx_key"), "DEMO_API_SECRET": b.get("okx_secret"),
                           "DEMO_API_PASSWORD": b.get("okx_pass")})
                return self._send(200, {"ok": True, "msg": "keys saved on this PC only"})
            if u.path == "/api/wallets":
                jwrite("wallets.json", {"wallets": b.get("wallets", [])})
                return self._send(200, {"ok": True, "msg": "wallets saved"})
            if u.path == "/api/events":
                jwrite("events.json", {"events": b.get("events", [])})
                return self._send(200, {"ok": True, "msg": "events saved"})
            if u.path == "/api/quit":
                for n in list(PROCS):
                    stop(n)
                threading.Timer(0.5, lambda: os._exit(0)).start()
                return self._send(200, {"ok": True, "msg": "HatCryptoBot closed — the bot is stopped"})
            if u.path == "/api/open_folder":
                if os.name == "nt":
                    os.startfile(BOT)  # noqa
                return self._send(200, {"ok": True, "msg": str(BOT)})
        except Exception as e:
            return self._send(500, {"ok": False, "msg": f"{type(e).__name__}: {e}"})
        return self._send(404, {"error": "not found"})


def open_window(url):
    """Open HatCryptoBot as its own app window (no tabs, no address bar) using Edge or Chrome's app mode.
    Falls back to a normal browser tab if neither is found."""
    if os.name == "nt":
        pf = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        candidates = [os.path.join(p, r"Microsoft\Edge\Application\msedge.exe") for p in pf] + \
                     [os.path.join(p, r"Google\Chrome\Application\chrome.exe") for p in pf]
        profile = str(HOME / "window")
        for exe in candidates:
            if exe and os.path.exists(exe):
                try:
                    subprocess.Popen([exe, f"--app={url}", "--window-size=1300,860", f"--user-data-dir={profile}",
                                      "--no-first-run", "--no-default-browser-check"], creationflags=NO_WINDOW)
                    return
                except Exception:
                    pass
    webbrowser.open(url)


def main():
    install_bot()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError:
        # already open: show the existing window — unless that copy is frozen/half-closed, then replace it
        if port_answers():
            open_window(f"http://127.0.0.1:{PORT}/")
            return
        print("  An old HatCryptoBot is stuck on the port — closing it.")
        free_port()
        time.sleep(2)
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    kill_leftovers()
    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGBREAK", None)):
        if sig:
            signal.signal(sig, lambda *_: (_shutdown(), os._exit(0)))
    url = f"http://127.0.0.1:{PORT}/"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    start_phone_server()
    threading.Thread(target=update_loop, daemon=True).start()
    if not os.environ.get("HCB_NO_BROWSER"):
        threading.Timer(0.8, lambda: open_window(url)).start()
    print(f"  Running. Bot controls are in the browser window ({url}).")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown()
        srv.shutdown()


def _shutdown():
    for n in list(PROCS):
        stop(n)


def run():
    """Entry point for the installed app: always show a status window, and keep errors on screen."""
    newer = HOME / "app" / "hatcryptobot.py"
    if newer.exists() and Path(__file__).resolve() != newer.resolve() and not os.environ.get("HCB_NO_SHIM"):
        try:
            import re
            m = re.search(r'^APP_VERSION = "([\d.]+)"', newer.read_text(encoding="utf-8"), re.M)
            if m and vtuple(m.group(1)) > vtuple(APP_VERSION):
                import importlib.util
                os.environ["HCB_NO_SHIM"] = "1"
                spec = importlib.util.spec_from_file_location("hatcryptobot_updated", newer)
                mod = importlib.util.module_from_spec(spec)
                mod.__dict__["__file__"] = str(newer)
                spec.loader.exec_module(mod)
                mod.APP_DIR = APP_DIR                   # bundled files (icons, bot copy) stay where the installer put them
                mod.SRC_BOT = SRC_BOT
                return mod.run()
        except Exception as e:
            print(f"  updated app could not start ({e}) — using the installed version")
    try:
        print("=" * 60)
        print("  HatCryptoBot")
        print("=" * 60)
        print(f"  Files: {BOT}")
        print(f"  Opening the HatCryptoBot window (address: http://127.0.0.1:{PORT}/)")
        print("  If no window opens, copy that address into Chrome or Edge.")
        print("  KEEP THIS WINDOW OPEN while the bot trades. Close it to stop.")
        print("=" * 60)
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        try:
            (HOME / "crash.log").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        print("\nSomething went wrong. Send a screenshot of this window.")
        input("Press Enter to close...")


if __name__ == "__main__":
    run()
