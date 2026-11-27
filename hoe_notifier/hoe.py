"""Клієнт сайту hoe.com.ua: запит відключень, парсинг, графік, пошук вулиці."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

EVENTS_PATH = "/shutdown-events"
FORM_PATH = "/shutdown/new"
GRAPH_PAGE_PATH = "/page/pogodinni-vidkljuchennja"
GRAPH_ALT_PATTERN = re.compile(r"(ГПВ|gpv)", re.I)
USER_AGENT = "Mozilla/5.0 (compatible; HoeShutdownNotifier/2.0)"
NO_OUTAGES = "NO_OUTAGES"


class HoeUnavailable(RuntimeError):
    """Сайт не відповів / повернув порожню відповідь після всіх спроб."""


class SearchUnavailable(RuntimeError):
    """Пошук населених пунктів / вулиць / будинків на сайті не відповів або повернув сміття."""


class ParseStatus(Enum):
    OUTAGES = "outages"   # знайдено таблицю з відключеннями
    NONE = "none"         # відключень немає
    UNKNOWN = "unknown"   # незрозумілий формат (імовірно змінилась верстка)


@dataclass(frozen=True)
class Parsed:
    status: ParseStatus
    text: str = ""


@dataclass(frozen=True)
class Option:
    """Елемент відповіді select2: населений пункт або вулиця."""
    id: str
    name: str


def normalize(text: str) -> str:
    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())


def parse_events(html: str) -> Parsed:
    """Перетворює HTML-відповідь /shutdown-events на текст.

    Стійкіший за оригінал: не падає без <thead>/<tbody>, не губить рядки,
    кількість комірок яких не збігається із заголовками.
    """
    soup = BeautifulSoup(html, "html.parser")
    blocks: list[str] = []

    # на сайті таблиця має клас table-shutdowns; якщо верстка зміниться — беремо всі таблиці
    for table in soup.select("table.table-shutdowns") or soup.find_all("table"):
        rows = table.find_all("tr")
        headers = [th.get_text(" ", strip=True) for th in table.select("thead th")]
        if not headers and rows and rows[0].find("th") and not rows[0].find("td"):
            headers = [th.get_text(" ", strip=True) for th in rows[0].find_all("th")]
        for tr in rows:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if not cells:
                continue
            if headers and len(cells) == len(headers):
                blocks.append("\n".join(f"{h}: {c}" for h, c in zip(headers, cells)))
            else:
                blocks.append(" | ".join(cells))

    blocks = [b for b in (normalize(b) for b in blocks) if b]
    if blocks:
        # кожна подія — окремий блок, розділений порожнім рядком (в оригіналі вони зливались)
        return Parsed(ParseStatus.OUTAGES, "\n\n".join(blocks))
    if "відключ" in soup.get_text(" ").lower():
        return Parsed(ParseStatus.NONE)
    return Parsed(ParseStatus.UNKNOWN)


@dataclass(frozen=True)
class Graphs:
    """Графік, опублікований на сторінці погодинних відключень: картинка та/або текст."""
    image_url: str | None = None
    text: str | None = None
    days: tuple = ()          # розпарсені графіки за датами (schedule.DaySchedule) — для картинок, які малює бот


_TIME_RANGE = re.compile(r"\d{1,2}:\d{2}\s*[-–—]\s*\d{1,2}:\d{2}")
_BR = re.compile(r"<br\s*/?>\s*", re.IGNORECASE)


def normalize_blocks(text: str) -> str:
    """Рядки без зайвих пробілів; групи, розділені порожніми рядками, зберігаються (кілька порожніх → один)."""
    out: list[str] = []
    for line in text.replace("\xa0", " ").splitlines():
        line = line.strip()
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


# Рядок графіка: «Підчерга 1.1 – …» / «Черга 2: …».
_QUEUE_LINE = re.compile(r"^\s*(?:під)?черга\s+\d", re.IGNORECASE)
# Абзац-заголовок перед графіком: «За розпорядженням … графік … (ГПВ) на 21 листопада», «Години відсутності електроенергії:».
_HEADER_HINT = re.compile(r"розпорядж|графік|гпв|відсутност|відключен", re.IGNORECASE)
_MAX_HEADER_PARAGRAPHS = 3


def _paragraph_text(p) -> str:
    # Переноси рядків і відступи у вихідному HTML — це звичайні пробіли; розрив рядка дає лише <br>.
    inner = _BR.sub("\n", re.sub(r"\s+", " ", p.decode_contents()))
    return normalize_blocks(BeautifulSoup(inner, "html.parser").get_text())


def _is_schedule_paragraph(text: str) -> bool:
    return any(_TIME_RANGE.search(line) for line in text.splitlines()) and "черг" in text.casefold()


def _is_header_paragraph(text: str) -> bool:
    return (bool(text) and not _TIME_RANGE.search(text) and not any(_QUEUE_LINE.match(l) for l in text.splitlines())
            and bool(_HEADER_HINT.search(text)))


_SUBQUEUE = re.compile(r"^\s*(?:під)?черга\s+(\d\.\d)(?!\d)", re.IGNORECASE | re.MULTILINE)
_ALL_SUBQUEUES = frozenset(f"{n}.{m}" for n in range(1, 7) for m in (1, 2))
_MAX_GAP_PARAGRAPHS = 3   # між блоками графіка (напр. рядок з датою «27.11.2026 р.»)


def _schedule_block(items: list[str | None], start: int) -> tuple[int, int]:
    """Блок графіка навколо абзацу `start` з чергами: (перший, останній) індекси з заголовками перед ним."""
    end = start
    j = start + 1
    while j < len(items) and items[j] is not None:
        if items[j] == "":
            j += 1
            continue
        if not _is_schedule_paragraph(items[j]):
            break
        end = j
        j += 1

    first = start
    k = start - 1
    while k >= 0 and start - k <= _MAX_HEADER_PARAGRAPHS and items[k] is not None:
        if items[k] == "":
            k -= 1
            continue
        if not _is_header_paragraph(items[k]):
            break
        first = k
        k -= 1
    return first, end


def parse_text_schedule(html: str) -> str | None:
    """Текстовий графік (ГПВ) зі сторінки: заголовки і рядки черг з інтервалами часу (`00:00-04:00`).

    Сайт публікує графік у двох виглядах:
      * один `<p>` у `div.post`, рядки розділені `<br>`, групи черг — подвійним `<br>`;
      * кілька `<p>` поспіль: заголовок, «Години відсутності електроенергії:», далі по абзацу на групу черг.

    Блок графіка — суміжні абзаци з чергами плюс абзаци-заголовки безпосередньо перед ними.
    Беремо перший блок. Якщо в ньому є не всі підчерги (напр. «Додаткові години відсутності електроенергії:
    Підчерга 6.1 …» над основним графіком, відокремлені `<hr>`), дописуємо наступні блоки, доки не будуть
    усі 12 підчерг.
    Повний графік першим (а нижче — старіша версія) береться сам, без старішої.
    Абзаци у результаті розділяються порожнім рядком, як і групи всередині одного абзацу.
    Розпізнаємо за змістом, а не за точним формулюванням, бо текст щодня трохи інший.
    """
    soup = BeautifulSoup(html, "html.parser")
    root = soup.select_one("div.post") or soup
    # <hr> — межа розділу сторінки (None). Повний графік через неї не продовжується; неповний (лише
    # «Додаткові години») — продовжується наступним блоком за однією межею.
    items: list[str | None] = [None if el.name == "hr" else _paragraph_text(el) for el in root.find_all(["p", "hr"])]

    start = next((i for i, t in enumerate(items) if t is not None and _is_schedule_paragraph(t)), None)
    if start is None:
        return None

    blocks = [_schedule_block(items, start)]
    covered = set(_SUBQUEUE.findall("\n".join(t for t in items[blocks[0][0]:blocks[0][1] + 1] if t)))
    # Формат без підчерг («Черга 1») — не знаємо, що таке «повний», тож лише перший блок.
    while covered and not _ALL_SUBQUEUES <= covered:
        # Наступний блок: шукаємо абзац із чергами далі, пропускаючи не більше кількох сторонніх абзаців
        # і однієї межі <hr> (сайт розділяє «Додаткові години» й основний графік саме нею).
        j, gap, hrs, nxt = blocks[-1][1] + 1, 0, 0, None
        while j < len(items):
            t = items[j]
            if t is None:
                hrs += 1
                if hrs > 1:
                    break
                j += 1
                continue
            if t and _is_schedule_paragraph(t):
                nxt = j
                break
            if t and not _is_header_paragraph(t):
                gap += 1
                if gap > _MAX_GAP_PARAGRAPHS:
                    break
            j += 1
        if nxt is None:
            break
        first, end = _schedule_block(items, nxt)
        blocks.append((max(first, blocks[-1][1] + 1), end))
        covered |= set(_SUBQUEUE.findall("\n".join(t for t in items[nxt:end + 1] if t)))

    parts = [t for first, end in blocks for t in items[first:end + 1] if t and (
        _is_schedule_paragraph(t) or _is_header_paragraph(t))]
    return "\n\n".join(parts)


def parse_image_url(html: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img"):
        if GRAPH_ALT_PATTERN.search(img.get("alt", "")) and img.get("src"):
            return urljoin(base_url.rstrip("/") + "/", img["src"])
    return None


def _unwrap(payload):
    if isinstance(payload, dict):
        for key in ("results", "items", "data", "houses"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return payload


def parse_options(payload) -> list[Option]:
    """Відповідь /settlements/ та /streets/: список {"id": ..., "text": ...} (формат select2)."""
    payload = _unwrap(payload)
    if not isinstance(payload, list):
        return []
    options: list[Option] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        oid, name = item.get("id"), item.get("text")
        if oid in (None, "") or not isinstance(name, str) or not name.strip():
            continue
        options.append(Option(str(oid), name.strip()))
    return options


def parse_houses(payload) -> list[str]:
    """Відповідь /houses/{streetId}: список номерів будинків (рядки/числа; словники теж терпимо)."""
    payload = _unwrap(payload)
    if not isinstance(payload, list):
        return []
    houses: list[str] = []
    for item in payload:
        if isinstance(item, dict):
            item = item.get("text") or item.get("name") or item.get("id")
        value = str(item).strip() if item not in (None, "") else ""
        if value and value not in houses:
            houses.append(value)
    return houses


# Латинські літери, схожі на кириличні: "12A" і "12А" для людини — той самий будинок.
_LOOKALIKE = str.maketrans({
    "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "i": "і", "k": "к",
    "m": "м", "o": "о", "p": "р", "t": "т", "x": "х", "y": "у",
})


def house_key(value: str) -> str:
    return value.strip().casefold().translate(_LOOKALIKE).replace(" ", "")


def match_house(houses: list[str], typed: str) -> str | None:
    """Точний збіг (без урахування регістру та латиниці/кирилиці). Повертає написання, як на сайті."""
    key = house_key(typed)
    return next((h for h in houses if house_key(h) == key), None)


def suggest_houses(houses: list[str], typed: str, limit: int = 8) -> list[str]:
    """Схожі будинки: спершу ті, що починаються з введеного, потім із тим самим числом (12 → 12А, 12/1)."""
    key = house_key(typed)
    if not key:
        return []
    found = [h for h in houses if house_key(h).startswith(key)]
    if not found:
        digits = re.match(r"\d+", key)
        if digits:
            found = [h for h in houses if re.match(r"\d+", house_key(h)) and
                     re.match(r"\d+", house_key(h)).group() == digits.group()]
    return found[:limit]


class HoeClient:
    def __init__(
        self,
        base_url: str = "https://hoe.com.ua",
        *,
        timeout: float = 15,
        retries: int = 3,
        retry_delay: float = 5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self.retry_delay = retry_delay
        self._client = client or httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _ajax_headers(self) -> dict[str, str]:
        """Заголовки, з якими сайт сам робить AJAX-запити зі сторінки форми."""
        return {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self.base_url + FORM_PATH,
        }

    async def fetch_events_html(self, street_id: str, house: str) -> str:
        """POST на /shutdown-events (як це робить сайт через AJAX) з ретраями та backoff."""
        last_error = "порожня відповідь"
        for attempt in range(self.retries):
            try:
                r = await self._client.post(
                    self.base_url + EVENTS_PATH,
                    data={"streetId": street_id, "house": house},
                    headers=self._ajax_headers(),
                )
                r.raise_for_status()
                if r.text.strip():
                    return r.text
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self.retries - 1:
                delay = self.retry_delay * (2**attempt)
                log.warning("hoe.com.ua: спроба %d/%d не вдалась (%s), чекаю %.0fс",
                            attempt + 1, self.retries, last_error, delay)
                await asyncio.sleep(delay)
        raise HoeUnavailable(last_error)

    async def fetch_graphs(self) -> Graphs | None:
        """Один запит до сторінки графіків → картинка і/або текст. None — сторінка недоступна."""
        try:
            r = await self._client.get(self.base_url + GRAPH_PAGE_PATH)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("Не вдалось завантажити сторінку графіків: %s", exc)
            return None
        try:
            from .schedule import parse_days
            days = tuple(parse_days(r.text))
        except Exception:
            log.exception("Не вдалось розібрати графіки за датами")
            days = ()
        return Graphs(image_url=parse_image_url(r.text, self.base_url), text=parse_text_schedule(r.text), days=days)

    async def download(self, url: str) -> bytes:
        r = await self._client.get(url)
        r.raise_for_status()
        return r.content

    async def _ajax_json(self, path: str, params: dict | None = None):
        """GET → JSON з однією повторною спробою (це інтерактивні запити, довго чекати не варто)."""
        last: Exception | None = None
        for attempt in range(2):
            try:
                r = await self._client.get(self.base_url + path, params=params, headers=self._ajax_headers())
                r.raise_for_status()
                return r.json()
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                if attempt == 0:
                    await asyncio.sleep(min(1.0, self.retry_delay))
        raise SearchUnavailable(f"{path}: {last}")

    async def search_settlements(self, query: str) -> list[Option]:
        data = await self._ajax_json("/settlements/", {"term": query, "_type": "query", "q": query})
        return parse_options(data)

    async def search_streets(self, settlement_id: str, query: str) -> list[Option]:
        data = await self._ajax_json(f"/streets/{quote(str(settlement_id), safe='')}",
                                     {"term": query, "_type": "query", "q": query})
        return parse_options(data)

    async def get_houses(self, street_id: str) -> list[str]:
        data = await self._ajax_json(f"/houses/{quote(str(street_id), safe='')}")
        return parse_houses(data)