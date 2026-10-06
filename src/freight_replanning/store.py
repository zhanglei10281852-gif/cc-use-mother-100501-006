"""持久化：追加式 JSONL 日志 + 压缩重写。

服务重启后通过重放日志恢复全部状态；``compact`` 把当前内存状态
整体重写为新日志，避免日志无限增长。日志目录可加文件锁，防止两个
进程同时写同一数据目录。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Iterator

try:  # POSIX
    import fcntl

    def _lock(file_obj) -> None:
        fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX)

    def _unlock(file_obj) -> None:
        fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)

except ImportError:  # pragma: no cover - Windows 退化路径
    try:
        import msvcrt

        def _lock(file_obj) -> None:
            msvcrt.locking(file_obj.fileno(), msvcrt.LK_LOCK, 1)

        def _unlock(file_obj) -> None:
            file_obj.seek(0)
            msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)

    except ImportError:  # pragma: no cover

        def _lock(file_obj) -> None:  # noqa: D103
            return None

        def _unlock(file_obj) -> None:
            return None


class Journal:
    """单文件追加日志，每条记录一行 JSON：{"seq", "type", "payload"}。"""

    def __init__(self, data_dir: str | Path) -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "journal.jsonl"
        self._lock_path = self._dir / "journal.lock"
        self._lock_file = open(self._lock_path, "a+b")
        _lock(self._lock_file)
        self._seq = 0
        for record in self._read_all():
            self._seq = max(self._seq, int(record.get("seq", 0)))

    @property
    def path(self) -> Path:
        return self._path

    def _read_all(self) -> Iterator[dict[str, Any]]:
        if not self._path.exists():
            return
        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)

    def replay(self) -> Iterator[tuple[str, dict[str, Any]]]:
        for record in sorted(self._read_all(), key=lambda r: int(r.get("seq", 0))):
            yield str(record["type"]), record["payload"]

    def append(self, record_type: str, payload: dict[str, Any]) -> int:
        self._seq += 1
        record = {"seq": self._seq, "type": record_type, "payload": payload}
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        return self._seq

    def compact(self, records: Iterator[tuple[str, dict[str, Any]]]) -> None:
        """用当前状态的整体快照重写日志。"""
        tmp_path = self._dir / "journal.jsonl.tmp"
        seq = 0
        with open(tmp_path, "w", encoding="utf-8") as fh:
            for record_type, payload in records:
                seq += 1
                record = {"seq": seq, "type": record_type, "payload": payload}
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
                fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, self._path)
        self._seq = seq

    def close(self) -> None:
        try:
            _unlock(self._lock_file)
        finally:
            self._lock_file.close()

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
