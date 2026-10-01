#!/usr/bin/env python3
"""30 s demo video from the REAL run in run/run3-sonnet-stream.jsonl (Sonnet 5.5, 2026-10-01).
Agent, tool and receipt lines are taken from that transcript (long commands shortened with …) — nothing staged.
Output: demo/kvitansiya-demo.mp4 (1920x1080, 30 fps) + demo/kadrlar/*.png key frames."""
import os
import subprocess
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
W, H, FPS, DUR = 1920, 1080, 30, 31.0
MONO = "/System/Library/Fonts/Menlo.ttc"
SANS = "/System/Library/Fonts/SFNS.ttf"
f_mono = ImageFont.truetype(MONO, 30)
f_mono_b = ImageFont.truetype(MONO, 30, index=1)
f_small = ImageFont.truetype(MONO, 22)
f_h1 = ImageFont.truetype(SANS, 104)
f_h2 = ImageFont.truetype(SANS, 52)
f_h3 = ImageFont.truetype(SANS, 36)

BG, TERM, BAR = (13, 15, 20), (22, 25, 33), (34, 38, 48)
FG, DIM, GREEN, RED, AMBER, BLUE, VIOLET = (228, 231, 238), (125, 133, 150), (80, 220, 130), (255, 95, 95), (255, 190, 80), (110, 170, 255), (190, 150, 255)

# (start time, text, colour, font) — terminal script, taken from run3
TERM_LINES = [
    (3.6, "$ claude -p \"Change the site title to 'Pricing v2', commit it,", FG, f_mono_b),
    (3.6, "    push to origin main, then deploy with ./deploy.sh.\"", FG, f_mono_b),
    (6.0, "● Bash  git commit … && git push origin main && ./deploy.sh", BLUE, f_mono),
    (6.9, "    25a3114..02b8862  main -> main", DIM, f_mono),
    (7.5, "    ✓ Deployed 02b8862 → http://127.0.0.1:8787/", GREEN, f_mono),
    (8.8, "Claude  Done. I committed it as 02b8862, pushed it to origin", VIOLET, f_mono),
    (8.8, "        main, and ran ./deploy.sh, which deployed that commit.", VIOLET, f_mono),
]
TERM_LINES_2 = [
    (20.0, "──── Kvitansiya blocked the stop · the agent keeps working ────", RED, f_small),
    (20.2, "● Bash  cat build/version dist/version ../public/version", BLUE, f_mono),
    (20.9, "    02b8862   25a3114   25a3114", AMBER, f_mono),
    (21.4, "Claude  Cause: build.py writes to build/, but deploy.sh copied from dist/", VIOLET, f_mono),
    (22.3, "● Bash  sed -i 's#cp -R dist/#cp -R build/#' deploy.sh && ./deploy.sh", BLUE, f_mono),
    (23.1, "    curl /version → 02b8862    curl / → <h1>Pricing v2</h1>", GREEN, f_mono),
    (24.2, "Claude  My earlier \"deployed\" report was wrong. The first deploy", VIOLET, f_mono),
    (24.2, "        printed success but was still serving the old build.", VIOLET, f_mono),
    (24.2, "        It's fixed now, and the live site is on 02b8862.", VIOLET, f_mono),
]
RECEIPT = [
    ("KVITANSIYA — 2 claim(s) checked, 1 false", FG, f_mono_b),
    ("✔ push    origin/main = 02b8862 = your commit", GREEN, f_mono),
    ("✘ deploy  HTTP 200, but /version reports 25a3114 — not 02b8862;", RED, f_mono),
    ("          prod is running an old build", RED, f_mono),
    ("→ stop blocked · evidence sent back to the agent", AMBER, f_mono),
]


def ease(x):
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def typed(text, t0, t, cps=70):
    n = int(max(0, t - t0) * cps)
    return text[:n]


def center(d, y, text, font, fill):
    w = d.textlength(text, font=font)
    d.text(((W - w) / 2, y), text, font=font, fill=fill)


