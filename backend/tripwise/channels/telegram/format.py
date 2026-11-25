from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ...core.models import Proposal
from ...core.proposals import reference
from ...data.dates import label

TG_LIMIT = 4096
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")


def escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_telegram_html(text: str) -> str:
    """Escape everything, then turn **bold** into <b>. LLM text can never inject tags."""
    return _BOLD.sub(r"<b>\1</b>", escape_html(text))


def _utf16_len(s: str) -> int:
    # Telegram (like JS) measures message length in UTF-16 code units, so an emoji counts as 2.
    return len(s.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = TG_LIMIT, render: Callable[[str], str] = to_telegram_html) -> list[str]:
    """
    Split raw text into chunks whose RENDERED form fits the limit. Splitting happens before rendering,
    so tags are always balanced, and it works on code points so an emoji pair is never cut.
    """
    out: list[str] = []
    rest = text
    while rest:
        n = min(len(rest), limit)
        while n > 1 and _utf16_len(render(rest[:n])) > limit:
            n = int(n * 0.9)
        if n < len(rest):
            window = rest[:n]
            cut = max(window.rfind("\n"), window.rfind(" "))
            if cut > n * 0.5:
                n = cut + 1
        chunk = rest[:n].rstrip()
        if chunk:
            out.append(chunk)
        rest = rest[n:]
    return out or [""]


_STATUS = {
    "proposed": "🟡 PROPOSED", "confirmed": "✅ BOOKED", "cancelled": "❌ CANCELLED", "expired": "⌛ EXPIRED", "superseded": "↩️ REPLACED",
}


def render_proposal(p: Proposal) -> dict[str, Any]:
    """Returns {"text": str, "keyboard": [[{text, callback_data}]] | None}."""
    flights: list[dict[str, Any]] = p.payload.get("flights") or []
    hotel: dict[str, Any] | None = p.payload.get("hotel")
    policy: dict[str, Any] = p.payload.get("policy") or {}
    ref = f" · ref {reference(p.token)}" if p.status == "confirmed" else ""
    lines = [f"<b>{_STATUS[p.status]}</b>{ref}", ""]
    for f in flights:
        lines.append(
            f"✈️ {escape_html(f['origin'])} → {escape_html(f['destination'])} · {label(f['date'])} {f['departTime']}→{f['arriveTime']}\n"
            f"    {escape_html(f['airline'])}, {f['cabin']}, ${f['priceUsd']}"
        )
    if hotel:
        lines.append(
            f"🏨 {escape_html(hotel['name'])} {'★' * hotel['stars']}\n"
            f"    {label(hotel['checkIn'])} → {label(hotel['checkOut'])} ({hotel['nights']} nights), ${hotel['totalUsd']}"
        )
    lines += ["", f"<b>Total: ${p.payload.get('totalUsd')}</b>"]
    lines.append(
        f"⚠️ Policy warning: {escape_html(' '.join(policy.get('reasons', [])))}" if policy.get("verdict") == "warn" else "✅ Within company policy"
    )
    keyboard = None
    if p.status == "proposed":
        keyboard = [[
            {"text": "✅ Confirm", "callback_data": f"c:{p.token}"},
            {"text": "✏️ Change", "callback_data": f"m:{p.token}"},
            {"text": "❌ Cancel", "callback_data": f"x:{p.token}"},
        ]]
    return {"text": "\n".join(lines), "keyboard": keyboard}
