"""Особистий кабінет cabinet.hoe.com.ua: точна черга відключень за договором.

API (з'ясовано за запитами браузера):
  POST /Token  {"username", "password", "rememberme"}  → {"accessToken", "tokenType": "bearer", "expiresIn"}
  GET  /api/household-consumers/cabinet/get-by-user               → договори користувача (cabinetId)
  GET  /api/household-consumers/technical-data/get-oms-data/{id}  → {"shutdowns": [...], "shutdownQueues": [...]}
Решта запитів іде з `Authorization: Bearer <token>`. Токен живе добу, тому пароль зберігається (зашифрованим),
щоб сервіс міг сам перелогінитися.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

import httpx

from .db import CABINET_AUTH_FAILED, CabinetAccount, Database

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://cabinet.hoe.com.ua"
TOKEN_PATH = "/Token"
CONTRACTS_PATH = "/api/household-consumers/cabinet/get-by-user"
OMS_PATH = "/api/household-consumers/technical-data/get-oms-data/{cabinet_id}"
TOKEN_MARGIN = 300            # оновлюємо токен, якщо до кінця його дії лишилось менше стількох секунд
USER_AGENT = "Mozilla/5.0 (compatible; HoeShutdownNotifier/2.0)"


class CabinetError(RuntimeError):
    """Будь-яка помилка роботи з кабінетом."""


class CabinetAuthError(CabinetError):
    """Логін або пароль не підходять (або збережений пароль не вдалося розшифрувати)."""


class CabinetUnavailable(CabinetError):
    """Кабінет не відповів або повернув щось незрозуміле."""


class TokenRejected(CabinetError):
    """Сайт не прийняв токен (HTTP 401) — треба увійти заново."""


@dataclass(frozen=True)
class Contract:
    cabinet_id: int
    number: str
    status: str
    address: str


@dataclass(frozen=True)
class QueueEntry:
    settlement: str
    street: str
    house: str
    queue: str          # як на сайті, напр. "2.2"
    queue_gav: str      # черга графіка аварійних відключень

    @property
    def address(self) -> str:
        return ", ".join(p for p in (self.settlement, f"{self.street} {self.house}".strip()) if p)


@dataclass(frozen=True)
class OmsData:
    queues: list[QueueEntry]
    shutdowns: list = field(default_factory=list)


# --- шифрування ----------------------------------------------------------

class SecretBox:
    """Шифрування паролів і токенів ключем Fernet із CABINET_SECRET_KEY."""

    def __init__(self, key: str) -> None:
        from cryptography.fernet import Fernet
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        from cryptography.fernet import InvalidToken
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise CabinetAuthError("Не вдалося розшифрувати збережені дані (змінився CABINET_SECRET_KEY?)") from exc


def validate_key(key: str) -> None:
    """ValueError, якщо рядок не є ключем Fernet."""
    try:
        SecretBox(key)
    except Exception as exc:
        raise ValueError(str(exc)) from exc


# --- HTTP-клієнт ---------------------------------------------------------

class CabinetClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, *, timeout: float = 15, retries: int = 2,
                 retry_delay: float = 3, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.retries = max(1, retries)
        self.retry_delay = retry_delay
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, token: str | None = None) -> dict[str, str]:
        h = {"Accept": "application/json, text/plain, */*", "User-Agent": USER_AGENT,
             "Origin": self.base_url, "Referer": f"{self.base_url}/"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async def _request(self, method: str, path: str, *, token: str | None = None, json=None) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = await self._client.request(method, self.base_url + path, json=json,
                                                  headers=self._headers(token))
            except httpx.HTTPError as exc:
                last = exc
            else:
                if resp.status_code < 500:
                    return resp
                last = CabinetUnavailable(f"HTTP {resp.status_code}")
            if attempt + 1 < self.retries:
                await asyncio.sleep(self.retry_delay * (2 ** attempt))
        raise CabinetUnavailable(f"{method} {path}: {last}")

    @staticmethod
    def _json(resp: httpx.Response, what: str):
        try:
            return resp.json()
        except ValueError:
            raise CabinetUnavailable(f"{what}: відповідь не JSON (HTTP {resp.status_code})") from None

    async def login(self, login: str, password: str) -> tuple[str, int]:
        """(токен, unix-час закінчення дії)."""
        resp = await self._request("POST", TOKEN_PATH,
                                   json={"username": login, "password": password, "rememberme": True})
        if resp.status_code in (400, 401, 403):
            raise CabinetAuthError("Невірний логін або пароль")
        if resp.status_code != 200:
            raise CabinetUnavailable(f"Вхід: HTTP {resp.status_code}")
        data = self._json(resp, "Вхід")
        token = data.get("accessToken") if isinstance(data, dict) else None
        if not token:
            raise CabinetUnavailable("Вхід: у відповіді немає accessToken")
        expires_in = int(data.get("expiresIn") or 3600)
        return token, int(time.time()) + expires_in

    async def _get(self, token: str, path: str, what: str):
        resp = await self._request("GET", path, token=token)
        if resp.status_code == 401:
            raise TokenRejected(what)
        if resp.status_code != 200:
            raise CabinetUnavailable(f"{what}: HTTP {resp.status_code}")
        return self._json(resp, what)

    async def contracts(self, token: str) -> list[Contract]:
        data = await self._get(token, CONTRACTS_PATH, "Договори")
        if not isinstance(data, list):
            raise CabinetUnavailable("Договори: очікував список")
        return [
            Contract(cabinet_id=int(c["cabinetId"]), number=str(c.get("contractNumber") or ""),
                     status=str(c.get("contractStatusName") or ""), address=str(c.get("address") or ""))
            for c in data if isinstance(c, dict) and c.get("cabinetId") is not None
        ]

    async def oms_data(self, token: str, cabinet_id: int) -> OmsData:
        data = await self._get(token, OMS_PATH.format(cabinet_id=int(cabinet_id)), "Черга")
        if not isinstance(data, dict):
            raise CabinetUnavailable("Черга: очікував об'єкт")
        queues = [
            QueueEntry(settlement=str(q.get("settlement") or ""), street=str(q.get("street") or ""),
                       house=str(q.get("house") or ""), queue=str(q.get("queue") or "").strip(),
                       queue_gav=str(q.get("queueGAV") or "").strip())
            for q in (data.get("shutdownQueues") or []) if isinstance(q, dict)
        ]
        shutdowns = data.get("shutdowns") or []
        if shutdowns:
            # Формат ще не бачили наживо — логуємо, щоб пізніше підключити як джерело відключень.
            log.info("Кабінет %s: shutdowns = %s", cabinet_id, shutdowns)
        return OmsData(queues=queues, shutdowns=shutdowns if isinstance(shutdowns, list) else [])


# --- зіставлення адрес ---------------------------------------------------

def _norm_house(value: str) -> str:
    table = str.maketrans({"a": "а", "b": "б", "e": "е", "i": "і", "o": "о", "c": "с", "k": "к"})
    return re.sub(r"[\s\-]+", "", value or "").lower().translate(table)


def house_matches(a: str, b: str) -> bool:
    return bool(a and b) and _norm_house(a) == _norm_house(b)


_STREET_NOISE = re.compile(r"\b(вул|вулиця|пров|провулок|просп|проспект|бульв|бульвар|пл|площа|туп|тупик|"
                           r"шосе|узвіз|проїзд|мкр)\b\.?", re.IGNORECASE)


def street_matches(cabinet_street: str, our_text: str) -> bool:
    """Груба перевірка: хоч одне значуще слово вулиці з кабінету є в нашій назві.

    Назви на двох сайтах записані по-різному («вул. Мирного П.» / «вул. Панаса Мирного»), тому порівнюємо слова.
    Якщо значущих слів немає — не заважаємо (True).
    """
    words = [w for w in re.findall(r"[^\W\d_]{4,}", _STREET_NOISE.sub(" ", cabinet_street or ""))]
    if not words:
        return True
    ours = (our_text or "").lower()
    return any(w.lower() in ours for w in words)


def pick_queue(oms: OmsData, house: str | None = None) -> QueueEntry | None:
    """Запис черги для будинку; якщо будинок не збігся — перший запис."""
    if not oms.queues:
        return None
    if house:
        for q in oms.queues:
            if house_matches(q.house, house):
                return q
    return oms.queues[0]


def mask_login(login: str) -> str:
    login = login or ""
    if "@" in login:
        name, _, domain = login.partition("@")
        return f"{name[:2]}***@{domain}"
    return f"{login[:3]}***{login[-2:]}" if len(login) > 5 else "***"


# --- сервіс: акаунти, токени, повторний вхід ------------------------------

class CabinetService:
    def __init__(self, db: Database, client: CabinetClient, box: SecretBox) -> None:
        self.db = db
        self.client = client
        self.box = box

    async def aclose(self) -> None:
        await self.client.aclose()

    async def sign_in(self, owner_id: int, login: str, password: str) -> CabinetAccount:
        """Перевіряє логін/пароль на сайті й зберігає акаунт (або оновлює пароль наявного)."""
        token, exp = await self.client.login(login, password)
        acc = self.db.upsert_cabinet_account(owner_id, login, self.box.encrypt(password))
        self.db.save_cabinet_token(acc.id, self.box.encrypt(token), exp)
        return self.db.get_cabinet_account(acc.id)

    async def _token(self, acc: CabinetAccount, *, fresh: bool = False) -> str:
        current = self.db.get_cabinet_account(acc.id) or acc
        if not fresh and current.token_enc and (current.token_exp or 0) > time.time() + TOKEN_MARGIN:
            try:
                return self.box.decrypt(current.token_enc)
            except CabinetAuthError:
                pass   # токен не розшифровується — просто увійдемо заново
        try:
            password = self.box.decrypt(current.password_enc)
            token, exp = await self.client.login(current.login, password)
        except CabinetAuthError as exc:
            self.db.set_cabinet_status(acc.id, CABINET_AUTH_FAILED, str(exc))
            raise
        self.db.save_cabinet_token(acc.id, self.box.encrypt(token), exp)
        return token

    async def _call(self, acc: CabinetAccount, func, *args):
        token = await self._token(acc)
        try:
            return await func(token, *args)
        except TokenRejected:
            token = await self._token(acc, fresh=True)
            try:
                return await func(token, *args)
            except TokenRejected:
                raise CabinetUnavailable("Сайт не приймає щойно отриманий токен") from None

    async def contracts(self, acc: CabinetAccount) -> list[Contract]:
        return await self._call(acc, self.client.contracts)

    async def oms(self, acc: CabinetAccount, cabinet_id: int) -> OmsData:
        return await self._call(acc, self.client.oms_data, cabinet_id)