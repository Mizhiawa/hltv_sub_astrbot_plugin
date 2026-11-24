"""跨平台安全的 JSON 原子读写工具（兼容 Linux Ubuntu Docker 与 Windows）。"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .log_compat import logger


@dataclass(frozen=True)
class JsonLoadResult:
    success: bool
    data: Any
    error: Exception | None = None
    backup_path: Path | None = None
    missing: bool = False


def load_json(
    path: str | Path,
    *,
    expected_type: type = dict,
    default: Any = None,
    missing_ok: bool = False,
    backup_on_error: bool = False,
) -> JsonLoadResult:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, expected_type):
            raise TypeError(
                f"JSON 顶层应为 {expected_type.__name__}，实际是 {type(data).__name__}"
            )
        return JsonLoadResult(True, data)
    except FileNotFoundError as exc:
        return JsonLoadResult(
            missing_ok,
            copy.deepcopy(default),
            None if missing_ok else exc,
            missing=True,
        )
    except (OSError, ValueError, TypeError) as exc:
        backup = None
        if backup_on_error and path.is_file():
            try:
                backup = path.with_name(f"{path.name}.corrupt.{time.time_ns()}")
                shutil.copy2(path, backup)
            except OSError as backup_error:
                logger.warning(f"[HLTV] 损坏的 JSON 备份失败 {path}: {backup_error}")
                backup = None
        return JsonLoadResult(False, copy.deepcopy(default), exc, backup)


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """临时文件 + fsync + os.replace 原子写入，防止容器重启/断电产生截断文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            with suppress(OSError):
                os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt" and hasattr(os, "O_RDONLY"):
            with suppress(OSError):
                directory_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def atomic_write_json(
    path: str | Path,
    data: Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
    expected_type: type | None = None,
) -> None:
    """原子写 JSON 文件。"""
    if expected_type is not None and not isinstance(data, expected_type):
        raise TypeError(f"JSON 顶层应为 {expected_type.__name__}")
    atomic_write_bytes(
        path, json.dumps(data, ensure_ascii=ensure_ascii, indent=indent).encode("utf-8")
    )
