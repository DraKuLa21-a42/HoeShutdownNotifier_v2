"""Журнал сервісу для перегляду з бота: кільцевий буфер останніх записів у пам'яті
і (необов'язково) файл із ротацією, щоб бачити записи й після перезапуску.

Секрети (токени Telegram/Slack, SECRET_KEY) маскуються ще під час форматування — і в
journald, і у файлі, і в буфері.
"""
from __future__ import annotations

import logging
import logging.handlers
import re
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
BUFFER_SIZE = 3000              # записів у пам'яті
FILE_BACKUPS = 3                # hoe.log.1 … hoe.log.3
PRELOAD_BYTES = 512 * 1024      # скільки хвоста файлу підтягнути в буфер після перезапуску

_TOKEN_PATTERNS = (
    re.compile(r"\b(?:bot)?\d{6,12}:[A-Za-z0-9_-]{30,}"),        # токен Telegram-бота
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{8,}"),                 # токени Slack
    re.compile(r"\bxapp-[A-Za-z0-9-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),         # bearer-токен кабінету
)
_LINE_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:,\d{3})? (DEBUG|INFO|WARNING|ERROR|CRITICAL) ([^:\s]+): ?(.*)$")


def redact(text: str, extra: tuple[str, ...] = ()) -> str:
    for secret in extra:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "***")
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "***", text)
    return text


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str = LOG_FORMAT, secrets: tuple[str, ...] = ()):
        super().__init__(fmt)
        self.secrets = tuple(s for s in secrets if s)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self.secrets)


@dataclass(frozen=True)
class LogRecordView:
    seq: int
    created: float
    levelno: int
    name: str
    text: str           # повідомлення разом із traceback, уже з маскуванням

    @property
    def levelname(self) -> str:
        return logging.getLevelName(self.levelno)


class LogBuffer(logging.Handler):
    """Тримає останні BUFFER_SIZE записів; кожен має зростаючий seq для стабільного гортання."""

    def __init__(self, capacity: int = BUFFER_SIZE, secrets: tuple[str, ...] = ()):
        super().__init__(level=logging.NOTSET)
        self.records: deque[LogRecordView] = deque(maxlen=capacity)
        self._seq = 0
        self._guard = threading.Lock()
        self.secrets = tuple(s for s in secrets if s)
        self.file_path: Path | None = None
        self._exc = logging.Formatter()

    def _push(self, created: float, levelno: int, name: str, text: str) -> None:
        with self._guard:
            self._seq += 1
            self.records.append(LogRecordView(self._seq, created, levelno, name, text))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + self._exc.formatException(record.exc_info)
            elif record.exc_text:
                text += "\n" + record.exc_text
            if record.stack_info:
                text += "\n" + record.stack_info
            self._push(record.created, record.levelno, record.name, redact(text, self.secrets))
        except Exception:
            self.handleError(record)

    def snapshot(self) -> list[LogRecordView]:
        with self._guard:
            return list(self.records)

    def preload_file(self, path: Path, max_bytes: int = PRELOAD_BYTES) -> int:
        """Підтягує в буфер хвіст файлу журналу (записи з попередніх запусків). Повертає кількість записів."""
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - max_bytes))
                data = fh.read().decode("utf-8", "replace")
        except OSError:
            return 0
        if size > max_bytes:            # перший рядок, імовірно, обрізаний
            data = data.split("\n", 1)[-1]
        parsed: list[tuple[float, int, str, list[str]]] = []
        for line in data.splitlines():
            m = _LINE_RE.match(line)
            if m:
                try:
                    created = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                except ValueError:
                    continue
                parsed.append((created, logging.getLevelName(m.group(2)), m.group(3), [m.group(4)]))
            elif parsed:                 # продовження traceback
                parsed[-1][3].append(line)
        for created, levelno, name, lines in parsed[-(self.records.maxlen or BUFFER_SIZE):]:
            self._push(created, levelno, name, redact("\n".join(lines), self.secrets))
        return len(parsed)

    def file_tail(self, max_bytes: int = 2 * 1024 * 1024) -> bytes | None:
        """Останні max_bytes журналу з файлу (разом із попереднім файлом ротації, якщо треба)."""
        if self.file_path is None:
            return None
        chunks: list[bytes] = []
        left = max_bytes
        for path in (self.file_path, self.file_path.with_name(self.file_path.name + ".1")):
            if left <= 0:
                break
            try:
                with open(path, "rb") as fh:
                    fh.seek(0, 2)
                    size = fh.tell()
                    fh.seek(max(0, size - left))
                    chunk = fh.read()
            except OSError:
                continue
            chunks.insert(0, chunk)
            left -= len(chunk)
        return b"".join(chunks) if chunks else None


_buffer: LogBuffer | None = None


def get_buffer() -> LogBuffer | None:
    return _buffer


