"""
超级生图插件的生图指令。

这个文件只负责"把提示词和参考图交给模型，然后拿回图片 bytes"。
它不认识 AstrBot，不知道用户是谁，也不负责保存图片；这样写的好处是：以后想在测试脚本、网页后台、命令行里复用生图能力，只要调用 makeImages() 就行。

数据从 data.py 来：main.py 会把 providers、当前模型下标、提示词、参考图、比例、质量、数量传进来。
结果回到 main.py：这里返回 list[bytes]，main.py 再保存图片并发回聊天。

调用示例：
    images = await makeImages(providers, 0, "一只猫", [], "1:1", "medium", 1)
    images = await makeImages(providers, 1, "把头像改成像素风", [avatarBytes], "auto", "high", 2)
    images = await makeImages(providers, 0, "画四格漫画", [], "16:9", "low", 4, keyGetter=myKeyGetter)
    await closeClients()
"""

from __future__ import annotations

import asyncio  # 重试失败后短暂等待，避免立刻再次撞限流
import base64  # OpenAI 和部分 Gemini 响应会把图片放在 base64 字符串里
import re
from typing import Any, Callable  # keyGetter 是外部传入的取 key 函数

import httpx
from openai import AsyncOpenAI  # OpenAI 官方异步客户端，兼容大多数 OpenAI-like 图像接口

try:
    from .tool.picture import detectMimeType, normalize_to_supported_image
except ImportError:
    from tool.picture import detectMimeType, normalize_to_supported_image

try:
    from google import genai  # Gemini 官方 SDK，可选依赖
    from google.genai import types as genaiTypes  # Gemini 请求体和响应体类型
except ImportError:
    genai = None
    genaiTypes = None


_clients: dict[tuple[Any, ...], Any] = {}  # HTTP 客户端缓存，key 里包含接口类型、地址、API Key


async def makeImages(
    providers: list[dict[str, Any]],
    currentIndex: int,
    prompt: str,
    images: list[bytes],
    size: str = "auto",
    quality: str = "auto",
    n: int = 1,
    keyGetter: Callable[[dict[str, Any]], str] | None = None,
) -> list[bytes]:
    """
    统一生图入口。

    只调用 currentIndex 指定的模型，不偷偷切到别的模型；但会按 provider.maxRetry 重试同一个模型，
    并且每次重试都通过 keyGetter 取一个 key，这样 data.py 可以控制多 key 轮换。
    """

    if not providers:
        raise ValueError("没有配置任何生图供应商，请先在插件配置里添加 api_providers。")

    if currentIndex < 0 or currentIndex >= len(providers):
        raise ValueError(f"当前模型下标 {currentIndex} 不存在，请重新发送 /生图模型 选择模型。")

    provider = providers[currentIndex]
    retryCount = max(1, int(provider.get("maxRetry", 3)))
    lastError: Exception | None = None

    for attempt in range(1, retryCount + 1):
        try:
            apiKey = keyGetter(provider) if keyGetter else _firstKey(provider)
            if provider.get("apiType") == "gemini":
                return await _callGemini(provider, apiKey, prompt, images, size, quality, n)
            if provider.get("apiType") == "openai_chat":
                return await _callOpenAiChat(
                    provider, apiKey, prompt, images, size, quality, n
                )
            return await _callOpenAi(provider, apiKey, prompt, images, size, quality, n)
        except Exception as error:
            lastError = error
            if attempt >= retryCount:
                break
            await asyncio.sleep(min(2 * attempt, 8))  # 轻微退避能缓解临时限流，也不会让用户等太久

    raise RuntimeError(f"生图接口连续失败 {retryCount} 次：{lastError}")


async def closeClients() -> None:
    """关闭所有缓存的 HTTP 客户端，插件卸载时调用，避免连接泄漏。"""

    for client in list(_clients.values()):
        try:
            if hasattr(client, "close"):
                await client.close()
            elif hasattr(client, "aio") and hasattr(client.aio, "aclose"):
                await client.aio.aclose()
        except Exception:
            pass
    _clients.clear()


def _firstKey(provider: dict[str, Any]) -> str:
    """没有传 keyGetter 时使用第一个 key；主要给测试脚本和旧代码保持兼容。"""

    keys = provider.get("apiKeys") or []
    if not keys:
        raise RuntimeError(f"{provider.get('name', 'provider')} 没有配置 apiKeys。")
    return str(keys[0])


