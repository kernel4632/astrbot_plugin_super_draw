"""AstrBot 超级生图插件入口。"""

from __future__ import annotations

import re
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

    def reply_urls(self, limit: int = 0) -> list[str]:
        message = getattr(self.event, "message_obj", None)
        urls = []

        def full() -> bool:
            return limit > 0 and len(urls) >= limit

        def collect(value):
            if value is None or full():
                return
            if isinstance(value, Comp.Image):
                url = getattr(value, "url", "") or getattr(value, "path", "") or getattr(value, "file", "") or ""
                if url:
                    urls.append(url)
                return
            if isinstance(value, Comp.Reply):
                chain = getattr(value, "chain", None)
                if chain is not None:
                    collect(chain)
                return
            if isinstance(value, (list, tuple)):
                for item in value:
                    collect(item)
                    if full():
                        return
                return
            if not isinstance(value, dict):
                return
            kind = str(value.get("type") or "").lower()
            data = value.get("data") if isinstance(value.get("data"), dict) else value
            if kind == "image":
                for key in ("url", "path", "file"):
                    url = data.get(key, "") if isinstance(data, dict) else ""
                    if url:
                        urls.append(str(url))
                        return
            if kind == "reply":
                chain = data.get("chain") if isinstance(data, dict) else None
                if chain is not None:
                    collect(chain)
                return
            for key in ("message", "messages", "chain", "content", "nodes"):
                child = value.get(key)
                if child is not None and child is not value:
                    collect(child)
                    if full():
                        return

        for name in ("message", "raw_message"):
            raw_msg = getattr(message, name, None)
            if isinstance(raw_msg, list):
                collect(raw_msg)
            elif isinstance(raw_msg, dict):
                collect(raw_msg.get("message", []))
            if urls:
                break
        return urls

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
        result = await self.flow.draw(
            view.request(prompt, urls=view.reply_urls(self.flow.config.maxRefImages))
        )
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
        amount = 0
        if tokens and tokens[-1].lstrip("+-").isdigit():
            try:
                amount = int(tokens[-1])
            except ValueError:
                amount = 0
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
        urls: list[str] | None = None,
    ) -> str:
        """生成图片或基于参考图改图（文生图 / 图生图）。

        使用时机：
        - 用户要求画图、修图、P图、换风格、加删元素、生成头像、海报、表情包时
        - 用户说"把图一的角色换到图二""把这张图改成…"等多图参考需求时

        参考图说明：
        - 当前消息、引用消息、合并转发里的图片会被自动收集为参考图，不用填 urls，也不要自己在回复里描述图片内容
        - 参考图顺序：引用消息里的图在前，本条消息里的图在后；用户说的"图一/图二"按这个顺序理解
        - 提示词点名了其他群友（不是 @ 形式）时，可以先用 get_group_members_info 查到他的 QQ 号，再把头像链接 https://q1.qlogo.cn/g?b=qq&nk=QQ号&s=640 放进 urls 当参考图
        - urls 只用于补充网络图片链接（http/https）或参考头像，不要填本地文件路径

        注意：
        - 一次需求只调用一次；返回任务号后图片会由插件自动发送，不要重复调用生图工具

        Args:
            prompt(string): 必填。画面描述，写清楚内容、风格、比例，例如"一只橘猫坐在窗边看雨，水彩风格"
            urls(list[string]): 可选。补充参考图的网络地址列表，例如群友头像 https://q1.qlogo.cn/g?b=qq&nk=QQ号&s=640；当前消息和引用消息里的图片会自动收集，不用填
        """
        if not prompt.strip():
            return "请提供生图描述。"
        if isinstance(urls, str):
            values = [url.strip() for url in urls.split(",") if url.strip()]
        elif isinstance(urls, (list, tuple)):
            values = [str(url).strip() for url in urls if str(url).strip()]
        else:
            values = []
        result = await self.flow.draw(Event(event).request(prompt, True, values))
        if result.startswith("生图任务已开始"):
            result += "\n（图片稍后自动发送，无需重复调用生图工具）"
        return result

    @filter.llm_tool(name="super_draw_data")
    async def tool_data(
        self,
        event: AstrMessageEvent,
        action: str = "",
        user_key: str = "",
        delta: int = 0,
        reason: str = "",
    ) -> str:
        """查询或修改生图积分与使用数据。

        action 可选值：
        - my_points：查询当前用户的积分，最常用
        - user_points：查询指定用户的积分，需要填 user_key
        - rank：查看积分排行榜
        - summary：查看生图开关、当前模型、用户数等概览
        - change_points：给指定用户增减积分，仅管理员/群主可用，需要 user_key 和 delta
        - set_points：把指定用户的积分设为某个值，仅管理员/群主可用，需要 user_key 和 delta

        注意：
        - user_key 只能填纯数字 QQ 号，不要填群号、UMO、昵称或 @；留空表示当前用户
        - 普通成员调用 change_points / set_points 会被拒绝，不要重试

        Args:
            action(string): 必填。my_points/user_points/rank/summary/change_points/set_points
            user_key(string): 目标用户的纯数字 QQ 号，留空表示当前用户
            delta(number): change_points 是增减值（如 50、-20），set_points 是目标值
            reason(string): 修改原因，会记录在改分结果里
        """
        view = Event(event)
        role = str(getattr(event, "role", "") or "member")
        target = self.normalize_user_key(user_key) or view.user()
        return self.flow.data(action, target, delta, reason, role)

    @filter.llm_tool(name="super_draw_ban")
    async def tool_ban(
        self,
        event: AstrMessageEvent,
        action: str = "",
        user_id: str = "",
    ) -> str:
        """管理生图黑名单（拉黑 / 解除拉黑用户）。

        action 可选值：
        - list：查看当前黑名单
        - add：把用户加入黑名单，需要填 user_id
        - remove：把用户移出黑名单，需要填 user_id

        使用时机：用户明确要求拉黑或解封某人，或你判断某人持续滥用生图时调用。

        Args:
            action(string): 必填。list/add/remove
            user_id(string): 目标用户的纯数字 QQ 号，add/remove 时必填
        """
        if not action.strip():
            return "请提供 action：list、add、remove"
        return self.flow.ban(action, user_id)

    @staticmethod
    def normalize_user_key(value: str) -> str:
        """只接受纯数字 QQ 号，避免 UMO、群号等字符串污染积分数据。"""
        runs = [run for run in re.findall(r"\d+", value or "") if 5 <= len(run) <= 12]
        return runs[0] if len(runs) == 1 else ""

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group(self, event: AstrMessageEvent):
        view = Event(event)
        earned = self.flow.talk(view.user(), view.name())
        if earned and self.flow.config.debug:
            logger.info(f"[SuperDraw] +{earned}: {view.user()}")


__all__ = ["SuperDraw"]
