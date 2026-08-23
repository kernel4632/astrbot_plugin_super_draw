"""一次性临时图片文件。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


class Files:
    def save(self, data: bytes) -> str | None:
        if not data:
            return None
        fd, name = tempfile.mkstemp(prefix="super_draw_", suffix=".png")
        os.close(fd)
        try:
            Path(name).write_bytes(data)
            return name
        except OSError:
            self.remove(name)
            return None

    def remove(self, path: str | os.PathLike[str]) -> bool:
        try:
            Path(path).unlink()
            return True
        except OSError:
            return False


files = Files()

__all__ = ["files"]
