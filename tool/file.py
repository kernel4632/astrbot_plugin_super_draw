"""
图片文件保存和缓存清理工具。

生图接口返回的是 bytes，聊天平台通常更适合发送本地图片文件，所以 main.py 会先调用 saveImage() 把 bytes 保存到缓存目录。
缓存目录会越用越大，因此插件启动后还会定时调用 cleanCache()，只保留最新的一批图片。

这个文件不懂 AstrBot，不懂模型，也不懂用户；它只处理“文件”这一件小事。

调用示例：
    path = saveImage(cacheDir, imageBytes, "png")       # 保存原图为 png
    path = saveImage(cacheDir, imageBytes, "jpeg", 85)  # 转成 jpeg，适合节省带宽
    path = saveImage(cacheDir, imageBytes, "webp", 90)  # 转成 webp，适合现代平台
    count = await cleanCache(cacheDir, 200)              # 只保留最新 200 个文件
"""

from __future__ import annotations

import hashlib  # 用图片内容生成短哈希，让文件名可追踪且不容易重复
import io  # Pillow 需要从内存 bytes 读取图片
import os  # 读取文件修改时间，用于判断哪些缓存最旧
import time  # 文件名里加入时间戳，方便按生成时间排序
from pathlib import Path
from typing import Literal

try:
    from PIL import Image  # Pillow 负责把 PNG 转成 JPEG/WebP
except ImportError:
    Image = None


Format = Literal["png", "webp", "jpeg"]  # 插件允许保存的图片格式，和 _conf_schema.json 保持一致


def saveImage(cacheDir: Path, imageBytes: bytes, fmt: Format = "png", quality: int = 90) -> str | None:
    """把图片 bytes 保存成真实文件；成功返回路径，失败返回 None。"""

    if not imageBytes:
        return None

    try:
        cacheDir.mkdir(parents=True, exist_ok=True)
        filePath = _newImagePath(cacheDir, imageBytes, fmt)

        if fmt == "png":
            filePath.write_bytes(imageBytes)
            return str(filePath)

        if Image is None:
            fallbackPath = filePath.with_suffix(".png")
            fallbackPath.write_bytes(imageBytes)
            return str(fallbackPath)

        image = Image.open(io.BytesIO(imageBytes))
        if fmt == "jpeg" and image.mode in ("RGBA", "P"):
            image = image.convert("RGB")  # JPEG 不支持透明通道，转 RGB 后才能保存
        image.save(filePath, format="JPEG" if fmt == "jpeg" else "WEBP", quality=quality)
        return str(filePath)
    except Exception:
        return None


def _newImagePath(cacheDir: Path, imageBytes: bytes, fmt: str) -> Path:
    """生成缓存文件路径；时间戳方便看生成顺序，哈希方便避免同秒重名。"""

    imageHash = hashlib.md5(imageBytes).hexdigest()[:10]
    fileName = f"gen_{int(time.time())}_{imageHash}.{fmt}"
    return cacheDir / fileName


async def cleanCache(cacheDir: Path, maxCount: int) -> int:
    """清理缓存目录，只保留最新 maxCount 个文件，并返回删除数量。"""

    if not cacheDir.exists():
        return 0

    files = [(path, os.path.getmtime(path)) for path in cacheDir.iterdir() if path.is_file()]
    files.sort(key=lambda item: item[1])

    if len(files) <= maxCount:
        return 0

    deleted = 0
    for path, _ in files[: len(files) - maxCount]:
        try:
            path.unlink()
            deleted += 1
        except OSError:
            pass

    return deleted