_OA_QUALITIES = {"low", "medium", "high"}  # OpenAI 的 auto 不传参数，让服务端自己决定


async def _callOpenAi(provider: dict[str, Any], apiKey: str, prompt: str, images: list[bytes], size: str, quality: str, n: int) -> list[bytes]:
    """调用 OpenAI 兼容接口；不传 size，由模型根据提示词自动决定比例。"""

    count = max(1, min(8, int(n)))

    request: dict[str, Any] = {
        "model": provider["model"],
        "prompt": prompt,
        "n": count,
        "response_format": "url",  # 不强制 b64_json；url 格式更通用，兼容所有实现
    }
    if size and size != "auto":
        request["size"] = size
    elif size == "auto":
        request["size"] = "auto"
    if quality in _OA_QUALITIES:
        request["quality"] = quality

    if images:
        request.pop("response_format", None)
        request.pop("size", None)
        return await _callOpenAiJsonEdit(provider, apiKey, request, images)
    else:
        client = _openAiClient(provider.get("baseUrl") or "https://api.openai.com", apiKey, int(provider.get("timeout", 180)))
        response = await client.images.generate(**request)

    result: list[bytes] = []
    for item in response.data or []:
        if b64_data := getattr(item, "b64_json", None):
            # 部分接口返回 b64_json，直接解码即可
            result.append(base64.b64decode(b64_data))
        elif img_url := getattr(item, "url", None):
            cleanUrl = str(img_url).strip()
            # data URI（data:image/png;base64,xxx）：直接解码，不需要下载
            if cleanUrl.startswith("data:image") and "," in cleanUrl:
                result.append(base64.b64decode(cleanUrl.split(",", 1)[1]))
            else:
                # 真正的 http/https URL，用 SDK 内置 httpx 异步下载
                try:
                    resp = await client._client.get(cleanUrl, timeout=int(provider.get("timeout", 180)))
                    resp.raise_for_status()
                    result.append(resp.content)
                except Exception as download_error:
                    raise ValueError(f"下载图片 URL 失败：{cleanUrl[:80]} — {download_error}")

    if not result:
        raise ValueError("OpenAI 响应里没有图片数据（既无 b64_json 也无 url）。")
    return result


async def _callOpenAiJsonEdit(
    provider: dict[str, Any], apiKey: str, request: dict[str, Any], images: list[bytes]
) -> list[bytes]:
    """调用要求 application/json 的 OpenAI 兼容改图接口。"""

    imageDataUris = _prepareOpenAiImageDataUris(images)
    if not imageDataUris:
        raise ValueError("OpenAI 改图至少需要一张参考图。")
    # JSON 改图服务通常直接 base64 解码 image，不接受 data URI 前缀。
    request["image"] = imageDataUris[0] if len(imageDataUris) == 1 else imageDataUris
    baseUrl = (provider.get("baseUrl") or "https://api.openai.com").rstrip("/")
    apiUrl = baseUrl if baseUrl.endswith("/v1") else f"{baseUrl}/v1"
    timeoutSec = int(provider.get("timeout", 180))
    try:
        async with httpx.AsyncClient(timeout=timeoutSec) as http:
            response = await http.post(
                f"{apiUrl}/images/edits",
                headers={"Authorization": f"Bearer {apiKey}"},
                json=request,
            )
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as error:
        detail = error.response.text[:300].strip()
        raise ValueError(f"OpenAI 改图请求失败：HTTP {error.response.status_code} {detail}") from error
    except Exception as error:
        raise ValueError(f"OpenAI 改图请求失败：{error}") from error

    result: list[bytes] = []
    for item in payload.get("data", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        if b64Data := item.get("b64_json") or item.get("base64"):
            result.append(base64.b64decode(b64Data))
        elif imageUrl := item.get("url") or item.get("image_url"):
            cleanUrl = str(imageUrl).strip()
            if cleanUrl.startswith("data:image") and "," in cleanUrl:
                result.append(base64.b64decode(cleanUrl.split(",", 1)[1]))
            else:
                result.append(await _downloadUrl(cleanUrl, timeoutSec))

    if not result:
        raise ValueError("OpenAI 改图响应里没有图片数据（既无 b64_json 也无 url）。")
    return result


async def _callOpenAiChat(
    provider: dict[str, Any],
    apiKey: str,
    prompt: str,
    images: list[bytes],
    size: str,
    quality: str,
    n: int,
) -> list[bytes]:
    """通过 OpenAI Chat Completions 调用支持图片输出的模型。"""
    client = _openAiClient(
        provider.get("baseUrl") or "https://api.openai.com",
        apiKey,
        int(provider.get("timeout", 180)),
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for imageBytes in images[:16]:
        cleanBytes, mime = normalize_to_supported_image(imageBytes, target_fmt="png")
        encoded = base64.b64encode(cleanBytes).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            }
        )

    request: dict[str, Any] = {
        "model": provider["model"],
        "messages": [{"role": "user", "content": content}],
    }
    if n > 1:
        request["n"] = max(1, min(8, int(n)))
    if size and size != "auto":
        request["size"] = size
    if quality in _OA_QUALITIES:
        request["quality"] = quality

    response = await client.chat.completions.create(**request)
    result: list[bytes] = []
    urls: list[str] = []
    for choice in getattr(response, "choices", None) or []:
        values = _chatImageValues(getattr(choice, "message", None))
        for value in values:
            if isinstance(value, bytes):
                result.append(value)
            else:
                urls.append(value)
    for url in urls:
        result.append(await _downloadUrl(url, int(provider.get("timeout", 180))))
    if not result:
        raise ValueError("OpenAI Chat 响应里没有图片数据。")
    return result


