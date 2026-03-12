import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EventLogger:
    path: Path

    def append(
        self,
        *,
        event_type: str,
        actor: str,
        request_id: str,
        target: str,
        outcome: str,
        summary: str,
        extra: dict[str, Any] | None = None,
    ):
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "actor": actor,
            "request_id": request_id,
            "target": target,
            "outcome": outcome,
            "summary": summary,
        }
        if extra:
            record.update(extra)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="ascii") as handle:
            handle.write(json.dumps(record, sort_keys=True))
            handle.write("\n")
