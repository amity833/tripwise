from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .dates import days_between, is_real_date

_TABLE: dict[str, list[dict[str, Any]]] = json.loads((Path(__file__).parent / "hotels.json").read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Hotel:
    id: str
    city: str
    name: str
    stars: int
    nightlyUsd: int
    checkIn: str
    checkOut: str
    nights: int
    totalUsd: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_hotel(city: str, idx: int, check_in: str, check_out: str) -> Hotel | None:
    rows = _TABLE.get(city, [])
    if not 0 <= idx < len(rows) or not is_real_date(check_in) or not is_real_date(check_out):
        return None
    h = rows[idx]
    nights = days_between(check_in, check_out)
    if nights < 1:
        return None
    return Hotel(
        id=f"HT-{city}-{idx}-{check_in.replace('-', '')}-{check_out.replace('-', '')}", city=city,
        name=h["name"], stars=h["stars"], nightlyUsd=h["nightlyUsd"], checkIn=check_in, checkOut=check_out,
        nights=nights, totalUsd=h["nightlyUsd"] * nights,
    )


def search_hotels(city: str, check_in: str, check_out: str) -> list[Hotel]:
    return [h for i in range(len(_TABLE.get(city, []))) if (h := make_hotel(city, i, check_in, check_out))]


_ID = re.compile(r"HT-([A-Z]{3})-(\d)-(\d{8})-(\d{8})", re.ASCII)


def _iso(s: str) -> str:
    return f"{s[:4]}-{s[4:6]}-{s[6:]}"


def get_hotel(hotel_id: str) -> Hotel | None:
    m = _ID.fullmatch(hotel_id)
    if not m:
        return None
    return make_hotel(m.group(1), int(m.group(2)), _iso(m.group(3)), _iso(m.group(4)))
