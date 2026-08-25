"""积分数据的 JSON 持久化和业务操作。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class Point:
    def __init__(self, path: str | Path, config: Any | None = None):
        self.path = Path(path)
        self.config = config or {}
        self.enabled = bool(self.get("enable_points", True))
        self.cost = self.number("draw_cost_per_image", 5, 0)
        self.reward = self.number("points_per_message", 1, 0)
        self.cooldown = self.number("message_point_cooldown_seconds", 30, 0)
        self.initial = self.number("new_user_points", 10, 0)
        self.users: dict[str, dict[str, Any]] = {}
        self.load()

    def check(self, uid: str) -> str | None:
        if not self.enabled or self.cost <= 0:
            return None
        user = self.user(uid)
        return None if user["points"] >= self.cost else f"积分不足：当前 {user['points']} 分，需要 {self.cost} 分。"

    def spend(self, uid: str, amount: int | None = None) -> int:
        value = self.cost if amount is None else max(0, int(amount))
        if not self.enabled or value <= 0:
            return 0
        user = self.user(uid)
        actual = min(user["points"], value)
        user["points"] -= actual
        user["spent"] += actual
        self.save()
        return actual

    def refund(self, uid: str, amount: int) -> int:
        value = max(0, int(amount))
        if not self.enabled or value <= 0:
            return 0
        user = self.user(uid)
        user["points"] += value
        user["spent"] = max(0, user["spent"] - value)
        self.save()
        return value

    def penalize(self, uid: str, amount: int, policy: bool | str = False) -> int:
        """只有明确给出策略（True 或非空名称）时才扣分。"""
        if not policy:
            return 0
        return self.spend(uid, amount)

    def give(self, uid: str, amount: int) -> int:
        return self.change(uid, int(amount))

    def set(self, uid: str, value: int) -> int:
        user = self.user(uid)
        before = user["points"]
        user["points"] = max(0, int(value))
        self.record(user, user["points"] - before)
        self.save()
        return user["points"]

    def rank(self, limit: int = 10) -> list[dict[str, Any]]:
        users = sorted(self.users.values(), key=lambda item: item["points"], reverse=True)
        return [dict(user) for user in users[:max(0, int(limit))]]

    def talk(self, uid: str, name: str = "") -> int:
        if not self.enabled or self.reward <= 0:
            return 0
        user = self.user(uid, name)
        now = time.time()
        if now - user["lastTalk"] < self.cooldown:
            return 0
        user.update(lastTalk=now, name=name or user["name"])
        user["points"] += self.reward
        user["earned"] += self.reward
        user["talk"] += 1
        self.save()
        return self.reward

    def change(self, uid: str, amount: int) -> int:
        user = self.user(uid)
        actual = amount if amount >= 0 else -min(user["points"], -amount)
        user["points"] += actual
        self.record(user, actual)
        self.save()
        return user["points"]

    @staticmethod
    def record(user: dict[str, Any], delta: int) -> None:
        if delta > 0:
            user["earned"] += delta
        elif delta < 0:
            user["spent"] += -delta

    def user(self, uid: str, name: str = "") -> dict[str, Any]:
        key = str(uid).strip()
        if key not in self.users:
            self.users[key] = {"uid": key, "name": name, "points": self.initial, "talk": 0, "earned": self.initial, "spent": 0, "lastTalk": 0.0}
        elif name:
            self.users[key]["name"] = name
        return self.users[key]

    def load(self) -> None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            self.users = value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            self.users = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.users, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def get(self, name: str, default: Any) -> Any:
        if isinstance(self.config, dict):
            return self.config.get(name, default)
        return getattr(self.config, name, default)

    def number(self, name: str, default: int, low: int) -> int:
        try:
            return max(low, int(self.get(name, default)))
        except (TypeError, ValueError):
            return default