def setup_logging(level: str, *, log_file: Path | None = None, max_mb: int = 2,
                  secrets: tuple[str, ...] = ()) -> LogBuffer:
    """Налаштовує кореневий логер: stderr (journald) + буфер для бота + файл (якщо задано)."""
    global _buffer
    root = logging.getLogger()
    root.setLevel(level)
    formatter = RedactingFormatter(secrets=secrets)

    stderr = logging.StreamHandler()
    stderr.setFormatter(formatter)
    handlers: list[logging.Handler] = [stderr]

    buffer = LogBuffer(secrets=secrets)
    file_problem = None
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            buffer.preload_file(log_file)
            fh = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=max(1, max_mb) * 1024 * 1024, backupCount=FILE_BACKUPS, encoding="utf-8")
            fh.setFormatter(formatter)
            handlers.append(fh)
            buffer.file_path = log_file
        except OSError as exc:
            file_problem = exc
    handlers.append(buffer)

    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    _buffer = buffer
    if file_problem is not None:
        logging.getLogger("hoe_notifier").warning("Не вдалось відкрити файл журналу %s: %s", log_file, file_problem)
    return buffer


# ------------------------------------------------------------------ подання для бота
FILTERS = {"a": logging.NOTSET, "w": logging.WARNING, "e": logging.ERROR}
FILTER_TITLES = {"a": "усі", "w": "попередження й помилки", "e": "лише помилки"}
_ICONS = {logging.DEBUG: "·", logging.INFO: "ℹ", logging.WARNING: "⚠️", logging.ERROR: "❌", logging.CRITICAL: "🔥"}


def icon(levelno: int) -> str:
    for lvl in (logging.CRITICAL, logging.ERROR, logging.WARNING, logging.INFO):
        if levelno >= lvl:
            return _ICONS[lvl]
    return _ICONS[logging.DEBUG]


def filtered(records: list[LogRecordView], flt: str) -> list[LogRecordView]:
    minimum = FILTERS.get(flt, logging.NOTSET)
    return [r for r in records if r.levelno >= minimum]


@dataclass
class LogPage:
    records: list[LogRecordView]   # у хронологічному порядку
    has_older: bool
    has_newer: bool
    total: int                      # записів під фільтром


def page(records: list[LogRecordView], flt: str, *, anchor: int = 0, direction: str = "l",
         budget: int = 3300, max_records: int = 25, record_limit: int = 600) -> LogPage:
    """Сторінка журналу, що влазить у повідомлення Telegram.

    direction: "l" — найновіші; "o" — старіші за запис seq=anchor; "n" — новіші за нього.
    """
    items = filtered(records, flt)
    if direction == "n":
        pool = [r for r in items if r.seq > anchor]
        chosen, used = [], 0
        for r in pool:
            cost = min(len(r.text), record_limit) + 40
            if chosen and (used + cost > budget or len(chosen) >= max_records):
                break
            chosen.append(r)
            used += cost
        if len(chosen) == len(pool):    # дійшли до кінця — показуємо як «найновіші», щоб сторінка була повна
            return page(records, flt, budget=budget, max_records=max_records, record_limit=record_limit)
    else:
        pool = [r for r in items if direction != "o" or r.seq < anchor]
        chosen, used = [], 0
        for r in reversed(pool):
            cost = min(len(r.text), record_limit) + 40
            if chosen and (used + cost > budget or len(chosen) >= max_records):
                break
            chosen.append(r)
            used += cost
        chosen.reverse()
    if not chosen:
        return LogPage([], False, False, len(items))
    return LogPage(chosen, has_older=items[0].seq < chosen[0].seq, has_newer=items[-1].seq > chosen[-1].seq,
                   total=len(items))


def format_record(r: LogRecordView, *, record_limit: int = 600, today: str | None = None) -> str:
    """Один запис для показу в <pre> (текст ще не екранований)."""
    dt = datetime.fromtimestamp(r.created)
    stamp = dt.strftime("%H:%M:%S") if dt.strftime("%Y-%m-%d") == today else dt.strftime("%d.%m %H:%M:%S")
    name = r.name.removeprefix("hoe_notifier.")
    text = r.text
    if len(text) > record_limit:    # початок і кінець: у traceback найважливіше — останній рядок
        head = record_limit // 3
        text = (text[:head].rstrip() + "\n…[обрізано, повністю — «📄 Файлом»]…\n"
                + text[-(record_limit - head):].lstrip())
    return f"{icon(r.levelno)} {stamp} {name}: {text}"


def as_text(records: list[LogRecordView]) -> str:
    """Записи як звичайний текстовий журнал (для надсилання файлом)."""
    lines = []
    for r in records:
        stamp = datetime.fromtimestamp(r.created).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{stamp} {r.levelname} {r.name}: {r.text}")
    return "\n".join(lines) + ("\n" if lines else "")