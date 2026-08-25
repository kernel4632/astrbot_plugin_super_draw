"""生图黑名单的检查、查看、添加和删除。"""

from typing import Any, Callable


class Ban:
    def check(self, values: list[str], user_id: str) -> bool:
        return str(user_id).strip() in values

    def change(self, values: list[str], action: str, user_id: str, save: Callable[[], Any]) -> str:
        action, user_id = action.strip().lower(), user_id.strip()
        if action == "list":
            return "黑名单：\n" + "\n".join(f"  - {item}" for item in values) if values else "黑名单为空。"
        if action == "add":
            if not user_id:
                return "用户 ID 不能为空。"
            if user_id in values:
                return f"{user_id} 已在黑名单中。"
            values.append(user_id)
            result = f"已将 {user_id} 加入黑名单。"
        elif action == "remove":
            if user_id not in values:
                return f"{user_id} 不在黑名单中。"
            values.remove(user_id)
            result = f"已将 {user_id} 移出黑名单。"
        else:
            return "可用 action：list、add、remove"
        save()
        return result


ban = Ban()