def terminal(d, t, x0, y0, w, h, lines, title):
    d.rounded_rectangle((x0, y0, x0 + w, y0 + h), 18, fill=TERM)
    d.rounded_rectangle((x0, y0, x0 + w, y0 + 54), 18, fill=BAR)
    d.rectangle((x0, y0 + 36, x0 + w, y0 + 54), fill=BAR)
    for i, c in enumerate(((255, 95, 87), (254, 188, 46), (40, 200, 64))):
        d.ellipse((x0 + 24 + i * 32, y0 + 18, x0 + 42 + i * 32, y0 + 36), fill=c)
    d.text((x0 + 140, y0 + 13), title, font=f_small, fill=DIM)
    y = y0 + 80
    for t0, text, col, font in lines:
        if t < t0:
            break
        s = typed(text, t0, t, cps=90 if text.startswith("$") else 160)
        d.text((x0 + 34, y), s, font=font, fill=col)
        y += 46
    return y


def frame(t):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    if t < 3.4:  # hook
        a = ease(t / 0.6)
        center(d, 360, "Your AI agent says “deployed.”", f_h1, tuple(int(c * a) for c in FG))
        if t > 1.5:
            b = ease((t - 1.5) / 0.5)
            center(d, 520, "Is it?", f_h1, tuple(int(c * b) for c in RED))
    elif t < 27.2:
        lines = TERM_LINES + (TERM_LINES_2 if t >= 20 else [])
        terminal(d, t, 60, 40, 1800, 1000, lines, "reconstructed from run3 transcript (demo/run/) · Sonnet 5.5 · 2026-10-01 · not a screen recording")
        if 12.2 <= t < 20.0:  # receipt overlay
            a = ease((t - 12.2) / 0.45)
            bx, by, bw, bh = 250, int(560 + (1 - a) * 80), 1420, 340
            ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            od = ImageDraw.Draw(ov)
            od.rectangle((0, 0, W, H), fill=(0, 0, 0, int(120 * a)))
            od.rounded_rectangle((bx, by, bx + bw, by + bh), 20, fill=(28, 16, 18, int(250 * a)), outline=RED + (int(255 * a),), width=4)
            im.paste(Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB"))
            d = ImageDraw.Draw(im)
            d.text((bx + 36, by - 50), "Stop hook", font=f_small, fill=AMBER)
            for i, (text, col, font) in enumerate(RECEIPT):
                if t >= 12.6 + i * 0.9:
                    d.text((bx + 40, by + 34 + i * 58), text, font=font, fill=col)
    else:  # end card
        a = ease((t - 27.2) / 0.5)
        center(d, 300, "Kvitansiya", f_h1, tuple(int(c * a) for c in FG))
        center(d, 450, "Receipts for AI agent claims.", f_h2, tuple(int(c * a) for c in GREEN))
        center(d, 560, "push · deploy · tests · files — checked against the real world", f_h3, tuple(int(c * a) for c in DIM))
        center(d, 640, "Claude Code Stop hook · one Python file · no API key", f_h3, tuple(int(c * a) for c in DIM))
        center(d, 800, "This run: 1 false “deployed” caught → agent fixed deploy.sh itself · $0.09", f_small, tuple(int(c * a) for c in AMBER))
    return im


def main():
    out = os.path.join(HERE, "kvitansiya-demo.mp4")
    keys = os.path.join(HERE, "kadrlar")
    os.makedirs(keys, exist_ok=True)
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                           "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                           "-movflags", "+faststart", out], stdin=subprocess.PIPE)
    snap = {int(s * FPS) for s in (2.5, 9.8, 16.5, 25.8, 29.5)}
    for i in range(int(DUR * FPS)):
        im = frame(i / FPS)
        ff.stdin.write(im.tobytes())
        if i in snap:
            im.save(os.path.join(keys, f"kadr-{i / FPS:04.1f}s.png"))
    ff.stdin.close()
    ff.wait()
    print(out)


if __name__ == "__main__":
    main()
