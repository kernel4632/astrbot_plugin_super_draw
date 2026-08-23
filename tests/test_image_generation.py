import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "astrbot_plugin_super_draw"


def _load_generate():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules.setdefault(PACKAGE, package)
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.generate", ROOT / "generate.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


generate = _load_generate()


def test_openai_request_passes_auto_size_and_count(monkeypatch):
    client = SimpleNamespace(
        images=SimpleNamespace(
            generate=AsyncMock(
                return_value=SimpleNamespace(data=[SimpleNamespace(b64_json="aW1hZ2U=")])
            )
        )
    )
    monkeypatch.setattr(generate, "_openAiClient", lambda *args: client)

    result = asyncio.run(
        generate._callOpenAi(
            {"model": "image-model", "baseUrl": "https://example.com"},
            "test-key",
            "一张横向风景图",
            [],
            "auto",  # size
            "auto",  # quality：不在 _OA_QUALITIES 里，不应出现在请求里
            1,  # n
        )
    )

    assert result == [b"image"]
    request = client.images.generate.call_args.kwargs
    assert request["size"] == "auto"  # 新版显式把 auto 传给服务端
    assert request["n"] == 1
    assert "quality" not in request


def test_openai_edit_uses_json_data_uris(monkeypatch):
    request = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"b64_json": "aW1hZ2U="}]}

    class HttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            request["url"] = url
            request.update(kwargs)
            return Response()

    monkeypatch.setattr(generate.httpx, "AsyncClient", lambda **kwargs: HttpClient())
    result = asyncio.run(
        generate._callOpenAi(
            {"model": "image-model", "baseUrl": "https://example.com", "timeout": 30},
            "test-key",
            "把图改成水彩画",
            [b"image"],
            "1024x1024",
            "auto",
            1,
        )
    )

    assert result == [b"image"]
    assert request["url"] == "https://example.com/v1/images/edits"
    assert request["headers"]["Authorization"] == "Bearer test-key"
    assert request["json"]["image"] == "aW1hZ2U="
    assert "response_format" not in request["json"]
    assert "size" not in request["json"]


def test_openai_chat_dispatches_and_decodes_data_uri(monkeypatch):
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="data:image/png;base64,aW1hZ2U="
                )
            )
        ]
    )
    chat = SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response)))
    client = SimpleNamespace(chat=chat)
    monkeypatch.setattr(generate, "_openAiClient", lambda *args: client)

    result = asyncio.run(
        generate.makeImages(
            [
                {
                    "apiType": "openai_chat",
                    "apiKeys": ["test-key"],
                    "model": "chat-image",
                    "baseUrl": "https://example.com",
                    "maxRetry": 1,
                }
            ],
            0,
            "画一只猫",
            [],
        )
    )
    assert result == [b"image"]
    request = chat.completions.create.call_args.kwargs
    assert request["model"] == "chat-image"
    assert request["messages"][0]["content"][0]["text"] == "画一只猫"
