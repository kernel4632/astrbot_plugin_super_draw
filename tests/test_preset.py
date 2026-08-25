from astrbot_plugin_super_draw.user.preset import preset


def test_preset_resolves_name_and_number():
    values = {"手办": "变成手办"}

    assert preset.resolve(values, "手办 猫") == ("变成手办 猫", "手办")
    assert preset.resolve(values, "1号 猫") == ("变成手办 猫", "手办")


def test_preset_adds_and_removes():
    values = {}
    saves = []

    assert preset.change(values, "添加 手办:变成手办", lambda: saves.append(True)) == "预设已添加：手办"
    assert values == {"手办": "变成手办"}
    assert preset.change(values, "删除 手办", lambda: saves.append(True)) == "预设已删除：手办"
    assert saves == [True, True]