def _chatImageValues(value: Any) -> list[bytes | str]:
    """从 Chat 响应的字符串、列表或字典中提取图片 bytes/URL。"""
    if value is None:
        return []
    if isinstance(value, bytes):
        return [value]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("data:image") and "," in text:
            try:
                return [base64.b64decode(text.split(",", 1)[1])]
            except Exception:
                return []
        if text.startswith(("http://", "https://")):
            return [text]
        urls = re.findall(r"https?://[^\s)\]]+", text)
        return urls
    if isinstance(value, list):
        result: list[bytes | str] = []
        for item in value:
            result.extend(_chatImageValues(item))
        return result
    if isinstance(value, dict):
        result: list[bytes | str] = []
        b64 = value.get("b64_json") or value.get("base64")
        if isinstance(b64, str):
            try:
                result.append(base64.b64decode(b64))
            except Exception:
                pass
        for key in ("url", "image_url", "image", "data", "content"):
            if key in value:
                result.extend(_chatImageValues(value[key]))
        return result
    return _chatImageValues(vars(value)) if hasattr(value, "__dict__") else []


def _prepareOpenAiImageDataUris(images: list[bytes]) -> list[str]:
    """把参考图转为 JSON 改图接口使用的纯 base64 字符串。"""

    result: list[str] = []
    for imageBytes in images[:16]:
        cleanBytes, mime = normalize_to_supported_image(imageBytes, target_fmt="png")
        encoded = base64.b64encode(cleanBytes).decode("ascii")
        result.append(encoded)
    return result


def _openAiClient(baseUrl: str, apiKey: str, timeout: int) -> AsyncOpenAI:
    """按接口地址和 key 复用 OpenAI 客户端，避免每次生图都重新建连接。"""

    cleanUrl = baseUrl.rstrip("/")
    apiUrl = cleanUrl if cleanUrl.endswith("/v1") else f"{cleanUrl}/v1"
    cacheKey = ("openai", apiUrl, apiKey)
    if cacheKey not in _clients:
        _clients[cacheKey] = AsyncOpenAI(api_key=apiKey, base_url=apiUrl, timeout=timeout, max_retries=0)
    return _clients[cacheKey]


_GM_RATIOS = {
    "auto": None,
    "1024x1024": "1:1",
    "1536x1024": "16:9",
    "1024x1536": "9:16",
    "1:1": "1:1",
    "16:9": "16:9",
    "9:16": "9:16",
    "3:2": "3:2",
    "2:3": "2:3",
}


