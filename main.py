"""
AstrBot 超级生图插件 4.0.0 入口。

事件 → 指令 → 数据 → 反馈。
用户命令走 AstrBot 标准 @filter.command，直接处理并停止事件传播，不会再交给 LLM。
Bot 想生图时通过 super_draw 工具调用，走 LLM 工具链路。

命令：
    /生图 一只猫坐在窗边看雨
    /取消生图
    /生图积分
    /生图预设
    /生图模型 2
    /生图开关

LLM 工具：
    super_draw        让 Bot 生图
    super_draw_data   让 Bot 查改积分
    super_draw_ban    让 Bot 管黑名单
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.star_tools import StarTools
from astrbot.core.utils.io import download_image_by_url

from .data import Data
from .generate import closeClients, makeImages
from .tool.file import saveImage


class SuperDraw(Star):
    """超级生图插件 4.0.0。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.dataDir = StarTools.get_data_dir()
        self.cacheDir = self.dataDir / "cache"
        self.data = Data(config, self.dataDir)
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self.taskInfo: dict[str, dict[str, Any]] = {}

    # ========== 生命周期 ==========

    async def initialize(self) -> None:
        self.cacheDir.mkdir(parents=True, exist_ok=True)
        if not self.data.providers:
            logger.error("[SuperDraw] 未配置可用模型，请在 api_providers 里填写 api_keys 和 available_models。")
            return
        logger.info(f"[SuperDraw] 4.0.0 启动完成，当前模型：{self.data.currentModelKey}")

    async def terminate(self) -> None:
        for task in list(self.tasks.values()):
            if not task.done():
                task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        self.taskInfo.clear()
        await closeClients()

    # ========== 用户命令 ==========

    @filter.command("生图")
    async def cmdDraw(self, event: AstrMessageEvent):
        """用户发 /生图 直接触发生图，不交给 LLM。"""

        yield event.plain_result(await self._handleDraw(event))
        event.stop_event()

    @filter.command("取消生图")
    async def cmdCancel(self, event: AstrMessageEvent):
        """取消发送者最近一个任务。"""

        yield event.plain_result(self._handleCancel(event))
        event.stop_event()

    @filter.command("生图积分")
    async def cmdPoints(self, event: AstrMessageEvent):
        """查看个人积分。"""

        yield event.plain_result(self.data.formatPoints(self._pointKey(event)))
        event.stop_event()

    @filter.command("生图预设")
    async def cmdPreset(self, event: AstrMessageEvent):
        """查看/添加/删除预设。"""

        yield event.plain_result(self._handlePreset(self._body(event)))
        event.stop_event()

    @filter.command("生图模型")
    async def cmdModel(self, event: AstrMessageEvent):
        """查看或切换模型。"""

        yield event.plain_result(self.data.chooseModel(self._body(event)))
        event.stop_event()

    @filter.command("生图开关")
    async def cmdToggle(self, event: AstrMessageEvent):
        """切换总开关。"""

        newState = not self.data.enabled
        self.data.setEnabled(newState)
        if not newState:
            for tid, task in list(self.tasks.items()):
                if not task.done():
                    task.cancel()
        yield event.plain_result(f"生图功能已{'开启' if newState else '关闭'}。")
        event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("生图赠分")
    async def cmdGivePoints(self, event: AstrMessageEvent):
        """管理员给指定用户增加生图积分。用法：/生图赠分 @用户 数量"""

        yield event.plain_result(self._handleGivePoints(event))
        event.stop_event()

    # ========== LLM 工具 ==========

    @filter.llm_tool(name="super_draw")
    async def toolDraw(self, event: AstrMessageEvent, prompt: str = "", urls: str = "") -> str:
        """
        生成图片工具。当用户想画图、修图、P图、改图、生成头像、生成海报、做表情包、画壁纸、画插画时调用此工具。
        必须提供 prompt 参数描述想要的图片内容。如果用户提供了参考图片 URL，请通过 urls 传入。
        调用后会启动异步生图任务并返回任务 ID，图片生成完成后会自动发送到聊天中。

        Args:
            prompt(string): 必填。用户想要的图片内容描述，用自然语言写清楚画面内容、风格、比例、用途等。例如"一只橘猫坐在窗边看雨，水彩风格，竖版手机壁纸"
            urls(string): 可选。参考图片的 URL 地址，如果有多张参考图用英文逗号分隔。聊天中的图片会自动收集，不需要手动填写
        """

        if not prompt.strip():
            return "请提供生图描述，例如：prompt='一只猫坐在窗边看雨'"
        if not self.data.enabled:
            return "生图功能当前关闭。"
        if not self.data.enableTool:
            return "LLM 生图工具当前关闭。"

        realEvent = self._realEvent(event)
        senderId = self._senderId(realEvent)
        if self.data.isBanned(senderId):
            return "该用户已被加入生图黑名单。"

        pointKey = self._pointKey(realEvent)
        if reason := self.data.checkPoints(pointKey):
            return reason
        if self._queueFull():
            return f"生图队列已满（{self._activeCount()}/{self.data.maxQueueSize}），请稍后。"

        images = await self._collectToolImages(realEvent, urls)
        request = {
            "userId": realEvent.unified_msg_origin,
            "pointKey": pointKey,
            "prompt": prompt.strip(),
            "images": images,
            "fromTool": True,
            "event": realEvent,
        }

        request["pointsCost"] = self.data.spendPoints(pointKey)
        taskId = self._newTaskId(request["userId"], prompt)
        self._startTask(taskId, self._runTask(taskId, request), request)
        return f"生图任务已开始（{taskId}），请稍候。"

    @filter.llm_tool(name="super_draw_data")
    async def toolData(self, event: AstrMessageEvent, action: str = "", user_key: str = "", delta: int = 0, reason: str = "") -> str:
        """
        查询或修改生图插件的数据和用户积分。可以查看插件状态、查询用户积分、修改积分、查看排行榜。
        action 参数决定执行哪种操作：
        - summary：查看插件整体状态（模型、用户数等）
        - my_points：查看当前对话用户自己的积分
        - user_points：查看指定用户的积分（需要 user_key）
        - change_points：给用户加分或扣分（需要 delta，正数加分负数扣分）
        - set_points：把用户积分直接设置为指定值（需要 delta 为目标值）
        - rank：查看积分排行榜前10名

        Args:
            action(string): 必填。操作名，可选值：summary、my_points、user_points、change_points、set_points、rank
            user_key(string): 目标用户的标识键。留空表示当前对话的用户。change_points 和 set_points 时可指定要操作的用户
            delta(number): change_points 时为增减值（正数加分如+10，负数扣分如-5）；set_points 时为要设置的目标积分值
            reason(string): 修改积分的原因说明，会记录在返回结果中便于审计
        """

        if not action.strip():
            return "请提供 action 参数。可用值：summary、my_points、user_points、change_points、set_points、rank"
        if not self.data.enableDataTools:
            return "数据工具当前关闭。"

        realEvent = self._realEvent(event)
        targetKey = user_key.strip() or self._pointKey(realEvent)
        act = action.strip().lower()

        if act == "summary":
            return self.data.formatSummary()
        if act == "my_points":
            return self.data.formatPoints(self._pointKey(realEvent))
        if act == "user_points":
            return self.data.formatPoints(targetKey)
        if act == "change_points":
            return self.data.changePoints(targetKey, int(delta), reason)
        if act == "set_points":
            return self.data.setPoints(targetKey, int(delta), reason)
        if act == "rank":
            return self._formatRank()
        return "可用 action：summary、my_points、user_points、change_points、set_points、rank"

    @filter.llm_tool(name="super_draw_ban")
    async def toolBan(self, event: AstrMessageEvent, action: str = "", user_id: str = "") -> str:
        """
        管理生图功能的黑名单。黑名单中的用户无法使用生图功能。
        - list：查看当前黑名单中所有用户
        - add：把指定用户加入黑名单（需要 user_id）
        - remove：把指定用户从黑名单移除（需要 user_id）

        Args:
            action(string): 必填。操作名，可选值：list、add、remove
            user_id(string): 要加入或移除黑名单的用户 ID。list 操作时不需要填写
        """

        if not action.strip():
            return "请提供 action 参数。可用值：list、add、remove"
        act = action.strip().lower()
        if act == "list":
            return self.data.formatBanList()
        if act == "add":
            return self.data.addBan(user_id)
        if act == "remove":
            return self.data.removeBan(user_id)
        return "可用 action：list、add、remove"

    # ========== 发言积分 ==========

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def onGroupMessage(self, event: AstrMessageEvent):
        """群消息加积分，命令消息不加。"""

        text = (event.message_str or "").strip()
        if text.startswith("/"):
            return

        gained = self.data.addTalkPoint(self._pointKey(event), self._displayName(event))
        if gained and self.data.debugMode:
            logger.info(f"[SuperDraw] 积分 +{gained}: {self._pointKey(event)}")

    # ========== 生图任务 ==========

    async def _handleDraw(self, event: AstrMessageEvent) -> str:
        """处理 /生图 命令。"""

        if not self.data.enabled:
            return "生图功能当前关闭。"

        senderId = self._senderId(event)
        if self.data.isBanned(senderId):
            return "你已被加入生图黑名单。"

        body = self._body(event)
        prompt, presetName = self.data.resolvePreset(body)
        if not prompt:
            return "请在 /生图 后面写你想画什么。\n例如：/生图 一只猫坐在窗边看雨"

        pointKey = self._pointKey(event)
        if reason := self.data.checkPoints(pointKey):
            return reason
        if self._queueFull():
            return f"生图队列已满（{self._activeCount()}/{self.data.maxQueueSize}），请稍后。"

        images = await self._collectImages(event)
        request = {
            "userId": event.unified_msg_origin,
            "pointKey": pointKey,
            "prompt": prompt,
            "images": images,
            "fromTool": False,
        }

        request["pointsCost"] = self.data.spendPoints(pointKey)
        taskId = self._newTaskId(request["userId"], prompt)
        self._startTask(taskId, self._runTask(taskId, request), request)

        parts = [f"生图任务已开始：{taskId}", f"模型：{self.data.currentModelKey}"]
        if presetName:
            parts.append(f"预设：{presetName}")
        if images:
            parts.append(f"参考图：{len(images)}张")
        return "\n".join(parts)

    async def _runTask(self, taskId: str, request: dict[str, Any]) -> None:
        """后台执行生图。"""

        try:
            logger.info(f"[SuperDraw] 开始 {taskId} | {self.data.currentModelKey} | {request['prompt'][:60]}")
            imageBytesList = await makeImages(
                self.data.providers,
                self.data.currentProviderIndex,
                request["prompt"],
                request["images"],
                "auto",
                "auto",
                1,
                self.data.getNextKey,
            )
            imagePaths = await self._sendImages(request["userId"], taskId, imageBytesList)

            # 工具生图后评价
            if request.get("fromTool") and self.data.enableCommentary:
                await self._sendCommentary(taskId, request, imagePaths)

        except asyncio.CancelledError:
            self.data.refundPoints(request["pointKey"], int(request.get("pointsCost", 0)))
            await self.context.send_message(request["userId"], MessageChain().message(f"任务 {taskId} 已取消，积分已退回。"))

        except Exception as error:
            pointsCost = int(request.get("pointsCost", 0))
            logger.error(f"[SuperDraw] 任务 {taskId} 失败: {error}")

            if self._is400(error):
                penalty = self.data.settleBadRequest(request["pointKey"], pointsCost)
                await self.context.send_message(request["userId"], MessageChain().message(f"生图失败（{taskId}）：内容安全限制，扣除 {penalty} 分。\n{self._safeError(error)}"))
            else:
                self.data.refundPoints(request["pointKey"], pointsCost)
                await self.context.send_message(request["userId"], MessageChain().message(f"生图失败（{taskId}），积分已退回：{self._safeError(error)}"))
        finally:
            self.taskInfo.pop(taskId, None)

    async def _sendImages(self, targetId: str, taskId: str, imageBytesList: list[bytes]) -> list[str]:
        """发送图片，返回本地路径列表。"""

        paths: list[str] = []
        chain = MessageChain().message(f"生图完成：{taskId}\n模型：{self.data.currentModelKey}")
        for data in imageBytesList:
            path = saveImage(self.cacheDir, data, "png")
            if path:
                paths.append(path)
                chain.file_image(path)
        await self.context.send_message(targetId, chain)
        return paths

    async def _sendCommentary(self, taskId: str, request: dict[str, Any], imagePaths: list[str]) -> None:
        """LLM 工具生图后追加自然评价。"""

        event = request.get("event")
        if not event:
            return

        try:
            contextText = await self._chatContext(event)
            imageText = f"已发送 {len(imagePaths)} 张图片。" if imagePaths else "图片已生成。"
            prompt = self.data.buildCommentaryPrompt(request["prompt"], self.data.currentModelKey, contextText, imageText)

            providerId = self.data.commentaryProviderId or await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
            if not providerId:
                return

            resp = await self.context.llm_generate(chat_provider_id=providerId, prompt=prompt)
            text = str(getattr(resp, "completion_text", "") or "").strip()[: self.data.commentaryMaxLength]
            if text:
                await self.context.send_message(request["userId"], MessageChain().message(text))
        except Exception as e:
            logger.warning(f"[SuperDraw] 评价失败: {e}")

    async def _chatContext(self, event: AstrMessageEvent) -> str:
        """读取会话历史。"""

        try:
            mgr = self.context.conversation_manager
            cid = await mgr.get_curr_conversation_id(event.unified_msg_origin)
            if cid:
                conv = await mgr.get_conversation(event.unified_msg_origin, cid)
                history = str(getattr(conv, "history", "") or "").strip()
                if history:
                    return history[-2000:]
        except Exception:
            pass
        return (event.message_str or "").strip()[-500:]

    # ========== 任务管理 ==========

    def _startTask(self, taskId: str, coro: Any, request: dict[str, Any]) -> None:
        """启动后台任务。"""

        for oldId, oldTask in list(self.tasks.items()):
            if oldTask.done():
                self.tasks.pop(oldId, None)
                self.taskInfo.pop(oldId, None)

        self.taskInfo[taskId] = {"userId": request["userId"], "pointKey": request["pointKey"], "prompt": request["prompt"][:40], "start": time.time()}
        self.tasks[taskId] = asyncio.create_task(coro)

    def _handleCancel(self, event: AstrMessageEvent) -> str:
        """取消用户最近的任务。"""

        pointKey = self._pointKey(event)
        for taskId, task in reversed(list(self.tasks.items())):
            info = self.taskInfo.get(taskId, {})
            if info.get("pointKey") == pointKey and not task.done():
                task.cancel()
                self.taskInfo.pop(taskId, None)
                return "已取消你最近的生图任务，积分会自动退回。"
        return "你当前没有正在运行的任务。"

    def _handleGivePoints(self, event: AstrMessageEvent) -> str:
        """
        管理员给指定用户增加积分。
        用法：/生图赠分 @用户 数量
        通过 @ 组件识别目标用户，通过文本末尾数字识别增量。
        """

        msgObj = getattr(event, "message_obj", None)
        message = getattr(msgObj, "message", []) if msgObj else []

        # 从消息链里找第一个 @（排除机器人自身）
        targetQQ = ""
        for comp in message:
            if isinstance(comp, Comp.At):
                qq = str(getattr(comp, "qq", "") or "")
                selfId = str(getattr(msgObj, "self_id", "") or "") if msgObj else ""
                if qq and qq != selfId and qq != "all":
                    targetQQ = qq
                    break

        if not targetQQ:
            return "用法：/生图赠分 @用户 数量\n请 @ 一个用户。"

        # 从文本里找数字参数
        body = self._body(event)
        amountText = "".join(ch for ch in body if ch.isdigit() or ch == "-")
        if not amountText:
            return "用法：/生图赠分 @用户 数量\n请在命令后加上要赠送的积分数量，例如：/生图赠分 @用户 50"

        try:
            amount = int(amountText)
        except ValueError:
            return "积分数量格式错误，请填写整数，例如：50"

        if amount == 0:
            return "积分数量不能为 0。"

        # 构建目标用户的积分 key（用户单独记录，不依赖群号）
        targetKey = targetQQ
        result = self.data.changePoints(targetKey, amount, "管理员赠分")
        action = "赠送" if amount > 0 else "扣除"
        return f"已向 @{targetQQ} {action} {abs(amount)} 分，{result}"

    def _handlePreset(self, body: str) -> str:
        """处理预设命令。"""

        if not body:
            return self.data.formatPresetList()
        if body.startswith("添加 "):
            return self.data.addPreset(body[3:].strip())
        if body.startswith("删除 "):
            return self.data.removePreset(body[3:].strip())
        if body.startswith("查看 "):
            name = body[3:].strip()
            prompt = self.data.presets.get(name)
            return f"{name}：{prompt}" if prompt else f"预设不存在：{name}"
        return "格式：/生图预设、/生图预设 添加 名称:内容、/生图预设 删除 名称、/生图预设 查看 名称"

    def _formatRank(self, limit: int = 10) -> str:
        """积分排行。"""

        if not self.data.pointsByUser:
            return "暂无积分记录。"
        rows = sorted(self.data.pointsByUser.items(), key=lambda x: int(x[1].get("points", 0)), reverse=True)[:limit]
        lines = ["积分排行："]
        for i, (_, user) in enumerate(rows, 1):
            lines.append(f"  {i}. {user.get('name', '群友')}：{user.get('points', 0)} 分")
        return "\n".join(lines)

    # ========== 图片收集 ==========

    async def _collectImages(self, event: AstrMessageEvent) -> list[bytes]:
        """从用户消息中收集参考图。"""

        msgObj = getattr(event, "message_obj", None)
        if not msgObj or not getattr(msgObj, "message", None):
            return []

        images: list[bytes] = []
        for i, comp in enumerate(msgObj.message):
            if i == 0 and isinstance(comp, Comp.At):
                continue
            images.extend(await self._extractImage(comp, event))

        for url in re.findall(r"https?://[^\s]+", event.message_str or ""):
            if data := await self._download(url.rstrip("，。,.）)")):
                images.append(data)
        return images[:8]

    async def _collectToolImages(self, event: AstrMessageEvent, urls: str) -> list[bytes]:
        """从 LLM 工具参数和消息中收集参考图。"""

        images: list[bytes] = []
        for url in [u.strip() for u in urls.split(",") if u.strip()]:
            if data := await self._download(url):
                images.append(data)
        images.extend(await self._collectImages(event))
        return images[:8]

    async def _extractImage(self, comp: Any, event: AstrMessageEvent) -> list[bytes]:
        """从消息组件提取图片。"""

        if isinstance(comp, Comp.Image):
            return [d] if (d := await self._download(comp.url or comp.file)) else []
        if isinstance(comp, Comp.Reply) and comp.chain:
            result: list[bytes] = []
            for item in comp.chain:
                result.extend(await self._extractImage(item, event))
            return result
        if isinstance(comp, Comp.At) and str(getattr(comp, "qq", "")) not in ("", "all"):
            url = f"https://q4.qlogo.cn/headimg_dl?dst_uin={comp.qq}&spec=640"
            return [d] if (d := await self._download(url)) else []
        return []

    async def _download(self, source: str | None) -> bytes | None:
        """下载图片。"""

        if not source:
            return None
        try:
            if not str(source).startswith(("http://", "https://")):
                p = Path(source)
                return p.read_bytes() if p.is_file() else None
            name = self.cacheDir / f"ref_{hashlib.md5(source.encode()).hexdigest()[:12]}"
            downloaded = await download_image_by_url(source, path=str(name))
            return Path(downloaded).read_bytes() if downloaded else None
        except Exception:
            return None

    # ========== 工具方法 ==========

    def _realEvent(self, event: Any) -> AstrMessageEvent:
        """兼容 AstrBot v4.26 ContextWrapper：提取真实事件对象。"""

        if isinstance(event, AstrMessageEvent):
            return event
        for attr in ["event", "context"]:
            inner = getattr(event, attr, None)
            if isinstance(inner, AstrMessageEvent):
                return inner
            deeper = getattr(inner, "event", None)
            if isinstance(deeper, AstrMessageEvent):
                return deeper
        return event

    def _body(self, event: AstrMessageEvent) -> str:
        """取命令参数部分。"""

        text = (event.message_str or "").strip()
        return text.split(maxsplit=1)[1].strip() if " " in text else ""

    def _pointKey(self, event: AstrMessageEvent) -> str:
        """积分用户键：只用 QQ 号，不区分群，同一个人在所有群共享积分。"""

        sid = self._senderId(event)
        return sid if sid else event.unified_msg_origin

    def _senderId(self, event: AstrMessageEvent) -> str:
        """读发送者 ID。"""

        if callable(getattr(event, "get_sender_id", None)):
            return str(event.get_sender_id() or "")
        msgObj = getattr(event, "message_obj", None)
        raw = getattr(msgObj, "raw_message", None) if msgObj else None
        if isinstance(raw, dict):
            return str(raw.get("user_id") or raw.get("sender", {}).get("user_id") or "")
        return str(getattr(raw, "user_id", "") or "")

    def _displayName(self, event: AstrMessageEvent) -> str:
        """读发送者昵称。"""

        if callable(getattr(event, "get_sender_name", None)):
            return str(event.get_sender_name() or "")
        return self._senderId(event) or "群友"

    def _newTaskId(self, userId: str, prompt: str) -> str:
        return hashlib.md5(f"{time.time()}|{userId}|{prompt[:80]}".encode()).hexdigest()[:8]

    def _activeCount(self) -> int:
        return sum(1 for t in self.tasks.values() if not t.done())

    def _queueFull(self) -> bool:
        return self._activeCount() >= self.data.maxQueueSize

    def _is400(self, error: Exception) -> bool:
        text = str(error).lower()
        return "400" in text or "bad request" in text or "content_policy" in text

    def _safeError(self, error: Exception) -> str:
        text = str(error)[:200]
        return re.sub(r"(sk-|key-|AIza)[A-Za-z0-9_-]{8,}", "***", text)
