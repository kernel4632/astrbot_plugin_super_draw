import asyncio
import base64
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest


models = importlib.import_module("astrbot_plugin_super_draw.draw.model")


def test_openai_generate_decodes_b64(monkeypatch):
    client = SimpleNamespace(images=SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(b"image").decode())]))))
    monkeypatch.setattr(models, "AsyncOpenAI", lambda **kwargs: client)
    result = asyncio.run(models.model.draw({"model": "image", "apiKey": "key"}, "cat", []))
    assert result == [b"image"]
    assert client.images.generate.call_args.kwargs["n"] == 1


def test_openai_edit_uses_official_multipart(monkeypatch):
    seen = {}
    monkeypatch.setattr(models, "normalize_to_supported_image", lambda data, target_fmt: (data, "image/png"))

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"b64_json": base64.b64encode(b"edited").decode()}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            seen.update(url=url, **kwargs)
            return Response()

    monkeypatch.setattr(models.httpx, "AsyncClient", lambda **kwargs: Client())
    result = asyncio.run(models.model.draw({"model": "image", "apiKey": "key", "baseUrl": "https://x.test"}, "edit", [b"raw"]))
    assert result == [b"edited"]
    assert seen["url"] == "https://x.test/v1/images/edits"
    assert seen["files"][0][0] == "image"


def test_openai_400_is_request_and_not_retried(monkeypatch):
    class Response:
        status_code = 400
        text = "bad request"

    error = httpx.HTTPStatusError("bad request", request=httpx.Request("POST", "https://x"), response=Response())
    client = SimpleNamespace(images=SimpleNamespace(generate=AsyncMock(side_effect=error)))
    monkeypatch.setattr(models, "AsyncOpenAI", lambda **kwargs: client)
    with pytest.raises(models.ModelFailure) as caught:
        asyncio.run(models.model.draw({"model": "image", "apiKey": "key", "maxRetry": 3}, "cat", []))
    assert caught.value.kind == "request"
    assert client.images.generate.await_count == 1


def test_policy_error_is_policy_and_not_retried(monkeypatch):
    error = RuntimeError("content_policy_violation")
    client = SimpleNamespace(images=SimpleNamespace(generate=AsyncMock(side_effect=error)))
    monkeypatch.setattr(models, "AsyncOpenAI", lambda **kwargs: client)
    with pytest.raises(models.ModelFailure) as caught:
        asyncio.run(models.model.draw({"model": "image", "apiKey": "key", "maxRetry": 3}, "cat", []))
    assert caught.value.kind == "policy"
    assert client.images.generate.await_count == 1


def test_error_extracts_message_without_url_ip_or_email():
    class Response:
        text = '{"error":{"message":"图片违反内容政策","code":"content_policy_violation","account_email":"shire_gorges_2o@icloud.com"}}'

    error = httpx.HTTPStatusError(
        "Client error '400 Bad Request' for url 'http://154.12.29.232:3000/v1/images/edits'",
        request=httpx.Request("POST", "http://154.12.29.232:3000/v1/images/edits"),
        response=Response(),
    )

    failure = models.failure(error)

    assert failure.message == "图片违反内容政策"
    assert "154.12.29.232" not in failure.message
    assert "icloud.com" not in failure.message


def test_error_sanitizes_fallback_exception():
    failure = models.failure(
        RuntimeError("request failed for url 'http://154.12.29.232:3000' with sk-testkey12345678")
    )

    assert failure.message == "request failed"
