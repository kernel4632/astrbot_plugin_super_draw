"""
超级生图插件 4.0.0 入口。命令 + 工具 + 生图任务 + 发送，全在这一个文件。
命令走 AstrBot 标准 @filter.command，处理后 stop_event 阻止 LLM 接管。
工具走 @filter.llm_tool，Bot 自动调用。命令和工具共享同一个 draw() 函数。
"""

from __future__ import annotations

import asyncio  # 后台任务
import base64  # data URI 解码
import hashlib  # 生成任务 ID
import re  # 提取图片 URL、脱敏 Key
import time  # 任务计时
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp  # 消息组件：图片、@、回复
from astrbot.api import logger  # 日志
from astrbot.api.event import (
    AstrMessageEvent,
    MessageChain,
    filter,
)  # 命令、工具、消息链
from astrbot.api.star import Context, Star  # 插件基类
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.star_tools import StarTools  # 获取数据目录
from astrbot.core.utils.io import download_image_by_url

from .data import Data
from .generate import closeClients, makeImages
from .tool.file import saveImage


class SuperDraw(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cacheDir = StarTools.get_data_dir() / "cache"  # 图片临时目录
        self.data = Data(config, StarTools.get_data_dir())  # 数据中心
        self.tasks: dict[str, asyncio.Task] = {}  # taskId → asyncio.Task
        self.taskMeta: dict[str, dict] = {}  # taskId → {uid, prompt, start}

    async def initialize(self) -> None:
        self.cacheDir.mkdir(parents=True, exist_ok=True)
        if not self.data.providers:
            logger.error("[SuperDraw] 未配置模型，请在 api_providers 填写 Key 和模型。")
        else:
            logger.info(f"[SuperDraw] 超级生图插件 启动，模型：{self.data.modelKey}")

    async def terminate(self) -> None:
        for t in self.tasks.values():  # 取消所有后台任务
            if not t.done():
                t.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        self.taskMeta.clear()
        await closeClients()  # 关闭 HTTP 客户端

    # ==================== 用户命令 ====================
    # 每个命令：解析参数 → 调通用方法 → 返回结果 → stop_event

    @filter.command("生图")
    async def cmd_draw(self, event: AstrMessageEvent):
        """用户发 /生图 直接生图，不交给 LLM。"""
        images = await self._images(event)  # 从消息收集参考图
        prompt, preset = self.data.resolvePreset(self._body(event))  # 匹配预设
        result = await self._draw(event, prompt, images)  # 通用生图函数
        if preset:
            result += f"\n预设：{preset}"
        yield event.plain_result(result)
        event.stop_event()  # 阻止 LLM 接管

    @filter.command("生图取消")
    async def cmd_cancel(self, event: AstrMessageEvent):
        body = self._body(event).strip()  # 取命令参数（可能是任务编码）
        uid = self._uid(event)  # 当前发送者 QQ 号
        isAdmin = (
            getattr(event, "role", "") == "admin"
        )  # AstrBot 在 process 阶段设置 event.role = "admin"

        if body and isAdmin:  # 管理员带任务编码 → 取消指定任务
            task = self.tasks.get(body)
            if task and not task.done():
                task.cancel()
                self.taskMeta.pop(body, None)
                yield event.plain_result(f"已取消任务 {body}")
            else:
                yield event.plain_result(f"任务 {body} 不存在或已完成")
            event.stop_event()
            return

        for tid in reversed(list(self.tasks)):  # 普通用户 → 取消自己最近的任务
            meta = self.taskMeta.get(tid, {})
            if meta.get("uid") == uid and not self.tasks[tid].done():
                self.tasks[tid].cancel()
                self.taskMeta.pop(tid, None)
                yield event.plain_result("已取消你最近的生图任务，积分会自动退回")
                event.stop_event()
                return
        yield event.plain_result("你当前没有正在运行的任务。")
        event.stop_event()

    @filter.command("生图积分")
    async def cmd_points(self, event: AstrMessageEvent):
        yield event.plain_result(self.data.points(self._uid(event)))
        event.stop_event()

    @filter.command("生图预设")
    async def cmd_preset(self, event: AstrMessageEvent):
        yield event.plain_result(self.data.preset(self._body(event)))
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("生图模型")
    async def cmd_model(self, event: AstrMessageEvent):
        yield event.plain_result(self.data.model(self._body(event)))
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("生图开关")
    async def cmd_toggle(self, event: AstrMessageEvent):
        self.data.toggle()
        if not self.data.enabled:  # 关闭时取消所有任务
            for t in self.tasks.values():
                if not t.done():
                    t.cancel()
        yield event.plain_result(f"生图功能已{'开启' if self.data.enabled else '关闭'}")
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("生图改分")
    async def cmd_give(self, event: AstrMessageEvent):
        """管理员改分。用法：/生图改分 @用户 数量"""
        target = self._atTarget(event)  # 从 @ 组件取目标 QQ
        if not target:
            yield event.plain_result("用法：/生图改分 @用户 数量")
            event.stop_event()
            return
        tokens = [
            t for t in (event.message_str or "").split() if not t.startswith(("@", "/"))
        ]  # 取数字部分
        amount = int(tokens[-1]) if tokens and tokens[-1].lstrip("+-").isdigit() else 0
        if amount == 0:
            yield event.plain_result("请填写积分数量，例如：/生图改分 @用户 50")
            event.stop_event()
            return
        result = self.data.give(target, amount, "管理员改分")
        action = "赠送" if amount > 0 else "扣除"
        yield event.plain_result(f"已向 @{target} {action} {abs(amount)} 分，{result}")
        event.stop_event()

    # ==================== LLM 工具 ====================
    # 工具和命令共享同一个 _draw() 函数

    @filter.llm_tool(name="super_draw")
    async def tool_draw(
        self, event: AstrMessageEvent, prompt: str = "", urls: str = ""
    ) -> str:
        """生成图片。当用户想画图、修图、P图、生成头像、海报、表情包时调用。
        Args:
            prompt(string): 必填。图片描述，自然语言写清楚内容、风格、比例。例如"一只橘猫坐在窗边看雨，水彩风格"
            urls(string): 可选。参考图 URL，多张用逗号分隔，若用户想要参考头像则使用url：https://q1.qlogo.cn/g?b=qq&nk=QQ号&s=640
        """
        if not prompt.strip():
            return "请提供生图描述。"
        real = self._real(event)  # 兼容 ContextWrapper
        images: list[bytes] = []
        for url in [
            u.strip() for u in urls.split(",") if u.strip()
        ]:  # 下载工具传入的 URL
            if d := await self._dl(url):
                images.append(d)
        images.extend(await self._images(real))  # 再从消息收集
        return await self._draw(real, prompt.strip(), images, from_tool=True)

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
        real = self._real(event)
        uid = user_key.strip() or self._uid(real)
        return self.data.toolAction(action, uid, delta, reason)

    @filter.llm_tool(name="super_draw_ban")
    async def tool_ban(
        self, event: AstrMessageEvent, action: str = "", user_id: str = ""
    ) -> str:
        """管理生图黑名单。
        Args:
            action(string): 必填。list/add/remove
            user_id(string): 要操作的用户 ID
        """
        if not action.strip():
            return "请提供 action：list、add、remove"
        return self.data.ban(action.strip().lower(), user_id)

    # ==================== 群消息积分 ====================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group(self, event: AstrMessageEvent):
        earned = self.data.addTalk(self._uid(event), self._name(event))
        if earned and self.data.debug:
            logger.info(f"[SuperDraw] +{earned}: {self._uid(event)}")

    # ==================== 通用生图（命令和工具共享） ====================

    async def _draw(
        self,
        event: AstrMessageEvent,
        prompt: str,
        images: list[bytes],
        from_tool: bool = False,
    ) -> str:
        """通用生图入口。检查 → 扣分 → 启动任务 → 返回任务 ID。"""
        if not self.data.enabled:
            return "生图关了，等管理员打开"
        if from_tool and not self.data.enableTool:
            return "LLM 生图工具当前关闭。"
        if not prompt:
            return "\n".join(
                [
                    "超级生图插件指令：",
                    "  /生图 描述 — 生成图片（带图片则图生图）",
                    "  /生图取消 — 取消最近一个任务",
                    "  /生图积分 — 查看积分",
                    "  /生图预设 — 查看/添加/删除预设",
                    "  /生图模型 — 查看或切换模型（管理员）",
                    "  /生图开关 — 切换总开关（管理员）",
                    "  /生图改分 @用户 数量 — 加减积分（管理员）",
                    "",
                    "示例：/生图 一只猫坐在窗边看雨",
                ]
            )
        uid = self._uid(event)
        if self.data.isBanned(uid):
            return "你在黑名单，画不了"
        if msg := self.data.check(uid):
            return msg  # 积分不够
        active = sum(1 for t in self.tasks.values() if not t.done())
        if active >= self.data.maxQueue:
            return f"队列已满（{active}/{self.data.maxQueue}），请稍后"
        cost = self.data.spend(uid)  # 预扣积分
        tid = hashlib.md5(f"{time.time()}|{uid}|{prompt[:80]}".encode()).hexdigest()[
            :4
        ]  # 4 位任务 ID
        req = {
            "uid": uid,
            "umo": event.unified_msg_origin,
            "prompt": prompt,
            "images": images[:8],
            "cost": cost,
            "from_tool": from_tool,
            "event": event if from_tool else None,
        }
        self._clean()  # 清理已完成的旧任务
        self.taskMeta[tid] = {"uid": uid, "prompt": prompt[:40], "start": time.time()}
        self.tasks[tid] = asyncio.create_task(self._run(tid, req))
        info = f"生图任务已开始：{tid}\n模型：{self.data.modelKey}"
        if req["images"]:
            info += f"\n参考图：{len(req['images'])}张"  # 有参考图时告诉用户收集了几张
        return info

    async def _run(self, tid: str, req: dict) -> None:
        """后台执行生图。成功发图，失败退积分。"""
        try:
            result = await makeImages(
                self.data.providers,
                self.data.modelIndex,
                req["prompt"],
                req["images"],
                "auto",
                "auto",
                1,
                self.data.nextKey,
            )
            paths = []  # 保存并发送图片
            chain = MessageChain().message(
                f"生图完成：{tid}\n模型：{self.data.modelKey}"
            )
            for img in result:
                p = saveImage(self.cacheDir, img, "png")
                if p:
                    paths.append(p)
                    chain.file_image(p)
            await self.context.send_message(req["umo"], chain)
            if req.get("from_tool") and self.data.enableComment:  # 工具生图后评价
                await self._comment(req, paths)
        except asyncio.CancelledError:  # 用户取消
            self.data.refund(req["uid"], req["cost"])
            await self.context.send_message(
                req["umo"],
                MessageChain().message(f"生图任务 {tid} 已取消，积分退给你了"),
            )
        except Exception as e:  # 生图失败
            logger.error(f"[SuperDraw] {tid} 失败: {e}")
            if self._is400(e):  # 400 内容安全错误 → 扣惩罚分
                penalty = self.data.settle400(req["uid"], req["cost"])
                await self.context.send_message(
                    req["umo"],
                    MessageChain().message(
                        f"生图失败（{tid}）：图违规了，扣 {penalty} 分\n{self._safe(e)}"
                    ),
                )
            else:  # 其他错误 → 全额退回
                self.data.refund(req["uid"], req["cost"])
                await self.context.send_message(
                    req["umo"],
                    MessageChain().message(
                        f"生图失败（{tid}），积分退给你了：{self._safe(e)}"
                    ),
                )
        finally:
            self.taskMeta.pop(tid, None)

    async def _comment(self, req: dict, paths: list[str]) -> None:
        """工具生图后追加自然评价。失败不影响发图。"""
        event = req.get("event")
        if not event:
            return
        try:
            ctx = ""  # 尝试读会话历史
            try:
                mgr = self.context.conversation_manager
                cid = await mgr.get_curr_conversation_id(event.unified_msg_origin)
                if cid:
                    conv = await mgr.get_conversation(event.unified_msg_origin, cid)
                    ctx = str(getattr(conv, "history", "") or "").strip()[-2000:]
            except:
                pass
            if not ctx:
                ctx = (event.message_str or "")[-500:]
            img_info = f"已发送 {len(paths)} 张图片。" if paths else "图片已生成。"
            prompt = self.data.commentPrompt(req["prompt"], ctx, img_info)
            pid = (
                self.data.commentProvider
                or await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
            )
            if not pid:
                return
            resp = await self.context.llm_generate(chat_provider_id=pid, prompt=prompt)
            text = str(getattr(resp, "completion_text", "") or "").strip()[
                : self.data.commentMaxLen
            ]
            if text:
                await self.context.send_message(
                    req["umo"], MessageChain().message(text)
                )
        except Exception as e:
            logger.warning(f"[SuperDraw] 评价失败: {e}")

    # ==================== 图片收集 ====================

    async def _images(self, event: AstrMessageEvent) -> list[bytes]:
        """从消息、回复、合并转发、@头像和文本 URL 中收集参考图。"""
        msg = getattr(
            event, "message_obj", None
        )  # 兼容 ContextWrapper 没有 message_obj
        if not msg or not getattr(msg, "message", None):
            return []
        result: list[bytes] = []
        forwardIds: set[str] = set()
        for i, c in enumerate(msg.message):
            if i == 0 and isinstance(c, Comp.At):
                continue  # 跳过开头 @机器人
            if isinstance(c, Comp.At) and str(getattr(c, "qq", "")) not in (
                "",
                "all",
            ):  # @用户头像
                if d := await self._dl(
                    f"https://q4.qlogo.cn/headimg_dl?dst_uin={c.qq}&spec=640"
                ):
                    result.append(d)
            else:
                await self._collectImagesRecursive(c, event, result, forwardIds)
            if len(result) >= 8:
                return result[:8]
        for url in re.findall(
            r"https?://[^\s]+", event.message_str or ""
        ):  # 文本里的 URL
            if d := await self._dl(url.rstrip("，。,.）)")):
                result.append(d)
            if len(result) >= 8:
                break
        return result[:8]  # 最多 8 张

    async def _collectImagesRecursive(
        self,
        value: Any,
        event: AstrMessageEvent,
        result: list[bytes],
        forwardIds: set[str],
    ) -> None:
        """递归读取 AstrBot 组件或 OneBot 合并转发原始数据中的图片。"""
        if value is None or len(result) >= 8:
            return
        if isinstance(value, Comp.Image):
            src = value.url or getattr(value, "path", "") or value.file
            if d := await self._dl(src):
                result.append(d)
            return
        if isinstance(value, Comp.Reply):
            await self._collectImagesRecursive(value.chain, event, result, forwardIds)
            return
        if isinstance(value, Comp.Node):
            await self._collectImagesRecursive(value.content, event, result, forwardIds)
            return
        if isinstance(value, Comp.Nodes):
            await self._collectImagesRecursive(value.nodes, event, result, forwardIds)
            return
        if isinstance(value, Comp.Forward):
            await self._collectForward(value.id, event, result, forwardIds)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                await self._collectImagesRecursive(item, event, result, forwardIds)
                if len(result) >= 8:
                    break
            return
        if not isinstance(value, dict):
            return

        kind = str(value.get("type") or "").lower()
        data = value.get("data") if isinstance(value.get("data"), dict) else value
        if kind == "image":
            src = data.get("url") or data.get("path") or data.get("file")
            if d := await self._dl(src):
                result.append(d)
            return
        if kind == "forward":
            forwardId = data.get("id") or data.get("message_id")
            if forwardId:
                await self._collectForward(str(forwardId), event, result, forwardIds)
            return
        for key in ("messages", "message", "nodes", "content", "data"):
            child = value.get(key)
            if child is not None and child is not value:
                await self._collectImagesRecursive(child, event, result, forwardIds)
                if len(result) >= 8:
                    break

    async def _collectForward(
        self,
        forwardId: str,
        event: AstrMessageEvent,
        result: list[bytes],
        forwardIds: set[str],
    ) -> None:
        """通过 OneBot API 展开只有 ID 的 Forward 组件。"""
        if not forwardId or forwardId in forwardIds:
            return
        forwardIds.add(forwardId)
        bot = getattr(event, "bot", None)
        callAction = getattr(bot, "call_action", None)
        if not callable(callAction):
            return
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        selfId = (
            raw.get("self_id")
            if isinstance(raw, dict)
            else getattr(raw, "self_id", None)
        )
        routing = {"self_id": selfId} if selfId else {}
        try:
            payload = await callAction(
                "get_forward_msg", message_id=forwardId, **routing
            )
        except Exception:
            try:
                payload = await callAction("get_forward_msg", id=forwardId, **routing)
            except Exception as e:
                logger.warning(f"[SuperDraw] 读取合并转发消息失败: {e}")

    # ==================== 工具方法 ====================

    def _uid(self, event: AstrMessageEvent) -> str:
        """取发送者 QQ 号。"""
        if callable(getattr(event, "get_sender_id", None)):
            return str(event.get_sender_id() or "")
        msg = getattr(event, "message_obj", None)
        raw = getattr(msg, "raw_message", None) if msg else None
        if isinstance(raw, dict):
            return str(raw.get("user_id") or raw.get("sender", {}).get("user_id") or "")
        return str(getattr(raw, "user_id", "") or "")

    def _name(self, event: AstrMessageEvent) -> str:
        """取发送者昵称。"""
        if callable(getattr(event, "get_sender_name", None)):
            return str(event.get_sender_name() or "")
        return self._uid(event) or "群友"

    def _body(self, event: AstrMessageEvent) -> str:
        """取命令参数部分。/生图 猫 → 猫"""
        t = (event.message_str or "").strip()
        return t.split(maxsplit=1)[1].strip() if " " in t else ""

    def _real(self, event: Any) -> AstrMessageEvent:
        """兼容 AstrBot v4.26 ContextWrapper：提取真实事件。"""
        if isinstance(event, AstrMessageEvent):
            return event
        for attr in ["event", "context"]:  # 递归找 AstrMessageEvent
            inner = getattr(event, attr, None)
            if isinstance(inner, AstrMessageEvent):
                return inner
            deeper = getattr(inner, "event", None)
            if isinstance(deeper, AstrMessageEvent):
                return deeper
        return event  # 找不到就原样返回

    def _atTarget(self, event: AstrMessageEvent) -> str:
        """从消息的 @ 组件取目标 QQ 号。"""
        msg = getattr(event, "message_obj", None)
        for c in getattr(msg, "message", []) if msg else []:
            if isinstance(c, Comp.At):
                qq = str(getattr(c, "qq", "") or "")
                selfId = str(getattr(msg, "self_id", "") or "")
                if qq and qq != selfId and qq != "all":
                    return qq
        return ""

    def _clean(self) -> None:
        """清理已完成的旧任务。"""
        for tid in [k for k, t in self.tasks.items() if t.done()]:
            self.tasks.pop(tid, None)
            self.taskMeta.pop(tid, None)

    def _is400(self, e: Exception) -> bool:
        """判断是否 400 内容安全错误。只匹配明确关键词，避免 base64 数据误触发。"""
        t = str(e).lower()
        return (
            "content_policy" in t
            or "content policy" in t
            or "invalid_request_error" in t
            or "error code: 400" in t
            or "status code: 400" in t
            or "bad request" in t
        )

    def _safe(self, e: Exception) -> str:
        """脱敏错误信息"""
        return re.sub(r"(sk-|key-|AIza)[A-Za-z0-9_-]{8,}", "***", str(e))
