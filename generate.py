"""
超级生图插件的生图指令。

这个文件只负责“把提示词和参考图交给模型，然后拿回图片 bytes”。
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
import aiohttp  # 用来下载 URL 格式的图片，兼容不返回 b64_json 的 API
from typing import Any, Callable  # keyGetter 是外部传入的取 key 函数

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


_OA_SIZES = {
    "auto": "1024x1024",
    "1:1": "1024x1024",
    "16:9": "1536x1024",
    "9:16": "1024x1536",
    "3:2": "1536x1024",
    "2:3": "1024x1536",
    "1024x1024": "1024x1024",
    "1536x1024": "1536x1024",
    "1024x1536": "1024x1536",
}

_OA_QUALITIES = {"low", "medium", "high"}  # OpenAI 的 auto 不传参数，让服务端自己决定


async def _callOpenAi(provider: dict[str, Any], apiKey: str, prompt: str, images: list[bytes], size: str, quality: str, n: int) -> list[bytes]:
    """调用 OpenAI 兼容接口；有参考图走 edit，没有参考图走 generate。"""

    client = _openAiClient(provider.get("baseUrl") or "https://api.openai.com", apiKey, int(provider.get("timeout", 180)))
    count = max(1, min(8, int(n)))

    request: dict[str, Any] = {
        "model": provider["model"],
        "prompt": prompt,
        "n": count,
        "size": _OA_SIZES.get(size, "1024x1024"),
    }
    if quality in _OA_QUALITIES:
        request["quality"] = quality

    if images:
        request["image"] = _prepareOpenAiImages(images)
        response = await client.images.edit(**request)
    else:
        response = await client.images.generate(**request)

    result: list[bytes] = []
    timeout_sec = int(provider.get("timeout", 180))
    for item in response.data or []:
        # 优先使用 b64_json；部分 OpenAI 兼容 API 不返回 b64_json 而是 url，需要下载
        if b64_data := getattr(item, "b64_json", None):
            result.append(base64.b64decode(b64_data))
        elif img_url := getattr(item, "url", None):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(img_url, timeout=aiohttp.ClientTimeout(total=timeout_sec)) as resp:
                        if resp.status == 200:
                            result.append(await resp.read())
            except Exception as download_error:
                raise ValueError(f"下载图片 URL 失败：{img_url} — {download_error}")

    if not result:
        raise ValueError("OpenAI 响应里没有图片数据（既无 b64_json 也无 url）。")
    return result


def _prepareOpenAiImages(images: list[bytes]) -> list[tuple[str, bytes, str]]:
    """把参考图统一整理成 OpenAI edit 接口可接收的 (文件名, bytes, mime) 列表。"""

    result: list[tuple[str, bytes, str]] = []
    for index, imageBytes in enumerate(images[:16]):
        cleanBytes, mime = normalize_to_supported_image(imageBytes, target_fmt="png")
        result.append((f"ref_{index}.png", cleanBytes, mime))
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
    for _ in range(max(1, min(8, int(n)))):
        response = await client.aio.models.generate_content(model=provider["model"], contents=parts, config=config)
        result.extend(_readGeminiImages(response))

    if not result:
        raise ValueError("Gemini 响应里没有图片数据。")
    return result


def _readGeminiImages(response: Any) -> list[bytes]:
    """从 Gemini 响应中抽取图片 bytes；不同 SDK 版本字段略有差异，所以这里集中兼容。"""

    result: list[bytes] = []
    candidates = getattr(response, "candidates", None) or []
    directParts = getattr(response, "parts", None) or []

    for part in directParts:
        result.extend(_readGeminiPart(part))

    for candidate in candidates:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            result.extend(_readGeminiPart(part))

    return result


def _readGeminiPart(part: Any) -> list[bytes]:
    """读取 Gemini 单个 part 里的 inline_data，bytes 直接用，字符串就按 base64 解码。"""

    inline = getattr(part, "inline_data", None)
    data = getattr(inline, "data", None) if inline else None

    if isinstance(data, bytes):
        return [data]
    if isinstance(data, str):
        return [base64.b64decode(data)]
    return []


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
