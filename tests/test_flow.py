import asyncio
from types import SimpleNamespace

from astrbot_plugin_super_draw.draw.flow import DrawRequest, Flow
from astrbot_plugin_super_draw.draw.model import ModelFailure


def build(tmp_path, monkeypatch, kind: str):
    config = {
        "api_providers": [
            {
                "name": "Images",
                "api_type": "openai",
                "api_keys": ["key"],
                "available_models": ["image"],
            }
        ],
        "points": {"new_user_points": 100, "draw_cost_per_image": 10, "bad_request_penalty_points": 50},
    }
    app = Flow(SimpleNamespace(), config, tmp_path)

    async def fail(*args, **kwargs):
        raise ModelFailure(kind, "failed")

    async def reply(*args, **kwargs):
        return None

    monkeypatch.setattr("astrbot_plugin_super_draw.draw.flow.model.draw", fail)
    monkeypatch.setattr("astrbot_plugin_super_draw.draw.flow.send.failure", reply)
    return app


def test_request_failure_refunds_the_reserved_points(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, "request")
    request = DrawRequest("u1", "origin", "message", "cat")

    async def run():
        await app.draw(request)
        await next(iter(app.tasks.values())).task

    asyncio.run(run())

    assert app.point.users["u1"]["points"] == 100


def test_policy_failure_refunds_then_applies_penalty(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, "policy")
    request = DrawRequest("u1", "origin", "message", "cat")

    async def run():
        await app.draw(request)
        await next(iter(app.tasks.values())).task

    asyncio.run(run())

    assert app.point.users["u1"]["points"] == 50
