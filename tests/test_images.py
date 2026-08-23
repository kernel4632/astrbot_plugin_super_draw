import asyncio
import base64

from astrbot_plugin_super_draw.images import images


def test_images_decode_embedded_data():
    encoded = base64.b64encode(b"image").decode()

    assert asyncio.run(images.download(f"base64://{encoded}")) == b"image"
    assert asyncio.run(images.download(f"data:image/png;base64,{encoded}")) == b"image"


def test_images_reject_local_file_paths():
    assert asyncio.run(images.download("C:/secret/image.png")) is None
