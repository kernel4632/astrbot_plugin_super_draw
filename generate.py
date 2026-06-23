"""
生图接口调用。

拿到提示词和参考图，调用 OpenAI 或 Gemini 接口生图，返回图片字节列表。
不故障转移：只使用指定的单个 provider，失败了直接抛异常。
每个 provider 内部也只使用第一个 apiKey，不轮换不兜底。

这个文件和 AstrBot 完全无关，只要传入 provider 配置就能用。

provider 是一个普通 dict，格式如下：
    {"name": "OpenAI", "apiType": "openai", "baseUrl": "https://api.openai.com",
     "apiKeys": ["sk-xxx"], "model": "gpt-image-2", "timeout": 180, "maxRetry": 3}

调用示例：
    result = await makeImages(providers, 0, "一只猫", [], "auto", "medium", 1)
    result = await makeImages(providers, 0, "变成水彩风", [refImg], "16:9", "high", 2)
    await closeClients()  # 插件关闭时调用，释放 HTTP 连接
"""

from __future__ import annotations

import base64  # 解码 OpenAI 返回的 base64 图片数据
from typing import Any  # 类型标注

from openai import AsyncOpenAI  # OpenAI 官方异步客户端

# 图片格式识别，给参考图标注正确的 MIME 类型
try:
    from .tool.picture import detectMimeType, normalize_to_supported_image
except ImportError:
    from tool.picture import detectMimeType, normalize_to_supported_image

# Gemini SDK 是可选依赖，没装就只能用 OpenAI 接口
try:
    from google import genai  # Gemini 官方 SDK
    from google.genai import types as genaiTypes  # Gemini 的请求/响应类型
except ImportError:
    genai = None
    genaiTypes = None


# ========== 客户端缓存 ==========
# 按 (接口类型, 地址, key) 缓存，避免每次生图都新建 HTTP 客户端
_clients: dict[tuple, Any] = {}


# ========== 统一入口 ==========


async def makeImages(
    providers: list[dict],
    currentIndex: int,
    prompt: str,
    images: list[bytes],
    size: str = "auto",
    quality: str = "auto",
    n: int = 1,
) -> list[bytes]:
    """
    统一生图入口。只使用 currentIndex 指定的单个 provider，
    失败了直接抛出异常，不轮询、不故障转移。
    """

    if not providers:
        raise ValueError("没有配置生图 provider")
    if not 0 <= currentIndex < len(providers):
        raise ValueError(f"currentIndex {currentIndex} 越界")

    p = providers[currentIndex]
    if p["apiType"] == "gemini":
        return await _callGemini(p, prompt, images, size, quality, n)
    return await _callOpenAi(p, prompt, images, size, quality, n)


async def closeClients():
    """关闭所有缓存的 HTTP 客户端。插件关闭时调用。"""

    for client in _clients.values():
        try:
            if hasattr(client, "close"):  # OpenAI 客户端用 close()
                await client.close()
            elif hasattr(client, "aio") and hasattr(client.aio, "aclose"):  # Gemini 客户端用 aio.aclose()
                await client.aio.aclose()
        except Exception:
            pass
    _clients.clear()


# ========== OpenAI 兼容接口 ==========

# 用户友好的比例名 -> OpenAI 接受的像素尺寸
_OA_SIZES = {
    "1:1": "1024x1024",
    "16:9": "1536x1024",
    "9:16": "1024x1536",  # 常用比例
    "3:2": "1536x1024",
    "2:3": "1024x1536",  # 近似映射
    "1024x1024": "1024x1024",
    "1536x1024": "1536x1024",
    "1024x1536": "1024x1536",  # 直接传像素也行
}

# OpenAI 接受的质量值
_OA_QUALITIES = {"low", "medium", "high"}


