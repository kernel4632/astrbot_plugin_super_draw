import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_super_draw.draw.picture import picture


def test_images_decode_embedded_data():
    encoded = base64.b64encode(b"image").decode()

    assert asyncio.run(picture.download(f"base64://{encoded}")) == b"image"
    assert asyncio.run(picture.download(f"data:image/png;base64,{encoded}")) == b"image"


def test_images_reject_local_file_paths():
    assert asyncio.run(picture.download("C:/secret/image.png", local=True)) is None


def test_reply_fetches_original_message_images():
    from astrbot.api import message_components as Comp

    raw = b"\x89PNG\r\n\x1a\n" + b"x" * 20
    calls = []

    async def call_action(name, **kwargs):
        calls.append((name, kwargs))
        return {"message": [{"type": "image", "data": {"file": "base64://" + base64.b64encode(raw).decode()}}]}

    reply = Comp.Reply(id="123", chain=[])
    event = SimpleNamespace(
        message_obj=SimpleNamespace(message=[reply], raw_message={"self_id": "bot"}),
        bot=SimpleNamespace(call_action=call_action),
        message_str="",
    )

    result = asyncio.run(picture.collect(event))

    assert result == [raw]
    assert calls == [("get_msg", {"message_id": 123, "self_id": "bot"})]


def test_reply_fetches_original_when_inline_chain_has_no_image():
    from astrbot.api import message_components as Comp

    raw = b"\x89PNG\r\n\x1a\n" + b"x" * 20

    async def call_action(name, **kwargs):
        return {"message": [{"type": "image", "data": {"file": "base64://" + base64.b64encode(raw).decode()}}]}

    event = SimpleNamespace(
        message_obj=SimpleNamespace(
            message=[Comp.Reply(id="123", chain=[Comp.Plain("quoted text")])]
        ),
        bot=SimpleNamespace(call_action=call_action),
        message_str="",
    )

    assert asyncio.run(picture.collect(event)) == [raw]


def test_forward_fetches_nested_images_with_standard_and_legacy_fallback():
    from astrbot.api import message_components as Comp

    raw = b"\xff\xd8\xff" + b"x" * 20
    calls = []

    async def call_action(name, **kwargs):
        calls.append((name, kwargs))
        if "message_id" in kwargs:
            raise RuntimeError("old adapter")
        return {"data": {"messages": [{"content": [{"type": "image", "data": {"file": "base64://" + base64.b64encode(raw).decode()}}]}]}}

    forward = Comp.Forward(id="456")
    event = SimpleNamespace(
        message_obj=SimpleNamespace(message=[forward], raw_message={"self_id": "bot"}),
        bot=SimpleNamespace(call_action=call_action),
        message_str="",
    )

    result = asyncio.run(picture.collect(event))

    assert result == [raw]
    assert calls[0] == ("get_forward_msg", {"message_id": 456, "self_id": "bot"})
    assert calls[-1] == ("get_forward_msg", {"id": 456, "self_id": "bot"})


def test_image_component_reads_local_path(tmp_path: Path):
    raw = b"GIF89a" + b"x" * 20
    path = tmp_path / "reference.gif"
    path.write_bytes(raw)
    from astrbot.api import message_components as Comp

    image = Comp.Image(path=str(path), file="")
    event = SimpleNamespace(
        message_obj=SimpleNamespace(message=[image]),
        message_str="",
    )

    assert asyncio.run(picture.collect(event)) == [raw]


def test_image_component_reads_windows_file_uri(tmp_path: Path):
    raw = b"\x89PNG\r\n\x1a\n" + b"x" * 20
    path = tmp_path / "reference.png"
    path.write_bytes(raw)
    from astrbot.api import message_components as Comp

    image = Comp.Image(file=path.as_uri())
    event = SimpleNamespace(
        message_obj=SimpleNamespace(message=[image]),
        message_str="",
    )

    assert asyncio.run(picture.collect(event)) == [raw]
