"""Картинка графіка погодинних відключень (вертикальна, під смартфон).

Рядки — години, стовпці — підчерги; кожна година — окремий блок, півгодини ділять блок навпіл.
Стан графіка видно з плашки в заголовку: ЗАВТРА / СЬОГОДНІ / МИНУВ / ЗАСТАРІЛИЙ. Минулий і застарілий
графіки — сірі, лінія поточного часу — лише на сьогоднішньому.
"""
from __future__ import annotations

import io
import logging
import os
from datetime import date, datetime
from functools import lru_cache

from .schedule import QUEUES, SLOTS, day_label

log = logging.getLogger(__name__)

# Шрифт із кирилицею. Debian/Ubuntu: apt install fonts-dejavu-core; Fedora/RHEL: dnf install dejavu-sans-fonts.
# Інший шрифт — GRAPH_FONT / GRAPH_FONT_BOLD. Якщо жоден шлях не підійшов — шукаємо DejaVuSans.ttf у теках шрифтів.
FONT_DIRS = ("/usr/share/fonts", "/usr/local/share/fonts", "/usr/X11R6/lib/X11/fonts", "/opt/homebrew/share/fonts")
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/local/share/fonts/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/arial.ttf",
)
BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/local/share/fonts/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)

BG = (255, 255, 255)
GRID = (205, 210, 218)
TEXT = (30, 34, 40)
MUTED = (110, 116, 126)
OFF = (38, 52, 74)            # світла немає
ON = (255, 214, 92)           # світло є
HL_BG = (232, 241, 255)
HL_BORDER = (40, 110, 230)
ADDED = (214, 48, 49)         # нове відключення порівняно з попередньою версією
REMOVED = (46, 160, 67)       # скасоване відключення
NOW_LINE = (214, 48, 49)

BADGES = {
    "future": ("ЗАВТРА", (40, 110, 230)),
    "today": ("СЬОГОДНІ", (34, 150, 70)),
    "past": ("МИНУВ", (130, 136, 146)),
    "superseded": ("ЗАСТАРІЛИЙ", (200, 60, 60)),
}
GREY_STATES = {"past", "superseded"}


class RenderUnavailable(RuntimeError):
    """Немає Pillow або шрифту з кирилицею."""


def _search(filename: str) -> str | None:
    """Шукає файл шрифту в стандартних теках (різні дистрибутиви кладуть DejaVu по-різному)."""
    for root in FONT_DIRS:
        if not os.path.isdir(root):
            continue
        for dirpath, _, files in os.walk(root):
            if filename in files:
                return os.path.join(dirpath, filename)
    return None


def _first_existing(env: str, candidates: tuple[str, ...], filename: str) -> str | None:
    custom = (os.environ.get(env) or "").strip()
    if custom:
        return custom if os.path.exists(custom) else None
    return next((p for p in candidates if os.path.exists(p)), None) or _search(filename)


_found: tuple[str, str] | None = None


def font_paths() -> tuple[str, str]:
    """Шляхи до звичайного й жирного шрифтів. Знайдений результат запам'ятовується; невдача — ні (шрифт можна
    доставити без перезапуску сервісу)."""
    global _found
    if _found:
        return _found
    regular = _first_existing("GRAPH_FONT", FONT_CANDIDATES, "DejaVuSans.ttf")
    bold = _first_existing("GRAPH_FONT_BOLD", BOLD_CANDIDATES, "DejaVuSans-Bold.ttf") or regular
    if not regular:
        raise RenderUnavailable("не знайдено шрифт із кирилицею (Debian/Ubuntu: apt install fonts-dejavu-core, "
                                "Fedora/RHEL: dnf install dejavu-sans-fonts; або задай GRAPH_FONT)")
    _found = (regular, bold)
    log.info("Шрифт для картинки графіка: %s", regular)
    return _found


