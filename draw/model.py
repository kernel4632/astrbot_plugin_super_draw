"""OpenAI Images、OpenAI Chat 和 Gemini 协议调用。"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import re
from typing import Any, Callable

import httpx
from openai import AsyncOpenAI

try:
    from .picture import normalize_to_supported_image
except ImportError:
    from picture import normalize_to_supported_image

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:  # pragma: no cover - optional dependency in small test envs
    genai = None
    genai_types = None


class ModelFailure(Exception):
    """A provider error with a stable kind for callers and retry logic."""

    def __init__(self, kind: str, message: str, status: int | None = None, code: str | None = None):
        if kind not in {"policy", "request", "network", "timeout", "unavailable"}:
            raise ValueError(f"invalid provider failure kind: {kind}")
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.code = code
        self.message = message


def get(config: Any, name: str, default: Any = None) -> Any:
    if isinstance(config, dict):
        return config.get(name, default)
    return getattr(config, name, default)


def failure(error: Exception, fallback: str = "request") -> ModelFailure:
    status = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    status = status or getattr(response, "status_code", None)
    body = getattr(error, "body", None)
    code = None
    if isinstance(body, dict):
        detail = body.get("error", body)
        if isinstance(detail, dict):
            code = detail.get("code") or detail.get("type")
    message = extract(error)
    lower = f"{code or ''} {message}".lower()
    if "policy" in lower or "content_filter" in lower or "safety" in lower:
        return ModelFailure("policy", message, status, str(code) if code else None)
    if isinstance(error, (asyncio.TimeoutError, httpx.TimeoutException)) or "timeout" in lower:
        return ModelFailure("timeout", message, status, str(code) if code else None)
    if isinstance(error, (httpx.NetworkError,)) or "connection" in lower:
        return ModelFailure("network", message, status, str(code) if code else None)
    return ModelFailure(fallback, message, status, str(code) if code else None)


def extract(error: Exception) -> str:
    """只取服务商错误正文里的说明，避免把请求地址带给用户。"""
    response = getattr(error, "response", None)
    body = getattr(error, "body", None)
    text = getattr(response, "text", "") if response is not None else ""
    value = body or text
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    message = find(value)
    return sanitize(message or str(error).split(" for url ", 1)[0])


def find(value: Any) -> str:
    """从常见 JSON 错误结构中取出 message，不关心 HTTP 状态码。"""
    if isinstance(value, dict):
        message = value.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
        return find(value.get("error"))
    if isinstance(value, list):
        return next((found for item in value if (found := find(item))), "")
    return ""


def sanitize(message: str) -> str:
    """删除错误文本中的地址、邮箱和密钥，保留剩余原文。"""
    patterns = (
        (r"https?://[^\s\"']+", "[地址]"),
        (r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b", "[地址]"),
        (r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱]"),
        (r"\b(?:sk|key|AIza)[-_][A-Za-z0-9_-]{8,}\b", "[密钥]"),
    )
    for pattern, replacement in patterns:
        message = re.sub(pattern, replacement, message, flags=re.IGNORECASE)
    return message.strip()


async def decode(payload: Any, timeout: int) -> list[bytes]:
    """Decode only the documented OpenAI image fields."""
    values = payload.get("data", []) if isinstance(payload, dict) else getattr(payload, "data", [])
    result: list[bytes] = []
    for item in values or []:
        item = item if isinstance(item, dict) else vars(item)
        encoded = item.get("b64_json")
        url = item.get("url")
        if not isinstance(url, str) and isinstance(item.get("image_url"), dict):
            url = item["image_url"].get("url")
        if isinstance(encoded, str):
            result.append(base64.b64decode(encoded))
        elif isinstance(url, str) and url.startswith("data:image") and "," in url:
            result.append(base64.b64decode(url.split(",", 1)[1]))
        elif isinstance(url, str) and url.startswith(("http://", "https://")):
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url)
                response.raise_for_status()
                result.append(response.content)
    return result


class Model:
    async def draw(
        self,
        provider_config: Any,
        prompt: str,
        images: list[bytes],
        size: str = "auto",
        quality: str = "auto",
        count: int = 1,
        key_getter: Callable[[Any], Any] | None = None,
    ) -> list[bytes]:
        retries = max(1, int(get(provider_config, "maxRetry", 3)))
        last: ModelFailure | None = None
        for attempt in range(retries):
            try:
                key = key_getter(provider_config) if key_getter else get(provider_config, "apiKey", "")
                if inspect.isawaitable(key):
                    key = await key
                if not key:
                    keys = get(provider_config, "apiKeys", []) or []
                    key = keys[0] if keys else ""
                kind = str(get(provider_config, "apiType", "openai")).lower()
                if kind in {"openai_chat", "chat"}:
                    return await self.chat(provider_config, str(key), prompt, images, size, quality, count)
                if kind == "gemini":
                    return await self.gemini(provider_config, str(key), prompt, images, size, count)
                return await self.openai(provider_config, str(key), prompt, images, size, quality, count)
            except ModelFailure as error:
                last = error
                retryable = error.kind in {"timeout", "network"} or error.status == 429 or (error.status is not None and error.status >= 500)
                if not retryable or attempt + 1 >= retries:
                    raise
                await asyncio.sleep(min(attempt + 1, 3))
        raise last or ModelFailure("unavailable", "provider did not return an image")

    async def openai(self, config: Any, key: str, prompt: str, images: list[bytes], size: str, quality: str, count: int) -> list[bytes]:
        base = (get(config, "baseUrl", "https://api.openai.com") or "https://api.openai.com").rstrip("/")
        timeout = int(get(config, "timeout", 180))
        model = get(config, "model")
        request = {"model": model, "prompt": prompt, "n": max(1, min(8, int(count)))}
        if size:
            request["size"] = size
        if quality in {"low", "medium", "high"}:
            request["quality"] = quality
        try:
            if images:
                url = base if base.endswith("/v1") else f"{base}/v1"
                prepared = []
                for index, image in enumerate(images[:16]):
                    clean, mime = normalize_to_supported_image(image, target_fmt="png")
                    prepared.append(("image", (f"ref_{index}.png", clean, mime)))
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(f"{url}/images/edits", headers={"Authorization": f"Bearer {key}"}, data={k: str(v) for k, v in request.items()}, files=prepared)
                    response.raise_for_status()
                    result = await decode(response.json(), timeout)
            else:
                client = AsyncOpenAI(api_key=key, base_url=base if base.endswith("/v1") else f"{base}/v1", timeout=timeout, max_retries=0)
                result = await decode(await client.images.generate(**request), timeout)
        except ModelFailure:
            raise
        except Exception as error:
            raise failure(error) from error
        if not result:
            raise ModelFailure("unavailable", "OpenAI response has no explicit b64_json or data URI image")
        return result

    async def chat(self, config: Any, key: str, prompt: str, images: list[bytes], size: str, quality: str, count: int) -> list[bytes]:
        base = (get(config, "baseUrl", "https://api.openai.com") or "https://api.openai.com").rstrip("/")
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in images[:16]:
            clean, mime = normalize_to_supported_image(image, target_fmt="png")
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(clean).decode('ascii')}"}})
        request: dict[str, Any] = {"model": get(config, "model"), "messages": [{"role": "user", "content": content}]}
        if count > 1:
            request["n"] = max(1, min(8, int(count)))
        try:
            client = AsyncOpenAI(api_key=key, base_url=base if base.endswith("/v1") else f"{base}/v1", timeout=int(get(config, "timeout", 180)), max_retries=0)
            response = await client.chat.completions.create(**request)
            result: list[bytes] = []
            for choice in getattr(response, "choices", []) or []:
                message = getattr(choice, "message", None)
                value = getattr(message, "content", None)
                if isinstance(value, str) and value.startswith("data:image") and "," in value:
                    result.append(base64.b64decode(value.split(",", 1)[1]))
                elif isinstance(value, str) and value.startswith(("http://", "https://")):
                    result.extend(await decode({"data": [{"url": value}]}, int(get(config, "timeout", 180))))
                elif isinstance(value, dict):
                    result.extend(await decode({"data": [value]}, int(get(config, "timeout", 180))))
        except Exception as error:
            raise failure(error) from error
        if not result:
            raise ModelFailure("unavailable", "OpenAI Chat response has no explicit image data")
        return result

    async def gemini(self, config: Any, key: str, prompt: str, images: list[bytes], size: str, count: int) -> list[bytes]:
        if genai is None or genai_types is None:
            raise ModelFailure("unavailable", "google-genai dependency is unavailable")
        try:
            client = genai.Client(api_key=key)
            parts = [genai_types.Part.from_text(text=prompt)]
            for image in images[:16]:
                clean, mime = normalize_to_supported_image(image, target_fmt="png")
                parts.append(genai_types.Part.from_bytes(data=clean, mime_type=mime))
            config_arg = genai_types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"])
            result: list[bytes] = []
            for _ in range(max(1, min(8, int(count)))):
                response = await client.aio.models.generate_content(model=get(config, "model"), contents=parts, config=config_arg)
                for candidate in getattr(response, "candidates", []) or []:
                    for part in getattr(getattr(candidate, "content", None), "parts", []) or []:
                        data = getattr(getattr(part, "inline_data", None), "data", None)
                        if isinstance(data, bytes):
                            result.append(data)
                        elif isinstance(data, str):
                            result.append(base64.b64decode(data))
        except Exception as error:
            raise failure(error) from error
        if not result:
            raise ModelFailure("unavailable", "Gemini response has no inline_data image")
        return result


model = Model()
