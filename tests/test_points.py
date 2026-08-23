from pathlib import Path

from astrbot_plugin_super_draw.points import Points


def test_request_failure_refunds_all_reserved_points(tmp_path: Path):
    points = Points(tmp_path / "points.json", {"new_user_points": 100})
    reserved = points.spend("u1", 10)

    points.refund("u1", reserved)

    assert points.users["u1"]["points"] == 100
    assert points.users["u1"]["spent"] == 0


def test_policy_failure_is_the_only_penalty_path(tmp_path: Path):
    points = Points(tmp_path / "points.json", {"new_user_points": 100})
    reserved = points.spend("u1", 10)

    points.refund("u1", reserved)
    assert points.penalize("u1", 50, policy=False) == 0
    assert points.users["u1"]["points"] == 100

    assert points.penalize("u1", 50, policy=True) == 50
    assert points.users["u1"]["points"] == 50
