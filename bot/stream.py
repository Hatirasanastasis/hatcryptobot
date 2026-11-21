#!/usr/bin/env python3
"""
Live-stream the meme bot to YouTube.

Draws a 1280x720 dashboard from the bot's state.json / memebot.log twice a
second and pushes it to YouTube Live over RTMPS with ffmpeg. Run it next to
memebot.py (same folder) — it only reads the bot's files, never trades.

  python stream.py             go live (needs YOUTUBE_STREAM_KEY in .env)
  python stream.py --preview   write preview.png, no streaming
  python stream.py --record 20 write a 20-second test.mp4 instead of streaming

Needs ffmpeg on PATH and Pillow (pip install pillow).
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))  # bundled Python does not add the script folder
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
LOG = HERE / "memebot.log"
CONFIG = HERE / "config.json"

W, H, FPS_IN = 1280, 720, 2
YT_URL = "rtmps://a.rtmps.youtube.com:443/live2/"

BG = (14, 18, 26)
PANEL = (23, 30, 42)
LINE = (40, 52, 70)
FG = (228, 234, 242)
MUTED = (130, 146, 166)
ACCENT = (120, 150, 255)
UP = (52, 200, 140)
DOWN = (240, 100, 100)
WARN = (230, 170, 70)


def font(size, mono=False, bold=False):
    names = (["DejaVuSansMono-Bold.ttf", "consolab.ttf", "Menlo.ttc"] if mono and bold else
             ["DejaVuSansMono.ttf", "consola.ttf", "Menlo.ttc"] if mono else
             ["DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf"] if bold else
             ["DejaVuSans.ttf", "arial.ttf", "Arial.ttf"])
    dirs = ["/usr/share/fonts/truetype/dejavu/", "C:/Windows/Fonts/", "/System/Library/Fonts/",
            "/Library/Fonts/", "/System/Library/Fonts/Supplemental/", ""]
    for n in names:
        for d in dirs:
            try:
                return ImageFont.truetype(d + n, size)
            except OSError:
                continue
    return ImageFont.load_default(size)


F = {
    "title": font(30, bold=True), "big": font(44, mono=True, bold=True), "mid": font(22, mono=True, bold=True),
    "label": font(14, bold=True), "row": font(17, mono=True), "small": font(14, mono=True), "tiny": font(12),
}


def load_json(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def tail_log(n=200):
    try:
        with open(LOG, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 40_000))
            return f.read().decode(errors="replace").splitlines()[-n:]
    except OSError:
        return []


def panel(d, x, y, w, h, label=None):
    d.rounded_rectangle([x, y, x + w, y + h], 10, fill=PANEL, outline=LINE)
    if label:
        d.text((x + 16, y + 12), label.upper(), font=F["label"], fill=MUTED)


def fit(d, text, fnt, maxw):
    if d.textlength(text, font=fnt) <= maxw:
        return text
    while text and d.textlength(text + "…", font=fnt) > maxw:
        text = text[:-1]
    return text + "…"


def render(cfg) -> Image.Image:
    st = load_json(STATE, {})
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    now = datetime.now(timezone.utc)

    # header
    d.text((28, 22), "LUNYX MEME BOT", font=F["title"], fill=FG)
    mode = cfg.get("mode", "paper").upper()
    tag = f" {mode} TRADING "
    tw = d.textlength(tag, font=F["label"])
    bx = 28 + d.textlength("LUNYX MEME BOT", font=F["title"]) + 18
    d.rounded_rectangle([bx, 28, bx + tw + 8, 56], 6, fill=WARN if mode == "PAPER" else DOWN)
    d.text((bx + 4, 34), tag, font=F["label"], fill=BG)
    alive = LOG.exists() and time.time() - LOG.stat().st_mtime < 180
    d.ellipse([W - 250, 34, W - 236, 48], fill=UP if alive else DOWN)
    d.text((W - 228, 30), ("LIVE " if alive else "BOT IDLE ") + now.strftime("%H:%M:%S UTC"), font=F["small"], fill=FG)
    d.text((28, 62), "Solana meme coins · DexScreener + RugCheck filters · not financial advice", font=F["tiny"], fill=MUTED)

    # stats
    hist = st.get("equity_history") or []
    positions = st.get("positions") or {}
    sol_free = st.get("sol_free", st.get("paper_sol", 0.0))
    eq = sol_free + sum(p.get("last_value_sol", p.get("cost_sol", 0)) for p in positions.values())
    start = st.get("start_equity") or eq or 1
    pnl = eq - start
    sells = [t for t in st.get("trades", []) if t["side"] == "sell"]
    wins = sum(1 for t in sells if (t.get("pnl_sol") or 0) > 0)

    y = 90
    cards = [("Balance", f"{eq:.4f} SOL", FG), ("Profit / loss", f"{pnl:+.4f}", UP if pnl >= 0 else DOWN),
             ("Return", f"{(eq / start - 1) * 100:+.1f}%", UP if pnl >= 0 else DOWN),
             ("Win rate", f"{wins}/{len(sells)}" if sells else "—", FG)]
    cw = (W - 56 - 3 * 16) / 4
    for i, (k, v, col) in enumerate(cards):
        x = 28 + i * (cw + 16)
        panel(d, x, y, cw, 96, k)
        d.text((x + 16, y + 36), v, font=F["big"] if len(v) < 12 else F["mid"], fill=col)

    # equity chart
    cx, cy, cwid, chei = 28, 202, 760, 250
    panel(d, cx, cy, cwid, chei, "Balance over time (SOL)")
    pts = [v for _, v in hist][-720:]
    if len(pts) >= 2:
        lo, hi = min(pts + [start]), max(pts + [start])
        if hi - lo < 1e-6:
            hi, lo = hi + 0.001, lo - 0.001
        x0, x1, y0, y1 = cx + 16, cx + cwid - 70, cy + 40, cy + chei - 16
        X = lambda i: x0 + (x1 - x0) * i / (len(pts) - 1)
        Y = lambda v: y1 - (y1 - y0) * (v - lo) / (hi - lo)
        for k in range(5):
            v = lo + (hi - lo) * k / 4
            d.line([x0, Y(v), x1, Y(v)], fill=LINE)
            d.text((x1 + 8, Y(v) - 7), f"{v:.3f}", font=F["tiny"], fill=MUTED)
        ys = Y(start)
        for xx in range(int(x0), int(x1), 10):
            d.line([xx, ys, xx + 5, ys], fill=MUTED)
        col = UP if pts[-1] >= start else DOWN
        line = [(X(i), Y(v)) for i, v in enumerate(pts)]
        d.polygon(line + [(x1, y1), (x0, y1)], fill=tuple(int(c * 0.25 + b * 0.75) for c, b in zip(col, PANEL)))
        d.line(line, fill=col, width=3)
        d.ellipse([line[-1][0] - 5, line[-1][1] - 5, line[-1][0] + 5, line[-1][1] + 5], fill=col)
    else:
        d.text((cx + 16, cy + 120), "Collecting data — first point after a minute.", font=F["row"], fill=MUTED)

    # positions
    px, pw = 804, W - 804 - 28
    panel(d, px, cy, pw, chei, f"Open positions ({len(positions)}/{cfg.get('max_positions', 3)})")
    if positions:
        yy = cy + 44
        for p in list(positions.values())[:5]:
            cost, val = p.get("cost_sol", 0), p.get("last_value_sol", p.get("cost_sol", 0))
            ch = (val / cost - 1) * 100 if cost else 0
            d.text((px + 16, yy), fit(d, p["symbol"], F["mid"], 170), font=F["mid"], fill=FG)
            d.text((px + pw - 16, yy), f"{ch:+.1f}%", font=F["mid"], fill=UP if ch >= 0 else DOWN, anchor="ra")
            d.text((px + 16, yy + 28), f"in {cost:.4f} → {val:.4f} SOL", font=F["small"], fill=MUTED)
            yy += 60
    else:
        d.text((px + 16, cy + 60), "Flat — waiting for a coin", font=F["row"], fill=MUTED)
        d.text((px + 16, cy + 86), "that passes every filter.", font=F["row"], fill=MUTED)

    # trades feed
    ty, th = 468, H - 468 - 20
    panel(d, 28, ty, 760, th, "Trades")
    yy = ty + 40
    trades = st.get("trades", [])[-8:][::-1]
    if not trades:
        d.text((44, yy + 10), "No trades yet.", font=F["row"], fill=MUTED)
    for t in trades:
        tm = t["time"][11:16]
        side = t["side"].upper()
        col = UP if side == "BUY" else (UP if (t.get("pnl_sol") or 0) > 0 else DOWN)
        d.text((44, yy), tm, font=F["small"], fill=MUTED)
        d.text((100, yy), side, font=F["small"], fill=col)
        d.text((150, yy), fit(d, t["symbol"], F["small"], 110), font=F["small"], fill=FG)
        right = f"{t['sol']:.4f} SOL" + (f"  {t['pnl_sol']:+.4f}" if t.get("pnl_sol") is not None else "")
        d.text((772, yy), right, font=F["small"], fill=col, anchor="ra")
        reason = t.get("reason", "")
        if side == "BUY":
            reason = reason.split(": ", 1)[-1]
        d.text((270, yy), fit(d, reason, F["small"], 772 - 270 - d.textlength(right, font=F["small"]) - 16),
               font=F["small"], fill=MUTED)
        yy += 26

    # scanner / log
    panel(d, px, ty, pw, th, "Scanner")
    ls = st.get("last_scan") or {}
    if ls:
        d.text((px + 16, ty + 40), f"{ls.get('seen', 0)} coins checked at {ls.get('time', '')[11:19]}",
               font=F["small"], fill=FG)
        passed = ls.get("passed") or []
        d.text((px + 16, ty + 62), "Passed: " + (", ".join(passed) if passed else "none"), font=F["small"],
               fill=UP if passed else MUTED)
    rej = [l.split("  ", 1)[-1] for l in tail_log() if ":" in l and any(
        w in l for w in ("too low", "out of range", "dev can", "RugCheck", "weak", "dumping"))][-6:]
    yy = ty + 94
    for r in rej[::-1]:
        d.text((px + 16, yy), "✕ " + fit(d, r, F["tiny"], pw - 48), font=F["tiny"], fill=MUTED)
        yy += 20
    if st.get("killed"):
        d.text((px + 16, ty + th - 30), "STOPPED: max drawdown hit", font=F["label"], fill=DOWN)
    elif st.get("halted_until"):
        d.text((px + 16, ty + th - 30), f"Daily loss limit — paused until {st['halted_until']}", font=F["label"], fill=WARN)
    return img


def load_env():
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def ffmpeg_cmd(out, fmt):
    return ["ffmpeg", "-loglevel", "warning", "-re",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS_IN), "-i", "-",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-vf", "fps=30", "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-g", "60", "-b:v", "2500k", "-maxrate", "2500k", "-bufsize", "5000k",
            "-c:a", "aac", "-b:a", "128k", "-shortest", "-f", fmt, out]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--record", type=int, metavar="SECONDS")
    a = ap.parse_args()
    cfg = load_json(CONFIG, {})

    if a.preview:
        render(cfg).save(HERE / "preview.png")
        print("wrote preview.png")
        return

    if a.record:
        out, fmt, frames = str(HERE / "test.mp4"), "mp4", a.record * FPS_IN
    else:
        load_env()
        key = os.getenv("YOUTUBE_STREAM_KEY")
        if not key:
            sys.exit("Put YOUTUBE_STREAM_KEY=... in .env (YouTube Studio → Go live → Stream → Stream key)")
        out, fmt, frames = YT_URL + key, "flv", None
        print("going live on YouTube — Ctrl+C to stop")

    while True:
        proc = subprocess.Popen(ffmpeg_cmd(out, fmt), stdin=subprocess.PIPE)
        n = 0
        try:
            while frames is None or n < frames:
                t = time.time()
                cfg = load_json(CONFIG, cfg)
                proc.stdin.write(render(cfg).tobytes())
                n += 1
                time.sleep(max(0, 1 / FPS_IN - (time.time() - t)))
            proc.stdin.close()
            proc.wait()
            print("wrote test.mp4")
            return
        except KeyboardInterrupt:
            proc.stdin.close()
            proc.wait()
            return
        except BrokenPipeError:
            print("stream dropped — reconnecting in 10 s")
            proc.kill()
            time.sleep(10)


if __name__ == "__main__":
    main()
