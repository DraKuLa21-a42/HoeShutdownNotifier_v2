"""Черги погодинних відключень (ГПВ): розпізнавання та фільтрація текстів.

Користувач вказує свою підчергу (1.1 … 6.2). Фільтр застосовується до двох джерел:
  * таблиці відключень за адресою (рядок «Черга (ГПВ): 1.1» у кожному записі);
  * текстового графіка зі сторінки (рядки «Підчерга 1.1 – 00:00-04:00; …»).
"""
from __future__ import annotations

import re
from typing import Iterable

from .hoe import NO_OUTAGES

# Усі підчерги, які існують на сайті: від 1.1 до 6.2.
QUEUES: tuple[str, ...] = tuple(f"{n}.{m}" for n in range(1, 7) for m in (1, 2))

_BLANK_LINE = re.compile(r"\n\s*\n")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
# «Черга (ГПВ): 1.1», «Черга ГПВ: 1.1»; рядок «Черга (ГАВ): 7» навмисно не збігається.
_GPV_LINE = re.compile(r"^\s*черга\s*\(?\s*гпв\s*\)?\s*:\s*(.*)$", re.IGNORECASE)
# «Підчерга 1.1 – 00:00-04:00; …», а також загальний варіант «Черга 2: 06:00-10:00».
_SCHEDULE_LINE = re.compile(r"^\s*(?:під)?черга\s+(\d+(?:\.\d+)?)(?!\d)", re.IGNORECASE)


def normalize_queue(value: object) -> str | None:
    """'1.1' / ' 1,1 ' → '1.1'. Невідому чергу повертає як None."""
    if value is None:
        return None
    candidate = str(value).strip().replace(",", ".")
    return candidate if candidate in QUEUES else None


def queue_matches(row_queue: str, queue: str) -> bool:
    """Чи стосується запис черги `row_queue` користувача з підчергою `queue`.

    Запис про цілу чергу («1») стосується обох її підчерг (1.1 і 1.2): краще показати зайве, ніж пропустити відключення.
    """
    return row_queue == queue or row_queue == queue.split(".")[0]


def block_queues(block: str) -> list[str]:
    """Черги ГПВ, указані в записі про відключення (може бути кілька: «1.1, 1.2»). Порожньо — черги немає."""
    for line in block.splitlines():
        m = _GPV_LINE.match(line)
        if m:
            return _NUMBER.findall(m.group(1))
    return []


def filter_outages(text: str, queue: str | None) -> str:
    """Лишає записи про відключення для черги `queue` та записи без черги ГПВ (наприклад, аварійні).

    Без `queue` текст не змінюється. Якщо нічого не лишилось — порожній рядок.
    """
    if not queue or not text:
        return text
    kept = []
    for block in _BLANK_LINE.split(text.strip()):
        if not block.strip():
            continue
        queues = block_queues(block)
        if not queues or any(queue_matches(q, queue) for q in queues):
            kept.append(block.strip())
    return "\n\n".join(kept)


def outage_view(state_text: str | None, queue: str | None) -> str:
    """Що бачить підписник із чергою `queue` у збереженому стані адреси (порожньо — відключень немає)."""
    if not state_text or state_text == NO_OUTAGES:
        return ""
    return filter_outages(state_text, queue)


def queue_line(line: str) -> str | None:
    m = _SCHEDULE_LINE.match(line)
    return m.group(1) if m else None


def filter_schedule(text: str, queues: Iterable[str] | None) -> str:
    """Текстовий графік: заголовок + рядки лише вказаних черг. Без `queues` текст не змінюється.

    Групи, у яких не лишилось жодного рядка черги, відкидаються; порожні рядки між групами зберігаються.
    Якщо у графіку немає рядків потрібних черг, дописується про це примітка.
    """
    wanted = sorted({q for q in (queues or ())})
    if not wanted or not text:
        return text

    had_queue_lines = kept_queue_lines = False
    groups: list[str] = []
    for group in _BLANK_LINE.split(text.strip()):
        kept: list[str] = []
        group_has_queues = False
        for line in group.split("\n"):
            q = queue_line(line)
            if q is None:
                kept.append(line)
                continue
            had_queue_lines = group_has_queues = True
            if any(queue_matches(q, w) for w in wanted):
                kept.append(line)
                kept_queue_lines = True
        if group_has_queues and not any(queue_line(l) for l in kept):
            continue
        if kept:
            groups.append("\n".join(kept))

    if not had_queue_lines:      # формат невідомий — краще віддати як є, ніж спотворити
        return text
    result = "\n\n".join(groups)
    if not kept_queue_lines:
        noun = "черги" if len(wanted) == 1 else "черг"
        result += f"\n\nДля {noun} {', '.join(wanted)} у цьому графіку відключень немає."
    return result