async def _callGemini(provider: dict[str, Any], apiKey: str, prompt: str, images: list[bytes], size: str, quality: str, n: int) -> list[bytes]:
    """调用 Gemini 官方接口；Gemini 一次通常返回一张图，所以需要几张就循环几次。"""

    if genai is None or genaiTypes is None:
        raise RuntimeError("缺少 google-genai 依赖，请安装 requirements.txt 后重启 AstrBot。")

    client = _geminiClient(apiKey, provider.get("baseUrl") or "")
    parts: list[Any] = [genaiTypes.Part.from_text(text=prompt)]

    for imageBytes in images[:16]:
        cleanBytes, mime = normalize_to_supported_image(imageBytes, target_fmt="png")
        if detectMimeType(cleanBytes).startswith("image/"):
            parts.append(genaiTypes.Part.from_bytes(data=cleanBytes, mime_type=mime))

    config = genaiTypes.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"])
    ratio = _GM_RATIOS.get(size)
    if ratio:
        config.image_config = genaiTypes.ImageConfig(aspect_ratio=ratio)

    result: list[bytes] = []
    timeoutSec = int(provider.get("timeout", 180))
    for _ in range(max(1, min(8, int(n)))):
        response = await client.aio.models.generate_content(model=provider["model"], contents=parts, config=config)
        imageBytes, imageUrls = _readGeminiImages(response)
        result.extend(imageBytes)
        for url in imageUrls:  # 某些代理把图片以 URL 形式返回，需要下载
            result.append(await _downloadUrl(url, timeoutSec))

    if not result:
        raise ValueError("Gemini 响应里没有图片数据。")
    return result


async def _downloadUrl(url: str, timeoutSec: int) -> bytes:
    """下载图片 URL；httpx 是 openai SDK 的依赖，环境里必定存在。"""

    try:
        async with httpx.AsyncClient(timeout=timeoutSec) as http:
            resp = await http.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception as error:
        raise ValueError(f"下载图片 URL 失败：{url} — {error}")


def _readGeminiImages(response: Any) -> tuple[list[bytes], list[str]]:
    """从 Gemini 响应中抽取图片；返回 (bytes 列表, 待下载 URL 列表)。"""

    imageBytes: list[bytes] = []
    imageUrls: list[str] = []
    candidates = getattr(response, "candidates", None) or []

    # response.parts 只是 candidates[0].content.parts 的快捷方式，两边都遍历会重复取图；
    # 所以优先遍历 candidates，只有拿不到 candidates 时才退回 response.parts。
    allParts: list[Any] = []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        allParts.extend(getattr(content, "parts", []) or [])
    if not allParts:
        allParts = list(getattr(response, "parts", None) or [])

    for part in allParts:
        data, url = _readGeminiPart(part)
        if data:
            imageBytes.append(data)
        if url:
            imageUrls.append(url)

    return imageBytes, imageUrls


def _readGeminiPart(part: Any) -> tuple[bytes | None, str | None]:
    """
    读取 Gemini 单个 part 里的图片，返回 (图片bytes, 图片URL)，两者最多一个非空。

    标准 Gemini API 把图片放在 inline_data.data（bytes 或 base64 字符串）。
    某些代理（如 new-api 类中转）会把图片 URL 放在 text 字段里，需要单独识别再下载。
    """

    inline = getattr(part, "inline_data", None)
    data = getattr(inline, "data", None) if inline else None
    if isinstance(data, bytes) and data:
        return data, None
    if isinstance(data, str) and data:
        return base64.b64decode(data), None

    text = getattr(part, "text", None)
    if isinstance(text, str):
        cleanText = text.strip()
        # data URI 格式（data:image/png;base64,xxx）：直接解出 base64 部分，不需要下载
        if cleanText.startswith("data:image"):
            if "," in cleanText:
                b64Part = cleanText.split(",", 1)[1]
                try:
                    return base64.b64decode(b64Part), None
                except Exception:
                    return None, None
            return None, None
        # http/https URL：交给外层下载
        if cleanText.startswith(("http://", "https://")):
            return None, cleanText

    return None, None


def _geminiClient(apiKey: str, baseUrl: str = "") -> Any:
    """按 key 和 baseUrl 复用 Gemini 客户端。"""

    cacheKey = ("gemini", apiKey, baseUrl)
    if cacheKey in _clients:
        return _clients[cacheKey]

    options: dict[str, Any] = {}
    if baseUrl:
        options["http_options"] = genaiTypes.HttpOptions(base_url=baseUrl)
    _clients[cacheKey] = genai.Client(api_key=apiKey, **options)
    return _clients[cacheKey]
