#!/usr/bin/env python3
"""Brand logo fetcher + catalog grid image generator for the shop bot.

Fetches brand logos (Clearbit -> Google favicons -> generated fallback),
caches them, and builds a 3-column brand grid image like the reference.
"""
import hashlib
import io
import os
import urllib.request

from PIL import Image, ImageDraw, ImageFont

_HERE = os.path.dirname(os.path.abspath(__file__))
LOGO_DIR = os.path.join(_HERE, "brand_logos")
os.makedirs(LOGO_DIR, exist_ok=True)

# brand (lowercase) -> domain for logo lookup
BRAND_DOMAINS = {
    "gemini": "gemini.google.com",
    "chatgpt": "openai.com",
    "openai": "openai.com",
    "claude": "anthropic.com",
    "grok": "x.ai",
    "capcut": "capcut.com",
    "canva": "canva.com",
    "duolingo": "duolingo.com",
    "cursor": "cursor.com",
    "lovable": "lovable.dev",
    "vpn": "nordvpn.com",
    "gmail": "gmail.com",
    "telegram": "telegram.org",
    "adobe": "adobe.com",
    "zoom": "zoom.us",
    "outlook": "outlook.com",
    "figma": "figma.com",
    "linkedin": "linkedin.com",
    "youtube": "youtube.com",
    "spotify": "spotify.com",
    "netflix": "netflix.com",
    "hbo": "max.com",
    "apple": "apple.com",
    "amazon": "amazon.com",
    "coursera": "coursera.org",
    "udemy": "udemy.com",
    "office": "microsoft.com",
    "microsoft": "microsoft.com",
    "paypal": "paypal.com",
    "discord": "discord.com",
    "tiktok": "tiktok.com",
    "notion": "notion.so",
    "miro": "miro.com",
    "edX": "edx.org",
    "edx": "edx.org",
    "fortnite": "epicgames.com",
    "jetbrains": "jetbrains.com",
    "ilovepdf": "ilovepdf.com",
    "autodesk": "autodesk.com",
    "api": "openai.com",
}

FALLBACK_COLORS = [
    (66, 133, 244), (219, 68, 55), (244, 180, 0), (15, 157, 88),
    (156, 39, 176), (0, 172, 193), (255, 109, 0), (94, 53, 177),
]


def _fetch(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    if img.width < 16 or img.height < 16:
        raise ValueError("logo too small")
    return img


def get_logo(brand):
    """Return path to cached logo PNG for brand, fetching if needed."""
    key = brand.strip().lower()
    path = os.path.join(LOGO_DIR, f"{key}.png")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    domain = BRAND_DOMAINS.get(key)
    if not domain:
        # guess: brand.com
        domain = f"{key}.com"
    img = None
    for url in (f"https://logo.clearbit.com/{domain}?size=128",
                f"https://www.google.com/s2/favicons?domain={domain}&sz=128"):
        try:
            img = _fetch(url)
            break
        except Exception:
            continue
    if img is None:
        img = _fallback_icon(brand)
    # normalize: 128x128, rounded
    img = _rounded(img.resize((128, 128), Image.LANCZOS), 28)
    img.save(path)
    return path


def _fallback_icon(brand):
    color = FALLBACK_COLORS[abs(hash(brand.lower())) % len(FALLBACK_COLORS)]
    img = Image.new("RGBA", (128, 128), color + (255,))
    d = ImageDraw.Draw(img)
    letter = (brand.strip()[:1] or "?").upper()
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 64)
    except Exception:
        font = ImageFont.load_default()
    bbox = d.textbbox((0, 0), letter, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((128 - w) / 2 - bbox[0], (128 - h) / 2 - bbox[1]), letter,
           fill="white", font=font)
    return img


def _rounded(img, radius):
    mask = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, img.size[0], img.size[1]], radius=radius, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def build_brand_grid(brands, out_path=None):
    """Build a 3-column brand grid image. brands: list of names. Returns path."""
    cols = 3
    cell_w, cell_h = 360, 120
    pad = 16
    rows = (len(brands) + cols - 1) // cols
    W = cols * cell_w + (cols + 1) * pad
    H = rows * cell_h + (rows + 1) * pad
    bg = Image.new("RGB", (W, H), (24, 24, 28))
    d = ImageDraw.Draw(bg)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 40)
    except Exception:
        font = ImageFont.load_default()

    for i, brand in enumerate(brands):
        r, c = divmod(i, cols)
        x0 = pad + c * (cell_w + pad)
        y0 = pad + r * (cell_h + pad)
        # card
        d.rounded_rectangle([x0, y0, x0 + cell_w, y0 + cell_h], radius=24,
                            fill=(52, 52, 58))
        # logo
        try:
            logo = Image.open(get_logo(brand)).convert("RGBA").resize((72, 72), Image.LANCZOS)
            bg.paste(logo, (x0 + 24, y0 + (cell_h - 72) // 2), logo)
        except Exception:
            pass
        # name
        label = brand if len(brand) <= 12 else brand[:11] + "…"
        d.text((x0 + 112, y0 + (cell_h - 44) // 2), label, fill="white", font=font)

    if out_path is None:
        h = hashlib.md5("|".join(sorted(brands)).encode()).hexdigest()[:12]
        out_path = os.path.join(_HERE, f"brand_grid_{h}.png")
    bg.save(out_path)
    return out_path


if __name__ == "__main__":
    import sys
    brands = sys.argv[1:] or ["Gemini", "ChatGPT", "Canva", "Netflix"]
    p = build_brand_grid(brands)
    print("saved:", p)
