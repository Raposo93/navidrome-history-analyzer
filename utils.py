from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def norm(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000_000:
            timestamp /= 1_000_000
        elif timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone()
        except (ValueError, OSError, OverflowError):
            return None

    text = str(value).strip()
    if not text:
        return None

    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            return parse_dt(float(text))
        except ValueError:
            pass

    candidates = [
        text,
        text.replace("Z", "+00:00"),
        text.replace(" ", "T", 1),
        text.replace(" ", "T", 1).replace("Z", "+00:00"),
    ]
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc).astimezone()
            return parsed.astimezone()
        except ValueError:
            continue

    for date_format in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    ):
        try:
            return (
                datetime.strptime(text, date_format)
                .replace(tzinfo=timezone.utc)
                .astimezone()
            )
        except ValueError:
            pass
    return None


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days} d {hours:02d} h {minutes:02d} min"
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min"


def pct(numerator: float, denominator: float) -> str:
    if not denominator:
        return "0,0%"
    return f"{100 * numerator / denominator:.1f}%".replace(".", ",")


def split_genres(value: str) -> set[str]:
    text = norm(value).lower()
    if not text:
        return set()
    return {
        item.strip()
        for item in re.split(r"[;,/|]+", text)
        if item.strip() and len(item.strip()) > 1
    }
