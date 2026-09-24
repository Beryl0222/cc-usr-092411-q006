"""追加式事件日志：JSONL 持久化，启动时全量回放重建状态。"""

from __future__ import annotations

import json
from pathlib import Path


class EventStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._events: list[dict] = []
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._events.append(json.loads(line))
        self._handle = self._path.open("a", encoding="utf-8")

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    def append(self, event: dict) -> None:
        self._handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._handle.flush()
        self._events.append(event)

    def close(self) -> None:
        self._handle.close()
