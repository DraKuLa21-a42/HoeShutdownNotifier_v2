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

    async def fetch_graph_url(self) -> str | None:
        """URL актуального зображення з графіком (ГПВ) або None."""
        try:
            r = await self._client.get(self.base_url + GRAPH_PAGE_PATH)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("Не вдалось завантажити сторінку графіків: %s", exc)
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        for img in soup.find_all("img"):
            if GRAPH_ALT_PATTERN.search(img.get("alt", "")) and img.get("src"):
                return urljoin(self.base_url + "/", img["src"])
        return None

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