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
