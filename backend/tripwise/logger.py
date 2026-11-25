import json
from datetime import UTC, datetime
from typing import Any


def log(level: str, msg: str, **fields: Any) -> None:
    t = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    print(json.dumps({"t": t, "level": level, "msg": msg, **fields}, default=str), flush=True)
