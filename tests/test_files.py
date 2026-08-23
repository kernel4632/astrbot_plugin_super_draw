from astrbot_plugin_super_draw.files import files


def test_temp_image_is_removed_after_use():
    path = files.save(b"image")

    assert path is not None
    assert files.remove(path)
    assert not files.remove(path)
