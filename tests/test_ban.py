from astrbot_plugin_super_draw.user.ban import ban


def test_ban_adds_checks_and_removes_user():
    values = []
    saves = []

    assert ban.change(values, "add", "123", lambda: saves.append(True)) == "已将 123 加入黑名单。"
    assert ban.check(values, "123")
    assert ban.change(values, "remove", "123", lambda: saves.append(True)) == "已将 123 移出黑名单。"
    assert not ban.check(values, "123")
    assert saves == [True, True]
