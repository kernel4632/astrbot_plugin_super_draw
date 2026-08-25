"""AstrBot 超级生图插件入口。"""

from __future__ import annotations

from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.star_tools import StarTools

from .draw.flow import Flow
from .draw.task import DrawRequest


class Event:
    """把 AstrBot 事件转换成业务层需要的少量字段。"""

    def __init__(self, event: Any):
        inner = getattr(event, "event", None)
        self.event = inner if isinstance(inner, AstrMessageEvent) else event

    def request(
        self,
        prompt: str,
        from_tool: bool = False,
        urls: list[str] | None = None,
    ) -> DrawRequest:
        event = self.event
        return DrawRequest(
            user_id=self.user(),
            origin=str(getattr(event, "unified_msg_origin", "") or ""),
            message_id=self.message(),
            prompt=prompt.strip(),
            from_tool=from_tool,
            source=event,
            urls=urls or [],
            message_text=str(getattr(event, "message_str", "") or ""),
        )

    def user(self) -> str:
        event = self.event
        getter = getattr(event, "get_sender_id", None)
        if callable(getter):
            return str(getter() or "")
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict):
            sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
            return str(raw.get("user_id") or sender.get("user_id") or "")
        return str(getattr(raw, "user_id", "") or "")

    def name(self) -> str:
        getter = getattr(self.event, "get_sender_name", None)
        return str(getter() or "") if callable(getter) else self.user() or "群友"

    def body(self) -> str:
        text = str(getattr(self.event, "message_str", "") or "").strip()
        return text.split(maxsplit=1)[1].strip() if " " in text else ""

    def message(self) -> str:
        message = getattr(self.event, "message_obj", None)
        value = getattr(message, "message_id", None)
        if value:
            return str(value)
        raw = getattr(message, "raw_message", None)
        value = raw.get("message_id") if isinstance(raw, dict) else getattr(raw, "message_id", None)
        return str(value) if value else ""

    def target(self) -> str:
        message = getattr(self.event, "message_obj", None)
        self_id = str(getattr(message, "self_id", "") or "")
        for component in getattr(message, "message", []) if message else []:
            if isinstance(component, Comp.At):
                user_id = str(getattr(component, "qq", "") or "")
                if user_id and user_id not in {self_id, "all"}:
                    return user_id
        return ""


class SuperDraw(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.flow = Flow(context, config, StarTools.get_data_dir())

    async def initialize(self) -> None:
        if not self.flow.config.providers:
            logger.error("[SuperDraw] 未配置模型，请在 api_providers 填写 Key 和模型。")
        else:
            logger.info(f"[SuperDraw] 超级生图插件启动，模型：{self.flow.config.modelKey}")

    async def terminate(self) -> None:
        await self.flow.close()

    @filter.command("生图")
    async def cmd_draw(self, event: AstrMessageEvent):
        view = Event(event)
        prompt, preset = self.flow.resolve(view.body())
        result = await self.flow.draw(view.request(prompt))
        if preset:
            result += f"\n预设：{preset}"
        yield event.plain_result(result)
        event.stop_event()

    @filter.command("生图取消")
    async def cmd_cancel(self, event: AstrMessageEvent):
        view = Event(event)
        result = self.flow.cancel(
            view.user(),
            view.body().strip(),
            getattr(event, "role", "") == "admin",
        )
        yield event.plain_result(result)
        event.stop_event()

    @filter.command("生图积分")
    async def cmd_points(self, event: AstrMessageEvent):
        yield event.plain_result(self.flow.balance(Event(event).user()))
        event.stop_event()

    @filter.command("生图预设")
    async def cmd_preset(self, event: AstrMessageEvent):
        yield event.plain_result(self.flow.preset(Event(event).body()))
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("生图模型")
    async def cmd_model(self, event: AstrMessageEvent):
        yield event.plain_result(self.flow.model(Event(event).body()))
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("生图开关")
    async def cmd_toggle(self, event: AstrMessageEvent):
        enabled = self.flow.toggle()
        yield event.plain_result(f"生图功能已{'开启' if enabled else '关闭'}")
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("生图改分")
    async def cmd_give(self, event: AstrMessageEvent):
        view = Event(event)
        target = view.target()
        if not target:
            yield event.plain_result("用法：/生图改分 @用户 数量")
            event.stop_event()
            return
        tokens = [
            token
            for token in (event.message_str or "").split()
            if not token.startswith(("@", "/"))
        ]
        amount = int(tokens[-1]) if tokens and tokens[-1].lstrip("+-").isdigit() else 0
        if amount == 0:
            yield event.plain_result("请填写积分数量，例如：/生图改分 @用户 50")
            event.stop_event()
            return
        result = self.flow.give(target, amount, "管理员改分")
        action = "赠送" if amount > 0 else "扣除"
        yield event.plain_result(f"已向 @{target} {action} {abs(amount)} 分，{result}")
        event.stop_event()

    @filter.llm_tool(name="super_draw")
    async def tool_draw(
        self,
        event: AstrMessageEvent,
        prompt: str = "",
        urls: str = "",
    ) -> str:
        """生成图片。当用户想画图、修图、P图、生成头像、海报、表情包时调用。
        Args:
            prompt(string): 必填。图片描述，自然语言写清楚内容、风格、比例。例如"一只橘猫坐在窗边看雨，水彩风格"
            urls(string): 可选。参考图 URL，多张用逗号分隔，若用户想要参考头像则使用url：https://q1.qlogo.cn/g?b=qq&nk=QQ号&s=640
        """
        if not prompt.strip():
            return "请提供生图描述。"
        values = [url.strip() for url in urls.split(",") if url.strip()]
        return await self.flow.draw(Event(event).request(prompt, True, values))

    @filter.llm_tool(name="super_draw_data")
    async def tool_data(
        self,
        event: AstrMessageEvent,
        action: str = "",
        user_key: str = "",
        delta: int = 0,
        reason: str = "",
    ) -> str:
        """查询或修改生图数据和积分。
        Args:
            action(string): 必填。summary/my_points/user_points/change_points/set_points/rank
            user_key(string): 目标用户，空则为当前用户
            delta(number): change_points 增减值，set_points 目标值
            reason(string): 修改原因
        """
        view = Event(event)
        return self.flow.data(action, user_key.strip() or view.user(), delta, reason)

    @filter.llm_tool(name="super_draw_ban")
    async def tool_ban(
        self,
        event: AstrMessageEvent,
        action: str = "",
        user_id: str = "",
    ) -> str:
        """管理生图黑名单。
        Args:
            action(string): 必填。list/add/remove
            user_id(string): 要操作的用户 ID
        """
        if not action.strip():
            return "请提供 action：list、add、remove"
        return self.flow.ban(action, user_id)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group(self, event: AstrMessageEvent):
        view = Event(event)
        earned = self.flow.talk(view.user(), view.name())
        if earned and self.flow.config.debug:
            logger.info(f"[SuperDraw] +{earned}: {view.user()}")


__all__ = ["SuperDraw"]
