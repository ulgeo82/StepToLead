"""Уникальная версия аватарки для каждого аккаунта: обрезка и масштаб, лёгкий поворот, цвет, зеркало, шум,
пересжатие, без EXIF. Не защищает от бана сама — главное лимиты и тексты; убирает очевидную одинаковость фото."""
from __future__ import annotations

import io
import random

from PIL import Image, ImageEnhance, ImageOps

STRENGTH = {"low": {"crop": 0.06, "rot": 1.0, "color": 0.05, "noise": 4},
            "medium": {"crop": 0.10, "rot": 2.0, "color": 0.08, "noise": 7}}
MAX_SIDE = 1280


def variant(content: bytes, seed: int | str, strength: str = "low") -> bytes:
    p = STRENGTH.get(strength, STRENGTH["low"])
    rng = random.Random(str(seed))
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(content))).convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    w, h = image.size
    side = min(w, h)
    cut = side * (1 - rng.uniform(p["crop"] / 2, p["crop"]))
    left = rng.uniform(0, w - cut)
    top = rng.uniform(0, h - cut)
    image = image.crop((int(left), int(top), int(left + cut), int(top + cut)))
    if rng.random() < 0.5:
        image = ImageOps.mirror(image)
    angle = rng.uniform(-p["rot"], p["rot"])
    if abs(angle) > 0.1:
        rotated = image.rotate(angle, resample=Image.BICUBIC, expand=False)
        margin = int(image.size[0] * 0.03)
        image = rotated.crop((margin, margin, image.size[0] - margin, image.size[1] - margin))
    for enhancer in (ImageEnhance.Brightness, ImageEnhance.Contrast, ImageEnhance.Color):
        image = enhancer(image).enhance(1 + rng.uniform(-p["color"], p["color"]))
    size = rng.randint(560, 720)
    image = image.resize((size, size), Image.LANCZOS)
    if p["noise"]:
        noise = Image.effect_noise((size, size), p["noise"] * 4).convert("RGB")
        image = Image.blend(image, noise, 0.02 + rng.random() * 0.02)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=rng.randint(84, 93), optimize=True)   # без EXIF: не передаём exif=
    return out.getvalue()