async def _callOpenAi(p: dict, prompt: str, images: list[bytes], size: str, quality: str, n: int) -> list[bytes]:
    """
    调用 OpenAI 兼容接口生图。
    有参考图走 images.edit（图生图），没有走 images.generate（文生图）。
    只使用第一个 apiKey，失败了直接抛异常，不重试不轮换。
    """

    if not p.get("apiKeys"):
        raise RuntimeError("OpenAI provider 没有配置 apiKeys")

    key = p["apiKeys"][0]
    client = _openAiClient(p["baseUrl"], key, p.get("timeout", 180))

    # 构建请求参数
    kwargs: dict[str, Any] = {
        "model": p["model"],
        "prompt": prompt,
        "n": min(max(1, n), 4),  # 限制 1-4 张
        "size": _OA_SIZES.get(size, "1024x1024"),  # 转成像素尺寸
    }
    if quality in _OA_QUALITIES:  # "auto" 时不传，让接口自己决定
        kwargs["quality"] = quality

    # 有参考图用 edit 接口，没有用 generate 接口
    if images:
        processed_images = []
        for i, img in enumerate(images[:16]):
            # OpenAI 不支持 GIF/WEBP 动态图，这里强制规范化成 PNG
            norm_img, mime = normalize_to_supported_image(img, target_fmt="png")
            processed_images.append((f"ref_{i}.png", norm_img, mime))

        kwargs["image"] = processed_images
        resp = await client.images.edit(**kwargs)
    else:
        resp = await client.images.generate(**kwargs)

    # 从响应里取出 base64 编码的图片，解码成 bytes
    result = [base64.b64decode(d.b64_json) for d in (resp.data or []) if getattr(d, "b64_json", None)]
    if not result:
        raise ValueError("OpenAI 响应中没有图片数据")
    return result


def _openAiClient(baseUrl: str, apiKey: str, timeout: int) -> AsyncOpenAI:
    """获取或创建 OpenAI 客户端（按地址 + key 缓存）。"""

    k = ("openai", baseUrl, apiKey)
    if k not in _clients:
        _clients[k] = AsyncOpenAI(api_key=apiKey, base_url=f"{baseUrl}/v1", timeout=timeout, max_retries=0)
    return _clients[k]


# ========== Gemini 官方接口 ==========

# Gemini 接口直接用比例名，像素尺寸也帮你转成比例
_GM_RATIOS = {
    "1024x1024": "1:1",
    "1536x1024": "16:9",
    "1024x1536": "9:16",  # 像素 -> 比例
    "1:1": "1:1",
    "16:9": "16:9",
    "9:16": "9:16",
    "3:2": "3:2",
    "2:3": "2:3",  # 比例直接用
}


async def _callGemini(p: dict, prompt: str, images: list[bytes], size: str, quality: str, n: int) -> list[bytes]:
    """
    调用 Gemini 官方生图接口。
    Gemini 不支持批量生成，所以循环调用 n 次。
    只使用第一个 apiKey，失败了直接抛异常，不重试不轮换。
    """

    if genai is None:
        raise RuntimeError("缺少 google-genai 依赖，请 pip install google-genai")

    if not p.get("apiKeys"):
        raise RuntimeError("Gemini provider 没有配置 apiKeys")

    key = p["apiKeys"][0]
    client = _geminiClient(key, p.get("baseUrl"))

    # 构建请求内容：文字提示词 + 参考图
    parts: list[Any] = [genaiTypes.Part.from_text(text=prompt)]
    for img in images[:16]:
        mime = detectMimeType(img)
        if mime.startswith("image/"):  # 只传真正的图片，跳过无法识别的
            parts.append(genaiTypes.Part.from_bytes(data=img, mime_type=mime))

    # 配置生成参数：要求返回图片
    config = genaiTypes.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"])
    ratio = _GM_RATIOS.get(size)
    if ratio:  # 有对应比例就设置，没有就让模型自己决定
        config.image_config = genaiTypes.ImageConfig(aspect_ratio=ratio)

    # Gemini 每次只生成一张，要 n 张就调 n 次
    result: list[bytes] = []
    for _ in range(min(max(1, n), 4)):
        resp = await client.aio.models.generate_content(
            model=p["model"],
            contents=parts,
            config=config,
        )
        # 从响应里提取图片字节
        for part in getattr(resp, "parts", []) or []:
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None) if inline else None
            if isinstance(data, bytes):  # 直接就是字节
                result.append(data)
            elif isinstance(data, str):  # base64 编码的字符串
                result.append(base64.b64decode(data))

    if not result:
        raise ValueError("Gemini 响应中没有图片数据")
    return result


def _geminiClient(apiKey: str, baseUrl: str | None = None) -> Any:
    """获取或创建 Gemini 客户端（按 key + baseUrl 缓存）。"""

    k = ("gemini", apiKey, baseUrl or "")
    if k not in _clients:
        opts = {}
        if baseUrl:
            opts["http_options"] = genaiTypes.HttpOptions(base_url=baseUrl)
        _clients[k] = genai.Client(api_key=apiKey, **opts)
    return _clients[k]
