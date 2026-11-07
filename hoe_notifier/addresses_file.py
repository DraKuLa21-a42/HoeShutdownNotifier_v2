"""Запасний варіант керування адресами: файл addresses.toml."""
from __future__ import annotations

import logging
from pathlib import Path

from .queues import QUEUES, normalize_queue

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

log = logging.getLogger(__name__)


def load_addresses(path: Path, *, default_silent: bool = True, default_graphs: bool = False,
                   available_channels: set[str] | None = None) -> list[dict]:
    if not path.exists():
        return []
    with path.open("rb") as f:
        data = tomllib.load(f)

    entries: list[dict] = []
    for i, raw in enumerate(data.get("address", []), start=1):
        try:
            channel = str(raw.get("send_to", "tg")).lower()
            if channel not in {"tg", "slack"}:
                raise ValueError(f"send_to має бути tg або slack, а не {channel!r}")
            if available_channels is not None and channel not in available_channels:
                log.warning("addresses.toml #%d: канал %s не налаштований, запис пропущено", i, channel)
                continue
            street_id, house, target = str(raw["street_id"]), str(raw["house"]), str(raw["target"])
            queue = None
            if raw.get("queue") not in (None, ""):
                queue = normalize_queue(raw["queue"])
                if queue is None:
                    raise ValueError(f"queue має бути однією з {QUEUES[0]} … {QUEUES[-1]}, а не {raw['queue']!r}")
            entries.append({
                "channel": channel, "target": target, "street_id": street_id, "house": house,
                "label": str(raw.get("name") or f"вул. #{street_id}, {house}"),
                "queue": queue,
                "events": bool(raw.get("events", True)),
                "graphs": bool(raw.get("graphs", default_graphs)),
                "silent": bool(raw.get("silent", default_silent)),
            })
        except (KeyError, ValueError) as exc:
            log.error("addresses.toml, запис #%d некоректний (%s) — пропущено", i, exc)
    return entries
