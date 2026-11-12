"""
SIGNATURE CHECK, ALL THREE INPUT MODES (2026-10-17) -- upload / draw / capture, positive and negative.

    python -m evals.signature.modes [--split dev|heldout] [--misses]

UPLOAD   a signature on paper, scanned or photographed flat: white / cream paper, black / blue pen, JPEG
DRAW     signed on a screen canvas: PNG, TRANSPARENT or white background, any ink colour (white ink on a dark-mode
         canvas too), thin to thick strokes; also sent as a base64 data URL
CAPTURE  a camera photo of a signature on paper: uneven light / shadow, colour cast, a few degrees of tilt, sensor
         noise, JPEG

Each mode has its NEGATIVES: blank (incl. a shadowed blank page, an empty transparent canvas), a dot, one straight
line, a typed name, blurred / motion-blurred, cut off at the frame, another document (a printed page).
Positives must be VERIFIED (a MANUAL_REVIEW counts as a miss, a REJECTED as a false rejection); negatives must never
be VERIFIED (REJECTED expected for the clear ones). Made-up names; DEV and HELDOUT use different seeds and hands.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import random
import time
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONTS = Path("C:/Windows/Fonts")
HANDS = {"dev": ["segoesc.ttf", "Inkfree.ttf", "LHANDW.TTF", "BRADHITC.TTF", "FREESCPT.TTF"],
         "heldout": ["MISTRAL.TTF", "RAGE.TTF", "KUNSTLER.TTF", "BRUSHSCI.TTF", "Gabriola.ttf", "FRSCRIPT.TTF"]}
NAMES = {"dev": ["Rahul Sharma", "Priya Patil", "Amit K", "Sneha V", "Vikas Rao", "Pooja"],
         "heldout": ["Kiran Joshi", "Sunita P", "Arjun M", "Deepa Nair", "Nitin S", "Asha"]}
INKS = [(20, 20, 30), (25, 40, 140), (10, 10, 10), (40, 30, 110)]


# ------------------------------------------------------------------------------------------------ strokes
def _font(rng, split, size):
    names = [f for f in HANDS[split] if (FONTS / f).is_file()]
    return ImageFont.truetype(str(FONTS / rng.choice(names)), size)


def written(rng, split, size=(900, 320), ink=(20, 20, 30), bg=(255, 255, 255, 255), mode="RGBA", margin=0.12):
    """A handwritten-font signature, slanted, on a canvas with a margin all round."""
    w, h = size
    layer = Image.new("RGBA", (w * 2, h * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    font = _font(rng, split, int(h * rng.uniform(0.55, 0.8)))
    text = rng.choice(NAMES[split])
    d.text((w // 2, h // 2), text, font=font, fill=(*ink, 255))
    # a flourish under the name, as people sign
    bbox = layer.getbbox()
    if bbox and rng.random() < 0.7:
        y = bbox[3] + 4
        d.line([(bbox[0] + 10, y), (bbox[2] - rng.randint(0, 60), y - rng.randint(-8, 8))], fill=(*ink, 255),
               width=max(2, h // 90))
    layer = layer.rotate(rng.uniform(-6, 6), resample=Image.BICUBIC, expand=False)
    layer = layer.crop(layer.getbbox())
    scale = min((w * (1 - 2 * margin)) / layer.width, (h * (1 - 2 * margin)) / layer.height, 1.0)
    layer = layer.resize((max(1, int(layer.width * scale)), max(1, int(layer.height * scale))), Image.LANCZOS)
    canvas = Image.new("RGBA", (w, h), bg)
    canvas.alpha_composite(layer, ((w - layer.width) // 2, (h - layer.height) // 2))
    return canvas if mode == "RGBA" else canvas.convert(mode)


def scribble(rng, size=(800, 260), ink=(20, 20, 30, 255), bg=(0, 0, 0, 0), width=4, margin=0.12):
    """A drawn signature: a few smooth strokes with loops, as a finger / stylus on a canvas makes."""
    w, h = size
    im = Image.new("RGBA", size, bg)
    d = ImageDraw.Draw(im)
    x0, x1 = w * margin, w * (1 - margin)
    for stroke in range(rng.randint(2, 3)):
        pts = []
        steps = 140
        amp = h * rng.uniform(0.18, 0.3)
        f1, f2 = rng.uniform(2, 5), rng.uniform(6, 11)
        start, end = x0 + stroke * (x1 - x0) * 0.08, x1 - rng.uniform(0, 0.25) * (x1 - x0)
        cy = h * rng.uniform(0.42, 0.58)
        for i in range(steps):
            t = i / (steps - 1)
            x = start + (end - start) * t + math.sin(t * f2 * math.pi) * w * 0.02
            y = cy + amp * math.sin(t * f1 * math.pi) * (0.6 + 0.4 * math.cos(t * f2))
            pts.append((x, min(h * (1 - margin), max(h * margin, y))))
        d.line(pts, fill=ink, width=width, joint="curve")
    return im


def _jpeg(im, q):
    b = io.BytesIO()
    im.convert("RGB").save(b, "JPEG", quality=q)
    return b.getvalue()


def _png(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def _flatten(im, bg=(255, 255, 255)):
    base = Image.new("RGBA", im.size, (*bg, 255))
    return Image.alpha_composite(base, im.convert("RGBA")).convert("RGB")


# ------------------------------------------------------------------------------------------------ capture effects
def camera(im_rgb, rng):
    """A phone photo of the paper: a light falloff / shadow, a warm or cool cast, a little tilt, noise, JPEG."""
    import numpy as np

    w, h = im_rgb.size
    a = np.asarray(im_rgb, dtype="float64")
    yy, xx = np.mgrid[0:h, 0:w]
    angle = rng.uniform(0, 2 * math.pi)
    grad = (np.cos(angle) * (xx / w - 0.5) + np.sin(angle) * (yy / h - 0.5))
    light = 1.0 - rng.uniform(0.15, 0.35) * (grad + 0.5)
    cast = np.array([rng.uniform(0.92, 1.0), rng.uniform(0.9, 0.98), rng.uniform(0.82, 0.95)])
    a = a * light[..., None] * cast
    a += rng.gauss(0, 1) * 0 + np.random.default_rng(rng.randint(0, 9999)).normal(0, rng.uniform(3, 7), a.shape)
    out = Image.fromarray(np.clip(a, 0, 255).astype("uint8"))
    tilt = rng.uniform(-5, 5)
    fill = tuple(int(v) for v in np.asarray(out).reshape(-1, 3).mean(axis=0))
    out = out.rotate(tilt, resample=Image.BICUBIC, expand=False, fillcolor=fill)
    return _jpeg(out, rng.randint(70, 88))


# ------------------------------------------------------------------------------------------------ cases
def cases(split: str):
    rng = random.Random({"dev": 1717, "heldout": 9191}[split])
    out = []

    def add(mode, label, data, expect, *, data_url=False, name="sig.png"):
        out.append({"mode": mode, "label": label, "data": data, "expect": expect, "data_url": data_url,
                    "name": name})

    for i in range(10):
        # UPLOAD ------------------------------------------------------------------------------------------
        paper = rng.choice([(255, 255, 255), (250, 247, 238), (245, 245, 240)])
        ink = rng.choice(INKS)
        im = written(rng, split, (rng.randint(700, 1400), rng.randint(250, 480)), ink=ink, bg=(*paper, 255))
        add("upload", "written, scan", _jpeg(im, rng.randint(80, 95)), "POS", name="sig.jpg")
        im2 = _flatten(scribble(rng, (rng.randint(700, 1200), rng.randint(240, 400)), ink=(*ink, 255),
                                width=rng.randint(3, 6)), paper)
        add("upload", "scribble, scan", _jpeg(im2, rng.randint(80, 95)), "POS", name="sig.jpg")
        # DRAW --------------------------------------------------------------------------------------------
        cw, ch = rng.choice([(600, 200), (800, 300), (500, 180), (1000, 350)])
        stroke = rng.randint(2, 6)
        draw_ink = rng.choice([(0, 0, 0, 255), (20, 40, 160, 255), (255, 255, 255, 255), (40, 40, 40, 255)])
        drawn = scribble(rng, (cw, ch), ink=draw_ink, bg=(0, 0, 0, 0), width=stroke)
        add("draw", f"transparent canvas, ink {draw_ink[:3]}, w{stroke}", _png(drawn), "POS")
        add("draw", "white canvas", _png(_flatten(scribble(rng, (cw, ch), ink=(0, 0, 0, 255), width=stroke))), "POS")
        add("draw", "data URL", b"data:image/png;base64," + base64.b64encode(_png(drawn)), "POS", data_url=True)
        add("draw", "written with stylus (font), transparent",
            _png(written(rng, split, (cw, ch), ink=(10, 10, 10), bg=(0, 0, 0, 0))), "POS")
        # CAPTURE -----------------------------------------------------------------------------------------
        cap = written(rng, split, (rng.randint(900, 1600), rng.randint(400, 700)), ink=ink, bg=(*paper, 255),
                      margin=0.2).convert("RGB")
        add("capture", "photo of written signature", camera(cap, rng), "POS", name="cap.jpg")
        cap2 = _flatten(scribble(rng, (1200, 500), ink=(*ink, 255), width=rng.randint(4, 7), margin=0.2), paper)
        add("capture", "photo of scribble", camera(cap2, rng), "POS", name="cap.jpg")

        # HARDER REAL-WORLD POSITIVES (2026-10-17): a printed signature line across the whole frame, ruled
        # notebook paper, a low-resolution phone capture, yellowish paper
        base = written(rng, split, (1000, 360), ink=rng.choice(INKS), bg=(255, 255, 255, 255)).convert("RGB")
        lined = base.copy()
        ImageDraw.Draw(lined).line((0, int(lined.height * 0.78), lined.width, int(lined.height * 0.78)),
                                   fill=(90, 90, 90), width=2)
        ImageDraw.Draw(lined).text((10, int(lined.height * 0.82)), "Signature of the applicant",
                                   font=ImageFont.truetype(str(FONTS / "arial.ttf"), 18), fill=(80, 80, 80))
        add("upload", "on a printed signature line", _jpeg(lined, 90), "POS", name="sig.jpg")
        ruled = Image.new("RGB", base.size, (252, 252, 248))
        rd = ImageDraw.Draw(ruled)
        for y in range(30, ruled.height, 42):
            rd.line((0, y, ruled.width, y), fill=(170, 190, 230), width=1)
        ruled = Image.composite(base, ruled, base.convert("L").point(lambda v: 255 if v < 200 else 0))
        add("upload", "ruled notebook paper", _jpeg(ruled, 88), "POS", name="sig.jpg")
        small = written(rng, split, (900, 380), ink=rng.choice(INKS), bg=(250, 248, 242, 255), margin=0.2)
        small = small.convert("RGB").resize((420, 178), Image.BILINEAR)
        add("capture", "low-resolution phone capture", camera(small, rng), "POS", name="cap.jpg")
        yellow = written(rng, split, (1000, 380), ink=(30, 30, 90), bg=(250, 238, 190, 255), margin=0.2)
        add("capture", "yellowish paper", camera(yellow.convert("RGB"), rng), "POS", name="cap.jpg")

    # NEGATIVES --------------------------------------------------------------------------------------------
    for i in range(4):
        add("draw", "empty transparent canvas", _png(Image.new("RGBA", (600, 200), (0, 0, 0, 0))), "NEG")
        add("draw", "empty white canvas", _png(Image.new("RGB", (600, 200), "white")), "NEG")
        dot = Image.new("RGBA", (600, 200), (0, 0, 0, 0))
        ImageDraw.Draw(dot).ellipse((300, 100, 306, 106), fill=(0, 0, 0, 255))
        add("draw", "a dot", _png(dot), "NEG")
        line = Image.new("RGBA", (600, 200), (0, 0, 0, 0))
        ImageDraw.Draw(line).line((60, 120, 540, 118 + i), fill=(0, 0, 0, 255), width=4)
        add("draw", "one straight line", _png(line), "NEG")
        typed = Image.new("RGB", (800, 220), "white")
        ImageDraw.Draw(typed).text((40, 60), rng.choice(NAMES[split]).upper(),
                                   font=ImageFont.truetype(str(FONTS / "arial.ttf"), 90), fill=(0, 0, 0))
        add("draw", "typed name (font)", _png(typed), "NEG")
        big = scribble(rng, (600, 200), ink=(0, 0, 0, 255), width=4, margin=0.12)
        add("draw", "stroke runs off the canvas", _png(big.crop((0, 0, 380, 200))), "NEG")
        im = written(rng, split, (1000, 360), ink=(20, 20, 30), bg=(255, 255, 255, 255)).convert("RGB")
        add("upload", "blurred", _jpeg(im.filter(ImageFilter.GaussianBlur(4)), 88), "NEG", name="sig.jpg")
        add("upload", "cut in half", _jpeg(im.crop((0, 0, im.width // 2, im.height)), 90), "NEG", name="sig.jpg")
        add("upload", "blank paper", _jpeg(Image.new("RGB", (1000, 360), (248, 246, 240)), 90), "NEG", name="sig.jpg")
        page = Image.new("RGB", (1000, 700), "white")
        dd = ImageDraw.Draw(page)
        f = ImageFont.truetype(str(FONTS / "arial.ttf"), 26)
        for r in range(14):
            dd.text((40, 30 + r * 46), "This agreement is made between the parties on the date below.", font=f,
                    fill=(0, 0, 0))
        add("upload", "printed page", _jpeg(page, 90), "NEG", name="sig.jpg")
        blank_photo = camera(Image.new("RGB", (1200, 500), (245, 243, 236)), rng)
        add("capture", "photo of blank paper (shadow)", blank_photo, "NEG", name="cap.jpg")
        cap = written(rng, split, (1200, 500), ink=(20, 20, 30), bg=(250, 248, 240, 255), margin=0.2).convert("RGB")
        moved = cap.filter(ImageFilter.BoxBlur(5))
        add("capture", "motion / focus blur", camera(moved, rng), "NEG", name="cap.jpg")
        add("capture", "cut off by the frame", camera(cap.crop((cap.width // 2 - 80, 0, cap.width, cap.height)), rng),
            "NEG", name="cap.jpg")
    return out


def run(split: str, misses: bool, via_api: bool = False) -> dict:
    from app.agents.signature import presence

    rows = []
    for c in cases(split):
        data = c["data"]
        if c["data_url"]:
            data = base64.b64decode(data.split(b",", 1)[1])
        s = time.perf_counter()
        r = presence.check(data, source=c["mode"])
        ms = (time.perf_counter() - s) * 1000
        status = r["status"]
        ok = status == "VERIFIED" if c["expect"] == "POS" else status != "VERIFIED"
        rows.append({**{k: v for k, v in c.items() if k != "data"}, "status": status, "reasons": r["reasons"],
                     "ok": ok, "ms": round(ms)})
    summary = defaultdict(lambda: {"pos": 0, "pos_ok": 0, "pos_rejected": 0, "neg": 0, "neg_ok": 0, "ms": []})
    for r in rows:
        s = summary[r["mode"]]
        s["ms"].append(r["ms"])
        if r["expect"] == "POS":
            s["pos"] += 1
            s["pos_ok"] += r["ok"]
            s["pos_rejected"] += r["status"] == "REJECTED"
        else:
            s["neg"] += 1
            s["neg_ok"] += r["ok"]
    out = {}
    for mode, s in summary.items():
        ms = sorted(s.pop("ms"))
        out[mode] = {**s, "p50_ms": ms[len(ms) // 2], "max_ms": ms[-1]}
        print(f"{mode:8} positives VERIFIED {s['pos_ok']}/{s['pos']} (rejected {s['pos_rejected']})   "
              f"negatives never VERIFIED {s['neg_ok']}/{s['neg']}   p50 {ms[len(ms)//2]} ms  max {ms[-1]} ms")
    if misses:
        for r in rows:
            if not r["ok"]:
                print("  MISS", r["mode"], r["expect"], r["label"], "->", r["status"], r["reasons"])
    return {"summary": out, "rows": rows}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="dev", choices=["dev", "heldout"])
    ap.add_argument("--misses", action="store_true")
    a = ap.parse_args()
    run(a.split, a.misses)
