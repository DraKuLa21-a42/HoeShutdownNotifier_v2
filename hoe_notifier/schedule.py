"""Графіки за датами зі сторінки погодинних відключень: розбір у півгодинні слоти та обчислення стану.

Сторінка може містити кілька графіків (новіші — вище), розділених `<hr>`:
  * графік на завтра над графіком на сьогодні;
  * «Додаткові години відсутності електроенергії» для дати — доповнення до основного графіка цієї дати;
  * старіша версія графіка на ту саму дату (нижче за новішу) — ігнорується.

Результат — `DaySchedule` на кожну дату: 12 підчерг × 48 півгодинних слотів (True — світла немає).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

KYIV = ZoneInfo("Europe/Kyiv")
QUEUES: tuple[str, ...] = tuple(f"{n}.{m}" for n in range(1, 7) for m in (1, 2))
SLOTS = 48                       # півгодинні слоти доби
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "нд")

MONTHS = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}
_DATE_NUM = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)")
_DATE_WORD = re.compile(r"(?<!\d)(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?", re.IGNORECASE)
_UPDATED = re.compile(r"оновлено[,:\s]*(?:о\s*)?(\d{1,2}:\d{2})", re.IGNORECASE)
_LINE = re.compile(r"^\s*(під)?черга\s+(\d)(?:\.(\d))?(?!\d)(.*)$", re.IGNORECASE)
_RANGE = re.compile(r"(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})")
_EXTRA = re.compile(r"додатков", re.IGNORECASE)
_NO_OUTAGES = re.compile(
    r"не\s+(?:будуть\s+)?(?:застосов|прогноз|передбача)|без\s+(?:застосування\s+)?(?:графік|відключен)"
    r"|відключен\w*\s+не\s+(?:плану|застосов|передбача|прогноз)", re.IGNORECASE)
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)


def now_kyiv() -> datetime:
    return datetime.now(KYIV)


def empty_slots() -> dict[str, tuple[bool, ...]]:
    return {q: (False,) * SLOTS for q in QUEUES}


@dataclass(frozen=True)
class DaySchedule:
    day: date
    slots: dict[str, tuple[bool, ...]]   # підчерга → 48 слотів, True — світла немає
    updated: str | None = None           # «оновлено, 12:30» з сайту

    @property
    def key(self) -> str:
        return self.day.isoformat()

    @property
    def hash(self) -> str:
        """Відбиток лише самих слотів: зміна формулювання на сайті не вважається зміною графіка."""
        return slots_hash(self.slots)


def slots_hash(slots: dict[str, tuple[bool, ...]]) -> str:
    raw = "|".join(q + ":" + "".join("1" if s else "0" for s in slots.get(q, (False,) * SLOTS)) for q in QUEUES)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def slots_to_json(slots: dict[str, tuple[bool, ...]]) -> str:
    return json.dumps({q: "".join("1" if s else "0" for s in slots.get(q, ())) for q in QUEUES})


def slots_from_json(raw: str) -> dict[str, tuple[bool, ...]]:
    data = json.loads(raw)
    out = empty_slots()
    for q, bits in data.items():
        if q in out and len(bits) == SLOTS:
            out[q] = tuple(b == "1" for b in bits)
    return out


# --- розбір сторінки ---------------------------------------------------------

def _slot(h: int, m: int) -> int:
    return min(SLOTS, h * 2 + (1 if m >= 30 else 0))


def _apply_ranges(target: list[bool], text: str) -> bool:
    found = False
    for h1, m1, h2, m2 in _RANGE.findall(text):
        a, b = _slot(int(h1), int(m1)), _slot(int(h2), int(m2))
        if b <= a:               # «22:00-00:00» — до кінця доби
            b = SLOTS
        for i in range(a, b):
            target[i] = True
        found = True
    return found


def _infer_year(day: int, month: int, today: date) -> date | None:
    best = None
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs((d - today).days) < abs((best - today).days):
            best = d
    return best


def find_date(text: str, today: date) -> date | None:
    m = _DATE_NUM.search(text)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    m = _DATE_WORD.search(text)
    if m:
        day, month = int(m.group(1)), MONTHS[m.group(2).lower()]
        if m.group(3):
            try:
                return date(int(m.group(3)), month, day)
            except ValueError:
                return None
        return _infer_year(day, month, today)
    return None


@dataclass
class _Section:
    day: date | None
    updated: str | None
    extra: bool
    slots: dict[str, list[bool]]
    has_lines: bool
    no_outages: bool


def _paragraphs(root) -> list[str | None]:
    """Тексти абзаців; None — роздільник `<hr>`."""
    items: list[str | None] = []
    for el in root.find_all(["p", "hr"]):
        if el.name == "hr":
            items.append(None)
            continue
        inner = _BR.sub("\n", re.sub(r"\s+", " ", el.decode_contents()))
        text = BeautifulSoup(inner, "html.parser").get_text().replace("\xa0", " ")
        items.append("\n".join(line.strip() for line in text.splitlines() if line.strip()))
    return items


def _section(paragraphs: list[str], today: date) -> _Section | None:
    text = "\n".join(paragraphs)
    slots = {q: [False] * SLOTS for q in QUEUES}
    has_lines = False
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        sub, major, minor, rest = m.groups()
        queues = [f"{major}.{minor}"] if minor else [f"{major}.1", f"{major}.2"]   # «Черга 1» — обидві підчерги
        for q in queues:
            if q in slots and _apply_ranges(slots[q], rest):
                has_lines = True
    day = find_date(text, today)
    if not has_lines and not (day and _NO_OUTAGES.search(text)):
        return None
    upd = _UPDATED.search(text)
    return _Section(day=day, updated=upd.group(1) if upd else None, extra=bool(_EXTRA.search(text)),
                    slots=slots, has_lines=has_lines, no_outages=not has_lines)


def parse_days(html: str, today: date | None = None) -> list[DaySchedule]:
    """Усі графіки зі сторінки, по одному на дату (новіші дати — першими)."""
    today = today or now_kyiv().date()
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("div.post") or soup
    sections: list[_Section] = []
    chunk: list[str] = []
    for item in _paragraphs(root) + [None]:
        if item is None:
            sec = _section(chunk, today) if chunk else None
            if sec:
                sections.append(sec)
            chunk = []
        elif item:
            chunk.append(item)

    # Розділ без дати належить до дати сусіднього (наступного) розділу; якщо такого немає — сьогодні.
    for i, sec in enumerate(sections):
        if sec.day is None:
            nxt = next((s.day for s in sections[i + 1:] if s.day), None)
            sec.day = nxt or today
            log.info("Графік без дати на сторінці — вважаю, що він на %s", sec.day)

    result: dict[date, DaySchedule] = {}
    pending_extra: dict[date, list[_Section]] = {}
    for sec in sections:                              # згори вниз: від новішого до старішого
        if sec.day in result:
            continue                                  # старіша версія вже врахованої дати
        if sec.extra and sec.has_lines:
            pending_extra.setdefault(sec.day, []).append(sec)
            continue
        merged = {q: list(v) for q, v in sec.slots.items()}
        extras = pending_extra.pop(sec.day, [])
        for extra in extras:
            for q in QUEUES:
                merged[q] = [a or b for a, b in zip(merged[q], extra.slots[q])]
        # «оновлено» — з найновішого доповнення (воно найвище на сторінці), інакше з основного графіка
        updated = next((e.updated for e in extras if e.updated), None) or sec.updated
        result[sec.day] = DaySchedule(sec.day, {q: tuple(v) for q, v in merged.items()}, updated)
    for day, extras in pending_extra.items():          # доповнення без основного графіка — що є
        merged = {q: [any(e.slots[q][i] for e in extras) for i in range(SLOTS)] for q in QUEUES}
        result[day] = DaySchedule(day, {q: tuple(v) for q, v in merged.items()}, extras[0].updated)
    return sorted(result.values(), key=lambda d: d.day, reverse=True)


# --- обчислення для підписів ---------------------------------------------------

def fmt_slot(i: int) -> str:
    return f"{i // 2:02d}:{'30' if i % 2 else '00'}"


def intervals(slots: tuple[bool, ...]) -> list[tuple[int, int]]:
    """Суміжні слоти без світла: [(початок, кінець)) у слотах."""
    out, start = [], None
    for i, off in enumerate(slots):
        if off and start is None:
            start = i
        elif not off and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, SLOTS))
    return out


def fmt_intervals(slots: tuple[bool, ...]) -> str:
    parts = [f"{fmt_slot(a)}–{fmt_slot(b)}" for a, b in intervals(slots)]
    return ", ".join(parts) if parts else "без відключень"


def hours_off(slots: tuple[bool, ...]) -> str:
    h = sum(slots) / 2
    return f"{h:g} год"


def fmt_duration(minutes: int) -> str:
    h, m = divmod(max(0, minutes), 60)
    if h and m:
        return f"{h} год {m} хв"
    return f"{h} год" if h else f"{m} хв"


def status_now(slots: tuple[bool, ...], now: datetime, *, with_left: bool = True) -> str:
    """«🟢 світло є · відключення о 17:00 (через 2 год 15 хв)» для поточного моменту доби.

    `with_left=False` — без «через …»: у підписі, який оновлюється не щохвилини, це число швидко застаріває.
    """
    minute = now.hour * 60 + now.minute
    idx = min(SLOTS - 1, minute // 30)
    off = slots[idx]
    j = idx
    while j < SLOTS and slots[j] == off:
        j += 1
    left = j * 30 - minute
    tail = (lambda: f" (через {fmt_duration(left)})") if with_left else (lambda: "")
    if off:
        if j >= SLOTS:
            return "🔴 світла немає до кінця доби"
        return f"🔴 світла немає · увімкнення о {fmt_slot(j)}{tail()}"
    if j >= SLOTS:
        return "🟢 світло є · до кінця доби відключень немає"
    return f"🟢 світло є · відключення о {fmt_slot(j)}{tail()}"


def changes(old: tuple[bool, ...], new: tuple[bool, ...]) -> str:
    """Що змінилось для черги: «+13:00–14:00, −20:00–22:00»; порожньо — без змін."""
    added = tuple(n and not o for o, n in zip(old, new))
    removed = tuple(o and not n for o, n in zip(old, new))
    parts = [f"+{fmt_slot(a)}–{fmt_slot(b)}" for a, b in intervals(added)]
    parts += [f"−{fmt_slot(a)}–{fmt_slot(b)}" for a, b in intervals(removed)]
    return ", ".join(parts)


def day_label(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}"


def day_state(d: date, today: date) -> str:
    """future | today | past."""
    if d > today:
        return "future"
    return "today" if d == today else "past"


def relative_word(d: date, today: date) -> str:
    delta = (d - today).days
    return {0: "Сьогодні", 1: "Завтра", -1: "Вчора"}.get(delta, day_label(d))


def tomorrow(today: date) -> date:
    return today + timedelta(days=1)