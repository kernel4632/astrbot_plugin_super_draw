"""从 AstrBot 消息中收集图片字节。"""

from __future__ import annotations

import base64
import io
import inspect
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from astrbot.api import logger

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import astrbot.api.message_components as Comp
except ImportError:  # 允许在没有 AstrBot 的环境中导入和测试
    Comp = None


class Picture:
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

    async def download(self, source: str | None, local: bool = False) -> bytes | None:
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

        if local:
            try:
                raw = Path(value).read_bytes()
                return raw if self.valid(raw) else None
            except OSError:
                pass

        parsed = urlparse(value)
        if local and parsed.scheme == "file":
            value = unquote(parsed.path)
            if value.startswith("/") and len(value) > 2 and value[2] == ":":
                value = value[1:]
            try:
                raw = Path(value).read_bytes()
                return raw if self.valid(raw) else None
            except OSError:
                return None
        if local and not parsed.scheme:
            try:
                raw = Path(value).read_bytes()
                return raw if self.valid(raw) else None
            except OSError:
                return None
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
        allow_reply: bool = True,
    ) -> None:
        if value is None or len(result) >= 8:
            return
        if self.kind(value, "Image"):
            data = await self.component(value)
            if data:
                result.append(data)
            return
        if self.kind(value, "Reply"):
            if not allow_reply:
                return
            chain = getattr(value, "chain", None)
            before = len(result)
            if chain:
                await self.scan(chain, event, result, forwards, allow_reply)
            if len(result) == before:
                await self.reply(self.id(value), event, result, forwards)
            return
        if self.kind(value, "Node"):
            await self.scan(getattr(value, "content", None), event, result, forwards, allow_reply)
            return
        if self.kind(value, "Nodes"):
            await self.scan(getattr(value, "nodes", None), event, result, forwards, allow_reply)
            return
        if self.kind(value, "Forward"):
            await self.forward(str(getattr(value, "id", "") or ""), event, result, forwards, allow_reply)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                await self.scan(item, event, result, forwards, allow_reply)
                if len(result) >= 8:
                    return
            return
        if not isinstance(value, dict):
            return

        kind = str(value.get("type") or "").lower()
        data = value.get("data") if isinstance(value.get("data"), dict) else value
        if kind == "image":
            image = await self.download(
                data.get("url") or data.get("path") or data.get("file"),
                local=True,
            )
            if image:
                result.append(image)
            return
        if kind == "reply":
            if not allow_reply:
                return
            chain = data.get("chain")
            before = len(result)
            if chain:
                await self.scan(chain, event, result, forwards, allow_reply)
            if len(result) == before:
                await self.reply(self.id(data), event, result, forwards)
            return
        if kind == "forward":
            await self.forward(self.id(data), event, result, forwards, allow_reply)
            return
        for key in ("messages", "message", "nodes", "content", "data"):
            child = value.get(key)
            if child is not None and child is not value:
                await self.scan(child, event, result, forwards, allow_reply)
                if len(result) >= 8:
                    return

    async def forward(
        self,
        forward_id: str,
        event: Any,
        result: list[bytes],
        forwards: set[str],
        allow_reply: bool = True,
    ) -> None:
        marker = f"forward:{forward_id}"
        if not forward_id or marker in forwards:
            return
        forwards.add(marker)
        try:
            payload = await self.action(event, "get_forward_msg", forward_id)
        except Exception as error:
            logger.warning(f"[SuperDraw] 读取合并转发消息失败: {error}")
            return
        await self.scan(payload, event, result, forwards, allow_reply)

    async def reply(self, message_id: str, event: Any, result: list[bytes], forwards: set[str]) -> None:
        marker = f"reply:{message_id}"
        if not message_id or marker in forwards:
            return
        forwards.add(marker)
        try:
            payload = await self.action(event, "get_msg", message_id)
        except Exception as error:
            logger.warning(f"[SuperDraw] 读取引用消息失败: {error}")
            return
        await self.scan(payload, event, result, forwards, False)

    async def action(self, event: Any, name: str, message_id: str) -> Any:
        bot = getattr(event, "bot", None)
        direct = getattr(bot, "call_action", None)
        nested = getattr(getattr(bot, "api", None), "call_action", None)
        call = direct if callable(direct) else nested
        if not callable(call):
            raise RuntimeError("当前消息平台不支持 OneBot 消息查询")

        routing = self.routing(event)
        errors: list[Exception] = []
        keys = ("message_id", "id") if name == "get_forward_msg" else ("message_id",)
        values: list[Any] = [message_id]
        if message_id.isdigit():
            values.insert(0, int(message_id))
        for key in keys:
            for value in values:
                try:
                    response = call(name, **{key: value}, **routing)
                    return await response if inspect.isawaitable(response) else response
                except Exception as error:
                    errors.append(error)
        raise errors[-1] if errors else RuntimeError(f"{name} 调用失败")

    async def component(self, value: Any) -> bytes | None:
        convert = getattr(value, "convert_to_base64", None)
        if callable(convert):
            try:
                encoded = convert()
                encoded = await encoded if inspect.isawaitable(encoded) else encoded
                if encoded:
                    return base64.b64decode(str(encoded), validate=True)
            except Exception:
                pass
        source = (
            getattr(value, "url", None)
            or getattr(value, "path", None)
            or getattr(value, "file", None)
        )
        return await self.download(source, local=True)

    @staticmethod
    def routing(event: Any) -> dict[str, Any]:
        message = getattr(event, "message_obj", None)
        self_id = getattr(message, "self_id", None)
        raw = getattr(message, "raw_message", None)
        if not self_id:
            self_id = raw.get("self_id") if isinstance(raw, dict) else getattr(raw, "self_id", None)
        return {"self_id": self_id} if self_id else {}

    @staticmethod
    def id(value: Any) -> str:
        if isinstance(value, dict):
            values = (value.get("id"), value.get("message_id"), value.get("msg_id"), value.get("resid"), value.get("forward_id"))
        else:
            data = getattr(value, "data", None)
            values = (
                getattr(value, "id", None),
                getattr(value, "message_id", None),
                getattr(value, "msg_id", None),
                getattr(value, "resid", None),
                data.get("id") if isinstance(data, dict) else None,
            )
        return next((str(item).strip() for item in values if item is not None and str(item).strip()), "")

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


SUPPORTED_FORMATS = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
TargetFormat = Literal["png", "jpeg"]


def detect(data: bytes) -> str:
    """根据图片文件头返回 MIME 类型。"""
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and len(data) > 12 and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) > 12 and data[4:8] == b"ftyp":
        if data[8:12] in (b"heic", b"heix", b"heim", b"heis"):
            return "image/heic"
        if data[8:12] in (b"mif1", b"msf1", b"heif"):
            return "image/heif"
    return "application/octet-stream"


def normalize_to_supported_image(data: bytes, target_fmt: TargetFormat = "png") -> tuple[bytes, str]:
    """保留静态图片，把动态图或其他格式转换为静态首帧。"""
    mime = detect(data)
    if mime in SUPPORTED_FORMATS:
        return data, mime
    if Image is None:
        raise RuntimeError(f"图片格式 {mime} 需要 Pillow 转换。")

    image = Image.open(io.BytesIO(data))
    image.seek(0)
    output = io.BytesIO()
    if target_fmt == "png":
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")
        image.save(output, format="PNG")
        return output.getvalue(), "image/png"
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    image.save(output, format="JPEG", quality=90)
    return output.getvalue(), "image/jpeg"


picture = Picture()

__all__ = ["picture", "normalize_to_supported_image"]
