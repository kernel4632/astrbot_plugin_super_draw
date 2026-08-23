"""从 AstrBot 消息中收集图片字节。"""

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import urlparse

try:
    import astrbot.api.message_components as Comp
except ImportError:  # 允许在没有 AstrBot 的环境中导入和测试
    Comp = None


class Images:
    async def collect(self, event: Any) -> list[bytes]:
        msg = getattr(event, "message_obj", None)
        message = getattr(msg, "message", None) if msg else None
        if not message:
            return []

        result: list[bytes] = []
        forwards: set[str] = set()
        for index, value in enumerate(message):
            if index == 0 and self.kind(value, "At"):
                continue
            if self.kind(value, "At") and str(getattr(value, "qq", "")) not in ("", "all"):
                data = await self.download(
                    f"https://q4.qlogo.cn/headimg_dl?dst_uin={value.qq}&spec=640"
                )
                if data:
                    result.append(data)
            else:
                await self.scan(value, event, result, forwards)
            if len(result) >= 8:
                return result[:8]

        for url in re.findall(r"https?://[^\s]+", getattr(event, "message_str", "") or ""):
            data = await self.download(url.rstrip("，。,.）)"))
            if data:
                result.append(data)
            if len(result) >= 8:
                break
        return result[:8]

    async def download(self, source: str | None) -> bytes | None:
        if not source:
            return None
        value = str(source)
        try:
            if value.startswith("base64://"):
                return base64.b64decode(value[9:], validate=True)
            if value.startswith("data:image/") and ";base64," in value:
                return base64.b64decode(value.split(";base64,", 1)[1], validate=True)
        except Exception:
            return None

        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https"):
            return None
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession() as session:
                async with session.get(value, timeout=timeout) as response:
                    raw = await response.read()
                    content_type = (response.headers.get("Content-Type") or "").lower()
                    if response.status != 200 or len(raw) < 64:
                        return None
                    if content_type and not content_type.startswith("image/"):
                        return None
                    if not self.valid(raw):
                        return None
                    return raw
        except Exception:
            return None

    async def scan(
        self,
        value: Any,
        event: Any,
        result: list[bytes],
        forwards: set[str],
    ) -> None:
        if value is None or len(result) >= 8:
            return
        if self.kind(value, "Image"):
            source = getattr(value, "url", None) or getattr(value, "file", None)
            data = await self.download(source)
            if data:
                result.append(data)
            return
        if self.kind(value, "Reply"):
            await self.scan(getattr(value, "chain", None), event, result, forwards)
            return
        if self.kind(value, "Node"):
            await self.scan(getattr(value, "content", None), event, result, forwards)
            return
        if self.kind(value, "Nodes"):
            await self.scan(getattr(value, "nodes", None), event, result, forwards)
            return
        if self.kind(value, "Forward"):
            await self.forward(str(getattr(value, "id", "") or ""), event, result, forwards)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                await self.scan(item, event, result, forwards)
                if len(result) >= 8:
                    return
            return
        if not isinstance(value, dict):
            return

        kind = str(value.get("type") or "").lower()
        data = value.get("data") if isinstance(value.get("data"), dict) else value
        if kind == "image":
            image = await self.download(data.get("url") or data.get("file"))
            if image:
                result.append(image)
            return
        if kind == "forward":
            await self.forward(str(data.get("id") or ""), event, result, forwards)
            return
        for key in ("messages", "message", "nodes", "content", "data"):
            child = value.get(key)
            if child is not None and child is not value:
                await self.scan(child, event, result, forwards)
                if len(result) >= 8:
                    return

    async def forward(self, forward_id: str, event: Any, result: list[bytes], forwards: set[str]) -> None:
        if not forward_id or forward_id in forwards:
            return
        forwards.add(forward_id)
        call = getattr(getattr(event, "bot", None), "call_action", None)
        if not callable(call):
            return
        try:
            payload = await call("get_forward_msg", message_id=forward_id)
        except Exception:
            return
        nodes = payload.get("messages", []) if isinstance(payload, dict) else []
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            nodes = payload["data"].get("messages", nodes)
        for node in nodes:
            content = node.get("content") or node.get("message") or [] if isinstance(node, dict) else node
            await self.scan(content, event, result, forwards)
            if len(result) >= 8:
                return

    @staticmethod
    def kind(value: Any, name: str) -> bool:
        return Comp is not None and isinstance(value, getattr(Comp, name, ()))

    @staticmethod
    def valid(raw: bytes) -> bool:
        return (
            raw.startswith(b"\x89PNG\r\n\x1a\n")
            or raw.startswith(b"\xff\xd8\xff")
            or raw[:6] in (b"GIF87a", b"GIF89a")
            or raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
        )


images = Images()

__all__ = ["images"]