def available() -> str | None:
    """None — малювати можна; інакше — причина, чому ні (для адмін-панелі)."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        return "не встановлено Pillow (pip install Pillow)"
    try:
        font_paths()
    except RenderUnavailable as exc:
        return str(exc)
    return None


@lru_cache(maxsize=32)
def _font(bold: bool, size: int):
    from PIL import ImageFont
    regular, bold_path = font_paths()
    return ImageFont.truetype(bold_path if bold else regular, size)


def render_day(
    day: date,
    slots: dict[str, tuple[bool, ...]],
    *,
    state: str = "today",
    highlight: frozenset[str] | set[str] | None = None,
    previous: dict[str, tuple[bool, ...]] | None = None,
    site_updated: str | None = None,
    now: datetime | None = None,
    footer: str | None = None,
) -> bytes:
    """PNG графіка на день. `previous` — попередня версія (позначки змін), `now` — для лінії часу (state=today)."""
    try:
        from PIL import Image, ImageDraw, ImageEnhance
    except ImportError as exc:
        raise RenderUnavailable("не встановлено Pillow") from exc
    highlight = frozenset(highlight or ())
    S = 2
    f_badge, f_title, f_sub = _font(True, 18 * S), _font(True, 30 * S), _font(False, 17 * S)
    f_q, f_qn, f_h = _font(True, 17 * S), _font(True, 13 * S), _font(True, 16 * S)
    f_sum, f_leg, f_note = _font(True, 14 * S), _font(False, 15 * S), _font(True, 15 * S)

    pad, hour_col = 16 * S, 72 * S
    col_w, col_gap, group_gap = 46 * S, 3 * S, 12 * S
    row_h, row_gap = 34 * S, 5 * S
    header_top = 112 * S
    grid_top = header_top + 58 * S

    xs, x = [], pad + hour_col
    for i in range(len(QUEUES)):
        if i and i % 2 == 0:
            x += group_gap - col_gap
        xs.append(x)
        x += col_w + col_gap
    W = x - col_gap + pad
    grid_h = 24 * (row_h + row_gap) - row_gap
    H = grid_top + grid_h + 150 * S

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Заголовок: плашка стану + день тижня і дата
    tx = pad
    if state in BADGES:
        btxt, bcol = BADGES[state]
        bw = d.textlength(btxt, font=f_badge) + 24 * S
        d.rounded_rectangle([pad, 22 * S, pad + bw, 56 * S], radius=8 * S, fill=bcol)
        d.text((pad + 12 * S, 27 * S), btxt, font=f_badge, fill=(255, 255, 255))
        tx = pad + bw + 14 * S
    d.text((tx, 20 * S), day_label(day), font=f_title, fill=TEXT)
    sub = f"Графік відключень · Хмельницькобленерго · {day.strftime('%d.%m.%Y')}"
    d.text((pad, 62 * S), sub, font=f_sub, fill=MUTED)
    if highlight:
        noun = "Ваша черга" if len(highlight) == 1 else "Ваші черги"
        d.text((pad, 86 * S), f"{noun}: {', '.join(sorted(highlight))}", font=f_sub, fill=HL_BORDER)

    for g in range(6):
        x0, x1 = xs[g * 2], xs[g * 2 + 1] + col_w
        label = f"Черга {g + 1}"
        tw = d.textlength(label, font=f_qn)
        d.text(((x0 + x1 - tw) / 2, header_top), label, font=f_qn, fill=MUTED)
        d.line([x0, header_top + 22 * S, x1, header_top + 22 * S], fill=GRID, width=S)
    for q, x in zip(QUEUES, xs):
        if q in highlight:
            d.rounded_rectangle([x - 4 * S, header_top + 26 * S, x + col_w + 4 * S, grid_top + grid_h + 46 * S],
                                radius=8 * S, fill=HL_BG)
        tw = d.textlength(q, font=f_q)
        d.text((x + (col_w - tw) / 2, header_top + 28 * S), q, font=f_q, fill=HL_BORDER if q in highlight else TEXT)

    empty = (False,) * SLOTS
    for h in range(24):
        y = grid_top + h * (row_h + row_gap)
        d.text((pad, y + (row_h - 20 * S) / 2), f"{h:02d}:00", font=f_h, fill=TEXT)
        for q, x in zip(QUEUES, xs):
            sl = slots.get(q, empty)
            a, b = sl[h * 2], sl[h * 2 + 1]
            half = row_h // 2
            if a == b:
                d.rounded_rectangle([x, y, x + col_w, y + row_h], radius=5 * S, fill=OFF if a else ON)
            else:   # відключення на півгодини
                d.rounded_rectangle([x, y, x + col_w, y + row_h], radius=5 * S, fill=ON)
                if a:
                    d.rounded_rectangle([x, y, x + col_w, y + half], radius=5 * S, fill=OFF)
                    d.rectangle([x, y + half - 5 * S, x + col_w, y + half], fill=OFF)
                else:
                    d.rounded_rectangle([x, y + half, x + col_w, y + row_h], radius=5 * S, fill=OFF)
                    d.rectangle([x, y + half, x + col_w, y + half + 5 * S], fill=OFF)
                d.line([x, y + half, x + col_w, y + half], fill=BG, width=S)
            if previous is not None:
                old = previous.get(q, empty)
                for k, (now_off, was_off) in enumerate(((a, old[h * 2]), (b, old[h * 2 + 1]))):
                    if now_off != was_off:
                        cy, r = y + (k + 0.5) * row_h / 2, 5 * S
                        d.ellipse([x + col_w - 3 * r, cy - r, x + col_w - r, cy + r],
                                  fill=ADDED if now_off else REMOVED, outline=BG, width=S)
            if q in highlight:
                d.rounded_rectangle([x - 2 * S, y - 2 * S, x + col_w + 2 * S, y + row_h + 2 * S],
                                    radius=6 * S, outline=HL_BORDER, width=2 * S)

    if state == "today" and now is not None:
        minutes = now.hour * 60 + now.minute
        hh, mm = divmod(minutes, 60)
        y = grid_top + hh * (row_h + row_gap) + mm / 60 * row_h
        d.line([pad + hour_col - 8 * S, y, W - pad, y], fill=NOW_LINE, width=3 * S)
        d.ellipse([pad + hour_col - 14 * S, y - 6 * S, pad + hour_col - 2 * S, y + 6 * S], fill=NOW_LINE)

    y = grid_top + grid_h + 14 * S
    d.text((pad, y + 2 * S), "Σ год", font=f_sum, fill=MUTED)
    for q, x in zip(QUEUES, xs):
        t = f"{sum(slots.get(q, empty)) / 2:g}"
        tw = d.textlength(t, font=f_sum)
        d.text((x + (col_w - tw) / 2, y + 2 * S), t, font=f_sum, fill=TEXT)

    ly = y + 62 * S
    items = [(OFF, "немає світла"), (ON, "світло є")]
    if previous is not None:
        items += [(ADDED, "нове"), (REMOVED, "скасовано")]
    x = pad
    for color, label in items:
        d.rounded_rectangle([x, ly + 2 * S, x + 22 * S, ly + 22 * S], radius=4 * S, fill=color)
        d.text((x + 30 * S, ly + 2 * S), label, font=f_leg, fill=TEXT)
        x += 30 * S + d.textlength(label, font=f_leg) + 24 * S
    stamp = []
    if site_updated:
        stamp.append(f"на сайті оновлено о {site_updated}")
    if now is not None:
        stamp.append(f"картинка {now.strftime('%d.%m %H:%M')}")
    if stamp:
        d.text((pad, ly + 38 * S), " · ".join(stamp).capitalize(), font=f_leg, fill=MUTED)

    if state in GREY_STATES:
        img = ImageEnhance.Color(img).enhance(0.0)
        img = Image.blend(img, Image.new("RGB", img.size, BG), 0.35)
        d = ImageDraw.Draw(img)
        note = footer or ("Графік минув" if state == "past" else "Є новіша версія графіка")
        d.text((W - pad - d.textlength(note, font=f_note), ly + 38 * S), note, font=f_note,
               fill=(200, 60, 60) if state == "superseded" else MUTED)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()