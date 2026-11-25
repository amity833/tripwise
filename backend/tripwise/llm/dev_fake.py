from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..core.models import LlmMessage
from ..core.trips import describe_trip
from ..data.airports import AIRPORTS, resolve_airport
from ..data.dates import add_days, today_iso, utcnow
from .base import LlmRequest, LlmResponse

_ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}
_PICK = re.compile(r"\b(book|take|pick|choose|propose)\b.*\b(first|second|third|fourth|1st|2nd|3rd|4th|[1-4])\b")
_BOOK = re.compile(r"\b(book|take|pick|choose|propose|go with|i'll take)\b")
_TRIPS = re.compile(r"\b(my trips|my bookings|trips)\b")
_TRAVEL = re.compile(r"\b(flights?|fly|flying|trip|travel|need to be|business|economy|class)\b")


class DevFakeLlm:
    """
    Keyword-driven stand-in for a real model, so the app demos the full flow with no API key.
    Understands: "flights to Berlin [from Tel Aviv] [business]", "book the second one", "my trips".
    """

    def __init__(self, tz: str = "UTC", now: Callable[[], datetime] = utcnow) -> None:
        self._tz, self._now, self._ids = tz, now, 0

    def _id(self) -> str:
        self._ids += 1
        return f"dev{self._ids}"

    async def generate(self, req: LlmRequest) -> LlmResponse:
        msgs = req.messages
        last = msgs[-1]
        if last["role"] == "tool":
            return self._after_tools(msgs)

        text = last["text"] if last["role"] == "user" else ""
        lower = text.lower()
        if _TRIPS.search(lower):
            return LlmResponse(tool_calls=[{"id": self._id(), "name": "list_bookings", "args": {}}])

        pick = _PICK.search(lower)
        if pick:
            return self._propose(msgs, pick.group(2))
        if _BOOK.search(lower):
            n = self._pick_by_name(msgs, lower)
            if n:
                return self._propose(msgs, str(n))
            return LlmResponse(text='Which flight? Say a number (1-4), an airline name, or "the cheapest".')

        cities = [a for a in AIRPORTS if a.city.lower() in lower or re.search(rf"\b{a.code.lower()}\b", lower)]
        dest = (
            next((c for c in cities if re.search(rf"(to|in)\s+{re.escape(c.city.lower())}", lower)), None)
            or next((c for c in cities if c.code != "TLV"), None)
            or (cities[0] if cities else None)
        )
        if _TRAVEL.search(lower) and dest:
            origin = next(
                (c for c in cities if c.code != dest.code and re.search(rf"from\s+{re.escape(c.city.lower())}", lower)),
                None,
            ) or resolve_airport("TLV")
            assert origin is not None
            today = today_iso(self._now(), self._tz)
            iso = re.findall(r"\d{4}-\d{2}-\d{2}", lower)
            date = iso[0] if iso else add_days(today, 14)
            ret = iso[1] if len(iso) > 1 else add_days(date, 3)
            cabin = "business" if "business" in lower else "economy"
            args = {"origin": origin.code, "destination": dest.code, "date": date, "return_date": ret, "cabin": cabin}
            return LlmResponse(tool_calls=[{"id": self._id(), "name": "search_flights", "args": args}])
        return LlmResponse(
            text='I can search flights and hotels and check them against policy. Try: "I need a flight to Berlin from Tel Aviv" '
            '(add dates like 2026-12-10 2026-12-13, or say "business").'
        )

    def _pick_by_name(self, msgs: list[LlmMessage], lower: str) -> int | None:
        f = self._last_result(msgs, "search_flights")
        outbound = (f or {}).get("outbound") or []
        if re.search(r"\b(cheap\w*|lowest)\b", lower) and outbound:
            return min(range(len(outbound)), key=lambda i: outbound[i]["priceUsd"]) + 1
        if re.search(r"\b(earliest|first flight)\b", lower) and outbound:
            return min(range(len(outbound)), key=lambda i: outbound[i]["departTime"]) + 1
        for i, x in enumerate(outbound):
            if x["airline"].lower() in lower:
                return i + 1
        return None

    @staticmethod
    def _last_result(msgs: list[LlmMessage], name: str) -> dict[str, Any] | None:
        for m in reversed(msgs):
            if m["role"] == "tool" and m["name"] == name:
                return m["result"]
        return None

    def _after_tools(self, msgs: list[LlmMessage]) -> LlmResponse:
        last = msgs[-1]
        r: dict[str, Any] = last["result"]
        if r.get("error"):
            extra = (f" {r['suggestion']}" if r.get("suggestion") else "") + (" " + " ".join(r["reasons"]) if r.get("reasons") else "")
            return LlmResponse(text=f"Sorry, that didn't work: {r['error']}{extra}")
        name = last["name"]
        if name == "search_flights":
            o = r["outbound"]
            # Chain into a hotel search for the stay, then summarise.
            inbound = r.get("inbound")
            check_out = inbound[0]["date"] if inbound else add_days(o[0]["date"], 3)
            args = {"city": o[0]["destination"], "check_in": o[0]["date"], "check_out": check_out}
            return LlmResponse(tool_calls=[{"id": self._id(), "name": "search_hotels", "args": args}])
        if name == "search_hotels":
            f = self._last_result(msgs, "search_flights") or {}
            lines = [
                f"{i + 1}. {x['airline']} {x['departTime']}→{x['arriveTime']} ({x['durationMin'] / 60:.1f}h) {x['cabin']} ${x['priceUsd']}"
                for i, x in enumerate(f["outbound"])
            ]
            hotels = [f"• {h['name']} {'★' * h['stars']} ${h['nightlyUsd']}/night" for h in r["hotels"]]
            res = f["resolved"]
            ret = f", returning {res['return']}" if res.get("return") else ""
            return LlmResponse(
                text=f"Flights {res['origin']} → {res['destination']}, departing {res['departure']}{ret}:\n" + "\n".join(lines)
                + "\n\nHotels:\n" + "\n".join(hotels) + '\n\nSay "book the second one" and I\'ll prepare a proposal for you to confirm.'
            )
        if name == "propose_booking":
            note = " Note: " + " ".join(r["warnings"]) if r.get("warnings") else ""
            return LlmResponse(text=f"I've prepared a proposal. Nothing is booked until you confirm.{note}")
        if name == "list_bookings":
            b = r["bookings"]
            if not b:
                return LlmResponse(text="You have no confirmed trips yet.")
            return LlmResponse(text="Your trips:\n\n" + "\n\n".join(describe_trip(x["reference"], x) for x in b))
        return LlmResponse(text="Done.")

    def _propose(self, msgs: list[LlmMessage], which: str) -> LlmResponse:
        n = _ORDINALS.get(which) or int(which)
        f = self._last_result(msgs, "search_flights")
        h = self._last_result(msgs, "search_hotels")
        outbound = (f or {}).get("outbound") or []
        if not f or not 1 <= n <= len(outbound):
            return LlmResponse(text="Search for flights first, then tell me which option to book.")
        inbound = f.get("inbound") or []
        hotels = (h or {}).get("hotels") or []
        args: dict[str, Any] = {"flight_id": outbound[n - 1]["id"]}
        if n <= len(inbound):
            args["return_flight_id"] = inbound[n - 1]["id"]
        if len(hotels) > 1:
            args["hotel_id"] = hotels[1]["id"]
        return LlmResponse(tool_calls=[{"id": self._id(), "name": "propose_booking", "args": args}])
