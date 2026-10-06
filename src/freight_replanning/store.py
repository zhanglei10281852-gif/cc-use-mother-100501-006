"""JSON 文件持久化：独占锁 + 原子替换，保证并发与崩溃安全。

所有写操作都通过 ``transact`` 在排他锁内完成"读取-修改-写入"，
因此同一主机上的多线程/多进程不会互相覆盖，也不会出现重复占位。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

try:  # POSIX 文件锁；Windows 等平台退化为进程内锁
    import fcntl
except ImportError:  # pragma: no cover - 仅非 POSIX 平台
    fcntl = None  # type: ignore[assignment]


class JsonStore:
    """单文件状态存储。"""

    def __init__(self, directory: str | os.PathLike[str]):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state_path = self.directory / "state.json"
        self.lock_path = self.directory / "state.lock"
        self.lock_path.touch(exist_ok=True)
        self._local_lock = threading.RLock()

    def load(self) -> dict:
        if not self.state_path.exists():
            return {"version": 0}
        with self.state_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def save(self, state: dict) -> None:
        """先写临时文件再原子替换，避免崩溃留下半个文件。"""
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def transact(self, fn: Callable[[dict], Any]) -> Any:
        """在独占锁内执行事务；fn 抛异常则不落盘，状态自动回滚。"""
        with self._local_lock:
            with self.lock_path.open("r+") as lock_fh:
                if fcntl is not None:
                    fcntl.flock(lock_fh, fcntl.LOCK_EX)
                try:
                    state = self.load()
                    result = fn(state)
                    state["version"] = int(state.get("version", 0)) + 1
                    self.save(state)
                    return result
                finally:
                    if fcntl is not None:
                        fcntl.flock(lock_fh, fcntl.LOCK_UN)
