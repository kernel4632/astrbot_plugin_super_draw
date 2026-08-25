from astrbot_plugin_super_draw.draw.temp import temp


def test_temp_image_is_removed_after_use():
    path = temp.save(b"image")

    assert path is not None
    assert temp.remove(path)
    assert not temp.remove(path)
