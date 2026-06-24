"""
AstrBot 超级生图插件 3.0.0 入口。

这个文件是“聊天事件 → 生图指令 → 数据记录 → 图片反馈”的主线入口。
它只做和 AstrBot 有关的事情：接收命令、收集消息里的图片、启动后台任务、把结果发回聊天。
真正的数据在 data.py，真正调用模型在 generate.py，文件保存和图片预处理在 tool 文件夹。

用户命令只保留一个核心入口：生图。
用户是否发了图片、回复了图片、贴了图片链接，由插件自动判断；有图就是图生图，没图就是文生图。
用户想要横图、竖图、高清、几张图，都直接写进自然语言提示词，不再学习任何参数格式。

推荐命令：
    /生图 一只猫坐在窗边看雨，画成手机壁纸
    /生图 参考这张图，做成水彩头像
    /取消生图
    /生图积分
    /生图预设
    /生图模型 2
    /生图开关

LLM 工具：
    super_draw(prompt="画一张 16:9 海报，高清，两张候选图", urls="https://...")
"""

from __future__ import annotations

import asyncio  # 后台任务、并发锁、取消任务都依赖 asyncio
import hashlib  # 用时间、用户、提示词生成短任务 ID
import re  # 从文本中识别图片 URL，并对错误里的 API Key 做脱敏
import time  # 记录任务开始时间和耗时
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp  # AstrBot 消息组件，图片、回复、转发、@ 都从这里判断
from astrbot.api import logger  # AstrBot 标准日志
from astrbot.api.event import AstrMessageEvent, MessageChain, filter  # 命令入口、LLM 工具入口、消息链
from astrbot.api.star import Context, Star  # AstrBot 插件基类
from astrbot.core.config.astrbot_config import AstrBotConfig  # 插件配置对象
from astrbot.core.star.star_tools import StarTools  # 获取插件数据目录
from astrbot.core.utils.io import download_image_by_url  # 下载网络参考图

from .data import Data  # 插件数据中心：配置、模型、预设、限制、用量
from .generate import closeClients, makeImages  # 生图指令：调用 OpenAI/Gemini 并返回图片 bytes
from .tool.file import saveImage  # 文件工具：AstrBot 发本地图片时仍需要一个临时文件路径


class SuperDraw(Star):
    """
    超级生图插件主类。

    AstrBot 要求插件继承 Star，所以入口必须是类；但类里的每个方法仍按 HOP 主线写成清晰动作。
    命令方法只负责“触发”，_startDraw() 负责“指令编排”，Data 负责“数据”，_sendImages() 负责“反馈”。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.dataDir = StarTools.get_data_dir()  # AstrBot 给插件的数据目录，缓存和 usage.json 都放这里
        self.cacheDir = self.dataDir / "cache"  # 图片缓存目录，生成后的图片先保存再发送
        self.data = Data(config, self.dataDir)  # 所有配置和用户数据集中在 Data 里
        self.semaphore = asyncio.Semaphore(self.data.maxConcurrent)  # 控制真正同时跑的生图任务数量
        self.tasks: dict[str, asyncio.Task[Any]] = {}  # 任务ID -> asyncio.Task，用于查看队列和取消任务
        self.taskInfo: dict[str, dict[str, Any]] = {}  # 任务ID -> 可读元信息，用于 /生图队列 展示

    # ========== 生命周期 ==========

    async def initialize(self) -> None:
        """插件启动时检查配置并创建临时图片目录；不做缓存保留和定时清理。"""

        self.cacheDir.mkdir(parents=True, exist_ok=True)

        if not self.data.providers:
            logger.error("[SuperDraw] 未配置可用模型，请在 api_providers 里填写 api_keys 和 available_models。")
            return

        logger.info(f"[SuperDraw] 3.0.0 启动完成，当前模型：{self.data.currentModelKey}")

    async def terminate(self) -> None:
        """插件关闭时取消所有后台任务并关闭 HTTP 客户端，防止残留连接。"""

        for task in list(self.tasks.values()):
            if not task.done():
                task.cancel()

        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()
        self.taskInfo.clear()
        await closeClients()

    # ========== 用户命令：生图 ==========

    @filter.command("生图")
    @filter.command("画图")
    async def cmdDraw(self, event: AstrMessageEvent):
        """用户发送 /生图 时触发；走 AstrBot 标准命令系统直接生图，不交给 LLM 工具判断。"""

        yield event.plain_result(await self._acceptDraw(event))
        self._stopEvent(event)

    @filter.command("生图帮助")
    @filter.command("画图帮助")
    async def cmdHelp(self, event: AstrMessageEvent):
        """用户发送 /生图帮助 时触发；返回最短可用说明，让群友不用翻 README。"""

        yield event.plain_result(self._helpText())
        self._stopEvent(event)

    @filter.command("生成")
    async def cmdGenerateAlias(self, event: AstrMessageEvent):
        """默认额外支持 /生成；更多自定义词走 WebUI commands.draw 配置和群消息路由。"""

        async for result in self.cmdDraw(event):
            yield result

    @filter.command("生图队列")
    @filter.command("画图队列")
    async def cmdQueue(self, event: AstrMessageEvent):
        """用户发送 /生图队列 时触发；展示最近几个未完成任务，方便用户取消或等待。"""

        yield event.plain_result(self._formatQueue())
        self._stopEvent(event)

    @filter.command("取消生图")
    @filter.command("取消画图")
    @filter.command("生图取消")
    async def cmdCancel(self, event: AstrMessageEvent):
        """用户发送 /取消生图 时触发；自动取消这个用户最近一个未完成任务，不要求复制任务 ID。"""

        yield event.plain_result(self._cancelLatestTask(event))
        self._stopEvent(event)

    @filter.command("生图模型")
    @filter.command("画图模型")
    async def cmdModel(self, event: AstrMessageEvent):
        """用户发送 /生图模型 时触发；无参数展示模型列表，有参数切换模型。"""

        yield event.plain_result(self.data.chooseModel(self._commandBody(event)))
        self._stopEvent(event)

    @filter.command("生图开关")
    async def cmdToggle(self, event: AstrMessageEvent):
        """用户发送 /生图开关 时触发；切换总开关，关闭时顺手取消所有生图任务。"""

        yield event.plain_result(self._toggleDraw())
        self._stopEvent(event)

    @filter.command("生图积分")
    @filter.command("积分")
    @filter.command("分")
    async def cmdPoints(self, event: AstrMessageEvent):
        """用户发送 /生图积分 时触发；查看自己的积分、发言次数和生图消耗。"""

        yield event.plain_result(self.data.formatPoints(self._pointKey(event)))
        self._stopEvent(event)

    @filter.command("积分排行")
    @filter.command("积分榜")
    @filter.command("榜")
    async def cmdPointRank(self, event: AstrMessageEvent):
        """用户发送 /榜 时触发；展示积分余额排行榜，鼓励群友正常聊天赚积分。"""

        yield event.plain_result(self.data.formatPointRank(10))
        self._stopEvent(event)

    @filter.command("生图预设")
    @filter.command("画图预设")
    @filter.command("预设")
    async def cmdPreset(self, event: AstrMessageEvent):
        """用户发送 /预设 时触发；查看、添加、删除提示词预设。"""

        yield event.plain_result(self._runPresetCommand(self._commandBody(event)))
        self._stopEvent(event)

    @filter.command("提示词优化")
    @filter.command("优化提示词")
    async def cmdOptimizePrompt(self, event: AstrMessageEvent):
        """用户发送 /提示词优化 时触发；调用 WebUI 选定的 AstrBot 模型改写提示词。"""

        yield event.plain_result(await self._optimizePromptByModel(event, self._commandBody(event)))
        self._stopEvent(event)

    # ========== LLM 工具 ==========

    @filter.llm_tool(name="super_draw")
    async def llmDraw(
        self,
        event: AstrMessageEvent,
        prompt: str,
        urls: str = "",
    ) -> str:
        """
        当用户想画图、修图、P图、改图、生成头像、生成海报、复刻表情包时调用。

        Args:
            prompt(string): 用户想要的图片内容或修改要求，直接用自然语言写清楚比例、风格、数量、用途
            urls(string): 参考图 URL，多个 URL 用英文逗号分隔；聊天里带图时也会自动收集
        """

        if not self.data.enabled:
            return "生图功能当前关闭。"

        if not self.data.enableTool:
            return "LLM 生图工具当前关闭，请改用 /生图 命令。"

        request = await self._buildRequestFromTool(event, prompt, urls)
        if not request["prompt"]:
            return "请提供明确的生图描述。"

        if reason := self.data.checkDrawPoints(request["pointKey"], request["count"], request["isPrivate"]):
            return reason

        if fullReason := self._queueFullReason():
            return fullReason

        request["event"] = event  # 工具生图才保存事件，后续评价需要读取当前会话上下文
        request["fromTool"] = True  # 只有 LLM 工具生图完成后才触发 Bot 自然评价，普通命令不触发
        request["pointsCost"] = self.data.spendDrawPoints(request["pointKey"], request["count"], request["isPrivate"])
        taskId = self._newTaskId(request["userId"], request["prompt"])
        self._startTask(taskId, self._runDrawTask(taskId, request), self._taskInfoFromRequest(request))
        return self._acceptedText(taskId, request)

    @filter.llm_tool(name="super_draw_data")
    async def llmDataTool(
        self,
        event: AstrMessageEvent,
        action: str,
        user_key: str = "",
        delta: int = 0,
        reason: str = "",
    ) -> str:
        """
        当用户或管理员想让 Bot 查询/修改生图插件数据时调用。

        Args:
            action(string): 操作名，可选 summary、status、my_points、user_points、change_points、rank、presets
            user_key(string): 目标积分用户键；为空时默认操作当前会话发送者
            delta(number): change_points 时要增加或扣除的积分，正数加分，负数扣分
            reason(string): 修改积分的原因，会写进返回文本方便审计
        """

        if not self.data.enableDataTools:
            return "生图数据工具当前关闭。"

        targetKey = user_key.strip() or self._pointKey(event)
        cleanAction = action.strip().lower()
        if cleanAction == "summary":
            return self.data.formatAllDataSummary()
        if cleanAction == "status":
            return self._formatStatus()
        if cleanAction == "my_points":
            return self.data.formatPoints(self._pointKey(event))
        if cleanAction == "user_points":
            return str(self.data.getUserData(targetKey))
        if cleanAction == "change_points":
            return self.data.changePoints(targetKey, int(delta), reason)
        if cleanAction == "rank":
            return self.data.formatPointRank(10)
        if cleanAction == "presets":
            return self.data.formatPresetList()
        return "未知操作。可用 action：summary、status、my_points、user_points、change_points、rank、presets。"

    @filter.llm_tool(name="super_draw_optimize_prompt")
    async def llmOptimizePrompt(self, event: AstrMessageEvent, prompt: str) -> str:
        """
        当用户想先优化生图提示词、但还没要求立刻画图时调用。

        Args:
            prompt(string): 用户原始想法或短提示词
        """

        return await self._optimizePromptByModel(event, prompt)

    # ========== 生图指令编排 ==========

    async def _runDrawTask(self, taskId: str, request: dict[str, Any]) -> None:
        """后台执行一次完整生图：等并发名额 → 调接口 → 记录用量 → 保存图片 → 发回聊天。"""

        async with self.semaphore:
            try:
                logger.info(f"[SuperDraw] 开始任务 {taskId} | {self.data.currentModelKey} | {request['prompt'][:60]}")
                imageBytesList = await makeImages(self.data.providers, self.data.currentProviderIndex, request["prompt"], request["images"], request["size"], request["quality"], request["count"], self.data.getNextKey)
                self.data.recordUsage(request["userId"], len(imageBytesList))
                imagePaths = await self._sendImages(request["userId"], taskId, imageBytesList, request)
                await self._sendToolCommentary(taskId, request, imagePaths)
            except asyncio.CancelledError:
                self.data.refundPoints(request["pointKey"], int(request.get("pointsCost", 0)))
                await self.context.send_message(request["userId"], MessageChain().message(f"生图任务 {taskId} 已取消，积分已退回。"))
            except Exception as error:
                pointsCost = int(request.get("pointsCost", 0))  # 生图排队前已经预扣的积分，失败时要按错误类型重新结算
                logger.error(f"[SuperDraw] 任务 {taskId} 失败: {error}")
                if self._isBadRequestError(error):
                    penalty = self.data.settleBadRequestPoints(request["pointKey"], pointsCost)
                    await self.context.send_message(request["userId"], MessageChain().message(f"生图失败（任务 {taskId}）：接口返回 400，通常是提示词或内容安全限制。本次按规则扣除 {penalty} 分，积分最低不会低于 0。\n{self._safeError(error)}"))
                    return

                self.data.refundPoints(request["pointKey"], pointsCost)
                await self.context.send_message(request["userId"], MessageChain().message(f"生图失败（任务 {taskId}），积分已退回：{self._safeError(error)}"))
            finally:
                self.taskInfo.pop(taskId, None)

    async def _sendImages(self, targetId: str, taskId: str, imageBytesList: list[bytes], request: dict[str, Any]) -> list[str]:
        """把模型返回的图片发回聊天；返回实际发送路径，方便 LLM 工具生图后继续评价。"""

        imagePaths: list[str] = []
        chain = MessageChain().message(f"生图完成：{taskId}\n模型：{self.data.currentModelKey}")
        for imageBytes in imageBytesList:
            path = saveImage(self.cacheDir, imageBytes, self.data.saveFormat)
            if path:
                imagePaths.append(path)
                chain.file_image(path)

        await self.context.send_message(targetId, chain)
        return imagePaths

    async def _optimizePromptByModel(self, event: AstrMessageEvent, text: str) -> str:
        """用 WebUI 选定的 AstrBot 聊天模型优化提示词；失败时退回模板文本，用户仍有结果可用。"""

        if not self.data.enablePromptOptimize:
            return "提示词优化功能当前关闭。"

        cleanText = str(text or "").strip()
        if not cleanText:
            return f"请在提示词优化命令后面写你想画什么，例如 {self.data.formatCommand(self.data.optimizeCommands[0])} 猫咪头像。"

        prompt = self.data.buildOptimizePrompt(cleanText)
        try:
            return await self._callAstrBotModel(event, self.data.promptOptimizeProviderId, prompt, "提示词优化")
        except Exception as error:
            logger.warning(f"[SuperDraw] 提示词优化模型调用失败，改用模板兜底: {error}")
            return prompt

    async def _sendToolCommentary(self, taskId: str, request: dict[str, Any], imagePaths: list[str]) -> None:
        """LLM 工具生图完成后，让 Bot 结合会话上下文自然追评；普通 /生图 命令不会走这里。"""

        if not request.get("fromTool") or not self.data.enableToolCommentary:
            return

        event = request.get("event")
        if not isinstance(event, AstrMessageEvent):
            return

        try:
            contextText = await self._conversationContext(event)
            imageText = self._imageInfo(imagePaths)
            prompt = self.data.buildToolCommentaryPrompt(request, contextText, imageText)
            comment = await self._callAstrBotModel(event, self.data.toolCommentaryProviderId, prompt, "生图后评价")
            comment = comment.strip()[: self.data.toolCommentaryMaxLength]
            if comment:
                await self.context.send_message(request["userId"], MessageChain().message(comment))
        except Exception as error:
            logger.warning(f"[SuperDraw] 生图后评价失败，已跳过不影响发图: {error}")

    async def _callAstrBotModel(self, event: AstrMessageEvent, providerId: str, prompt: str, actionName: str) -> str:
        """统一调用 AstrBot 已配置的聊天模型；providerId 留空时使用当前会话模型。"""

        chatProviderId = providerId.strip() or await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
        if not chatProviderId:
            raise RuntimeError(f"{actionName}没有可用的 AstrBot 聊天模型，请在会话或 WebUI 配置 provider_id。")

        llmResp = await self.context.llm_generate(chat_provider_id=chatProviderId, prompt=prompt)
        text = str(getattr(llmResp, "completion_text", "") or "").strip()
        if not text:
            raise RuntimeError(f"{actionName}模型返回为空。")
        return text

    async def _conversationContext(self, event: AstrMessageEvent) -> str:
        """读取 AstrBot 当前会话历史；读不到就退回当前消息文本，保证评价至少知道刚才聊了什么。"""

        try:
            convMgr = self.context.conversation_manager
            conversationId = await convMgr.get_curr_conversation_id(event.unified_msg_origin)
            conversation = await convMgr.get_conversation(event.unified_msg_origin, conversationId) if conversationId else None
            history = str(getattr(conversation, "history", "") or "").strip()
            if history:
                return history[-3000:]
        except Exception as error:
            if self.data.debugMode:
                logger.warning(f"[SuperDraw] 读取会话历史失败: {error}")

        return (event.message_str or "").strip()[-1000:]

    def _imageInfo(self, imagePaths: list[str]) -> str:
        """把已发送图片整理成给评价模型看的信息；当前 AstrBot 文档未暴露稳定图片输入时先传路径和数量。"""

        if not imagePaths:
            return "图片已生成并发送，但本地发送路径为空。"

        lines = [f"本次已发送 {len(imagePaths)} 张图片。"]
        lines.extend(f"图片{index}本地路径：{path}" for index, path in enumerate(imagePaths, 1))
        lines.append("如果当前聊天模型支持读取最近消息图片，请结合刚刚发出的图片本身来评价；否则请根据用户需求和图片数量自然接话。")
        return "\n".join(lines)

    def _startTask(self, taskId: str, coro: Any, info: dict[str, Any]) -> None:
        """启动一个后台任务，并顺手清掉已经完成的旧任务，避免任务表无限增长。"""

        for oldId, oldTask in list(self.tasks.items()):
            if oldTask.done():
                self.tasks.pop(oldId, None)
                self.taskInfo.pop(oldId, None)

        self.taskInfo[taskId] = {**info, "start": time.time()}
        self.tasks[taskId] = asyncio.create_task(coro)

    # ========== 发言积分 ==========

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def onGroupMessage(self, event: AstrMessageEvent):
        """
        群消息统一入口：先看是不是 WebUI 自定义命令；不是命令才按普通发言加积分。

        AstrBot 的 @filter.command 适合固定默认命令；用户要求“WebUI 能改全部指令”时，就需要在普通消息钩子里自己路由。
        为避免默认命令被 @filter.command 和这里重复处理，这里只处理 WebUI 新增的别名，默认内置命令仍交给上面的命令函数。
        """

        command = self._matchConfiguredCommand(event)
        if command and not self._isBuiltInCommand(command["name"]):
            await self._sendText(event, await self._runConfiguredCommand(event, command["kind"], command["body"]))
            self._stopEvent(event)  # 自定义命令已经由插件直接处理，必须阻止后续 LLM 再把它当普通聊天理解成工具调用
            return

        if command or self._isCommandMessage(event):
            return

        gained = self.data.addTalkPoint(self._pointKey(event), self._displayName(event))
        if gained and self.data.debugMode:
            logger.info(f"[SuperDraw] 发言积分 +{gained}: {self._pointKey(event)}")

    # ========== 请求构建 ==========

    async def _buildRequestFromEvent(self, event: AstrMessageEvent, body: str = "") -> dict[str, Any]:
        """从用户命令里构建生图请求：命令文本负责提示词和参数，消息组件负责参考图。"""

        text = body if body else self._commandBody(event)
        prompt, presetName = self.data.resolvePreset(text)
        images = await self._collectImages(event)

        return {
            "userId": event.unified_msg_origin,
            "pointKey": self._pointKey(event),
            "isPrivate": self._isPrivate(event),
            "prompt": prompt,
            "preset": presetName,
            "images": images[: self.data.maxReferenceImages],
            "size": self.data.defaultSize,
            "quality": self.data.defaultQuality,
            "count": 1,
        }

    async def _buildRequestFromTool(self, event: AstrMessageEvent, prompt: str, urls: str) -> dict[str, Any]:
        """从 LLM 工具参数构建生图请求；比例、质量、数量都让 LLM 写进自然语言 prompt。"""

        images: list[bytes] = []
        for url in [item.strip() for item in urls.split(",") if item.strip()]:
            if imageBytes := await self._downloadImage(url):
                images.append(imageBytes)
        images.extend(await self._collectImages(event))

        return {
            "userId": event.unified_msg_origin,
            "pointKey": self._pointKey(event),
            "isPrivate": self._isPrivate(event),
            "prompt": self.data.buildPrompt(prompt.strip()),
            "preset": None,
            "images": images[: self.data.maxReferenceImages],
            "size": self.data.defaultSize,
            "quality": self.data.defaultQuality,
            "count": 1,
        }

    def _taskInfoFromRequest(self, request: dict[str, Any]) -> dict[str, Any]:
        """把完整请求压缩成适合 /生图队列 和 /取消生图 使用的小字典。"""

        return {"userId": request["userId"], "pointKey": request["pointKey"], "prompt": request["prompt"][:40], "count": request["count"], "size": request["size"], "quality": request["quality"]}

    # ========== 参考图收集 ==========

    async def _collectImages(self, event: AstrMessageEvent) -> list[bytes]:
        """从消息图片、回复、合并转发、@头像、文本 URL 中收集参考图。"""

        if not event.message_obj or not event.message_obj.message:
            return []

        images: list[bytes] = []
        for index, component in enumerate(event.message_obj.message):
            if index == 0 and isinstance(component, Comp.At):
                continue  # 群聊里开头 @机器人 是命令触发，不应把机器人头像当参考图
            images.extend(await self._extractImagesFromComponent(component, event))

        for url in self._imageUrls(event.message_str or ""):
            if imageBytes := await self._downloadImage(url):
                images.append(imageBytes)

        return images[: self.data.maxReferenceImages]

    async def _extractImagesFromComponent(self, component: Any, event: AstrMessageEvent | None = None) -> list[bytes]:
        """从单个消息组件里提取图片；遇到回复和转发会继续往里面找。"""

        if isinstance(component, Comp.Image):
            return [imageBytes] if (imageBytes := await self._downloadImage(component.url or component.file)) else []

        if isinstance(component, Comp.Forward):
            return await self._extractImagesFromForward(component, event)

        if isinstance(component, Comp.Nodes):
            result: list[bytes] = []
            for node in component.nodes:
                result.extend(await self._extractImagesFromComponent(node, event))
            return result

        if isinstance(component, Comp.Node):
            result: list[bytes] = []
            for item in component.content or []:
                result.extend(await self._extractImagesFromComponent(item, event))
            return result

        if isinstance(component, Comp.Reply) and component.chain:
            result: list[bytes] = []
            for item in component.chain:
                result.extend(await self._extractImagesFromComponent(item, event))
            return result

        if isinstance(component, Comp.At) and str(getattr(component, "qq", "")) not in ("", "all"):
            avatarUrl = f"https://q4.qlogo.cn/headimg_dl?dst_uin={component.qq}&spec=640"
            return [imageBytes] if (imageBytes := await self._downloadImage(avatarUrl)) else []

        return []

    async def _extractImagesFromForward(self, component: Comp.Forward, event: AstrMessageEvent | None) -> list[bytes]:
        """从合并转发消息中拉取原始节点，再提取里面的图片 URL 或 file。"""

        if event is None:
            return []

        bot = getattr(event, "bot", None)
        if not bot or not callable(getattr(bot, "call_action", None)):
            return []

        forwardId = component.id or self._findForwardId(event)
        if not forwardId:
            return []

        try:
            response = await bot.call_action("get_forward_msg", id=forwardId)
            nodes = response.get("messages") or response.get("data", {}).get("messages") or []
        except Exception as error:
            logger.warning(f"[SuperDraw] 拉取合并转发失败: {error}")
            return []

        images: list[bytes] = []
        for node in nodes:
            content = node.get("content") or node.get("message") or []
            if isinstance(content, str):
                content = self._segmentsFromText(content)
            for segment in content if isinstance(content, list) else []:
                url = self._imageUrlFromSegment(segment)
                if url and (imageBytes := await self._downloadImage(url)):
                    images.append(imageBytes)
        return images

    def _findForwardId(self, event: AstrMessageEvent) -> str:
        """从平台原始消息里寻找合并转发 ID；有些适配器不会直接填在 Comp.Forward.id 上。"""

        messageObj = getattr(event, "message_obj", None)
        raw = getattr(messageObj, "raw_message", None) if messageObj else None
        segments = getattr(raw, "message", None) if hasattr(raw, "message") else (raw.get("message", []) if isinstance(raw, dict) else [])

        for segment in segments or []:
            if segment.get("type") == "forward":
                return segment.get("data", {}).get("id") or segment.get("data", {}).get("resid") or ""
        return ""

    def _segmentsFromText(self, text: str) -> list[dict[str, Any]]:
        """部分平台把转发内容给成字符串，这里只从字符串里提取图片 URL，转成统一 segment 结构。"""

        return [{"type": "image", "data": {"url": url}} for url in self._imageUrls(text)]

    def _imageUrlFromSegment(self, segment: Any) -> str:
        """从 OneBot 风格 segment 中读取图片地址，读不到就返回空字符串。"""

        if not isinstance(segment, dict) or segment.get("type") != "image":
            return ""
        data = segment.get("data", {}) or {}
        return data.get("url") or data.get("file") or ""

    async def _downloadImage(self, source: str | None) -> bytes | None:
        """把网络 URL 或本地文件路径读取成 bytes；失败返回 None，让调用方跳过这张参考图。"""

        if not source:
            return None

        try:
            if not str(source).startswith(("http://", "https://")):
                path = Path(source)
                return path.read_bytes() if path.is_file() else None

            fileName = self.cacheDir / f"ref_{hashlib.md5(source.encode()).hexdigest()[:12]}"
            downloaded = await download_image_by_url(source, path=str(fileName))
            return Path(downloaded).read_bytes() if downloaded else None
        except Exception as error:
            if self.data.debugMode:
                logger.warning(f"[SuperDraw] 下载参考图失败 {source}: {error}")
            return None

    # ========== 自定义命令路由 ==========

    async def _runConfiguredCommand(self, event: AstrMessageEvent, kind: str, body: str) -> str:
        """执行 WebUI 配出来的命令；kind 是命令用途，body 是去掉命令词后的正文。"""

        if kind == "draw":
            return await self._acceptDraw(event, body)
        if kind == "help":
            return self._helpText()
        if kind == "status":
            return self._formatStatus()
        if kind == "queue":
            return self._formatQueue()
        if kind == "cancel":
            return self._cancelLatestTask(event)
        if kind == "model":
            return self.data.chooseModel(body)
        if kind == "toggle":
            return self._toggleDraw()
        if kind == "points":
            return self.data.formatPoints(self._pointKey(event))
        if kind == "rank":
            return self.data.formatPointRank(10)
        if kind == "preset":
            return self._runPresetCommand(body)
        if kind == "optimize":
            return await self._optimizePromptByModel(event, body)
        return "这个命令还没有处理逻辑。"

    async def _acceptDraw(self, event: AstrMessageEvent, body: str = "") -> str:
        """统一受理一次生图；默认命令和 WebUI 自定义命令都走这里，避免两套扣分和排队逻辑。"""

        if not self.data.enabled:
            return "生图功能当前关闭。管理员可发送开关命令重新开启。"

        request = await self._buildRequestFromEvent(event, body)
        if not request["prompt"]:
            return self._helpText()

        if reason := self.data.checkDrawPoints(request["pointKey"], request["count"], request["isPrivate"]):
            return reason

        if fullReason := self._queueFullReason():
            return fullReason

        request["fromTool"] = False  # 用户命令生图只发图，不追加 LLM 评价，避免群聊里显得啰嗦
        request["pointsCost"] = self.data.spendDrawPoints(request["pointKey"], request["count"], request["isPrivate"])
        taskId = self._newTaskId(request["userId"], request["prompt"])
        self._startTask(taskId, self._runDrawTask(taskId, request), self._taskInfoFromRequest(request))
        return self._acceptedText(taskId, request)

    def _formatStatus(self) -> str:
        """把插件开关、模型和队列状态整理成文本；默认命令、自定义命令、LLM 数据工具共用。"""

        running = sum(1 for task in self.tasks.values() if not task.done())
        waiting = max(0, running - self.data.maxConcurrent)
        return self.data.formatStatus(running, waiting)

    def _formatQueue(self) -> str:
        """把当前队列整理成文本；命令函数和自定义路由共用同一份展示。"""

        activeIds = [taskId for taskId, task in self.tasks.items() if not task.done() and taskId != "clean"]
        if not activeIds:
            return "当前没有运行中的生图任务。"

        lines = [f"运行中任务：{len(activeIds)}"]
        for taskId in activeIds[-8:]:
            info = self.taskInfo.get(taskId, {})
            seconds = int(time.time() - float(info.get("start", time.time())))
            lines.append(f"  {taskId} | {info.get('count', 1)}张 | {seconds}s | {info.get('prompt', '?')}...")
        return "\n".join(lines)

    def _cancelLatestTask(self, event: AstrMessageEvent) -> str:
        """取消发送者最近的任务；不需要任务 ID，手机端只打一条命令就够。"""

        taskId = self._latestTaskIdForUser(self._pointKey(event))
        if not taskId:
            return "你当前没有正在运行的生图任务。"

        task = self.tasks.get(taskId)
        if task and not task.done():
            task.cancel()
            self.taskInfo.pop(taskId, None)
            return "已取消你最近的生图任务，积分会自动退回。"
        return "你最近的生图任务已经结束。"

    def _toggleDraw(self) -> str:
        """切换生图总开关；关闭时顺手取消所有未完成任务。"""

        newState = not self.data.enabled
        self.data.setEnabled(newState)
        if not newState:
            for taskId, task in list(self.tasks.items()):
                if taskId != "clean" and not task.done():
                    task.cancel()
        return f"生图功能已{'开启' if newState else '关闭'}。"

    def _runPresetCommand(self, body: str) -> str:
        """执行预设命令；WebUI 自定义别名和默认 /预设 都使用同一份逻辑。"""

        if not body:
            return self.data.formatPresetList()
        if body.startswith("查看 "):
            return self.data.getPresetDetail(body[3:].strip())
        if body.startswith("添加 "):
            return self._addPreset(body[3:].strip())
        if body.startswith("删除 "):
            name = body[3:].strip()
            return f"预设已删除：{name}" if self.data.removePreset(name) else f"预设不存在：{name}"
        return "格式：/预设、/预设 查看 名称、/预设 添加 名称:内容、/预设 删除 名称"

    def _matchConfiguredCommand(self, event: AstrMessageEvent) -> dict[str, str] | None:
        """从消息文本里匹配 WebUI 配置的命令词；前缀不再自定义，保持 AstrBot 标准斜杠命令习惯。"""

        text = (event.message_str or "").strip()
        if not text.startswith("/"):
            return None

        bodyText = text[1:].strip()
        commandMap = {
            "draw": self.data.drawCommands,
            "help": self.data.helpCommands,
            "queue": self.data.queueCommands,
            "cancel": self.data.cancelCommands,
            "model": self.data.modelCommands,
            "toggle": self.data.toggleCommands,
            "points": self.data.pointsCommands,
            "rank": self.data.rankCommands,
            "preset": self.data.presetCommands,
            "optimize": self.data.optimizeCommands,
        }
        for kind, names in commandMap.items():
            for name in sorted(names, key=len, reverse=True):
                if bodyText == name or bodyText.startswith(f"{name} "):
                    return {"kind": kind, "name": name, "body": bodyText[len(name) :].strip()}
        return None

    def _isBuiltInCommand(self, commandName: str) -> bool:
        """判断命令词是不是写在 @filter.command 里的默认命令；默认命令不在消息钩子里重复执行。"""

        return commandName in {
            "生图",
            "画图",
            "生成",
            "生图帮助",
            "画图帮助",
            "生图队列",
            "画图队列",
            "取消生图",
            "取消画图",
            "生图取消",
            "生图模型",
            "画图模型",
            "生图开关",
            "生图积分",
            "积分",
            "分",
            "积分排行",
            "积分榜",
            "榜",
            "生图预设",
            "画图预设",
            "预设",
            "提示词优化",
            "优化提示词",
        }

    async def _sendText(self, event: AstrMessageEvent, text: str) -> None:
        """给自定义命令发送纯文本反馈；普通命令用 yield，这里用 context 主动发消息。"""

        await self.context.send_message(event.unified_msg_origin, MessageChain().message(text))

    def _stopEvent(self, event: AstrMessageEvent) -> None:
        """命令已经直接回复后停止事件传播，避免 AstrBot 再进入 LLM 对话并自动调用 super_draw 工具。"""

        if callable(getattr(event, "stop_event", None)):
            event.stop_event()

    # ========== 文本工具 ==========

    def _commandBody(self, event: AstrMessageEvent) -> str:
        """去掉命令名，只保留用户真正输入的内容，例如 /生图 猫 会得到 猫。"""

        text = (event.message_str or "").strip()
        return text.split(maxsplit=1)[1].strip() if " " in text else ""

    def _pointKey(self, event: AstrMessageEvent) -> str:
        """生成积分用户键；群聊里同一个群同一个人一份积分，私聊里按会话单独记。"""

        senderId = self._senderId(event)
        return f"{event.unified_msg_origin}:{senderId}" if senderId else event.unified_msg_origin

    def _senderId(self, event: AstrMessageEvent) -> str:
        """尽量读取发送者 ID；AstrBot 提供 get_sender_id()，没有时从原始消息兜底读取。"""

        if callable(getattr(event, "get_sender_id", None)):
            return str(event.get_sender_id() or "")

        messageObj = getattr(event, "message_obj", None)
        raw = getattr(messageObj, "raw_message", None) if messageObj else None
        if isinstance(raw, dict):
            return str(raw.get("user_id") or raw.get("sender", {}).get("user_id") or "")
        return str(getattr(raw, "user_id", "") or "")

    def _displayName(self, event: AstrMessageEvent) -> str:
        """尽量读取群友昵称；排行榜显示昵称比显示一长串 ID 更适合手机 QQ。"""

        if callable(getattr(event, "get_sender_name", None)):
            return str(event.get_sender_name() or "")

        messageObj = getattr(event, "message_obj", None)
        raw = getattr(messageObj, "raw_message", None) if messageObj else None
        if isinstance(raw, dict):
            sender = raw.get("sender", {}) or {}
            return str(sender.get("card") or sender.get("nickname") or raw.get("user_id") or "群友")
        return str(getattr(raw, "sender", "") or self._senderId(event) or "群友")

    def _isCommandMessage(self, event: AstrMessageEvent) -> bool:
        """判断消息是否是插件命令；命令不算普通发言，避免用户刷命令套积分。"""

        text = (event.message_str or "").strip()
        if self._matchConfiguredCommand(event):
            return True

        names = [
            "生图",
            "画图",
            "生成",
            "生图队列",
            "画图队列",
            "取消生图",
            "取消画图",
            "生图取消",
            "生图模型",
            "画图模型",
            "生图预设",
            "画图预设",
            "预设",
            "生图积分",
            "积分",
            "分",
            "积分排行",
            "积分榜",
            "榜",
            "生图帮助",
            "画图帮助",
            "生图开关",
            "提示词优化",
            "优化提示词",
        ]
        commandNames = tuple(self.data.formatCommand(name) for name in names)
        return text.startswith(commandNames)

    def _latestTaskIdForUser(self, pointKey: str) -> str:
        """找到这个用户最近一个未完成生图任务；/取消生图 不需要用户复制任务 ID。"""

        for taskId, task in reversed(list(self.tasks.items())):
            info = self.taskInfo.get(taskId, {})
            if info.get("pointKey") == pointKey and not task.done():
                return taskId
        return ""

    def _imageUrls(self, text: str) -> list[str]:
        """从文本中提取 http/https URL；头像接口和明显非图片链接不强行过滤，下载失败会自动跳过。"""

        return [url.rstrip("，。,.）)") for url in re.findall(r"https?://[^\s]+", text or "")]

    def _isPrivate(self, event: AstrMessageEvent) -> bool:
        """尽量判断当前事件是否来自私聊；不同平台字段不同，判断不到时按群聊处理更安全。"""

        messageObj = getattr(event, "message_obj", None)
        raw = getattr(messageObj, "raw_message", None) if messageObj else None
        messageType = getattr(raw, "message_type", "") if raw is not None else ""
        if isinstance(raw, dict):
            messageType = raw.get("message_type", "") or raw.get("detail_type", "")
        return str(messageType).lower() in {"private", "friend", "direct"}

    def _newTaskId(self, userId: str, prompt: str) -> str:
        """用用户、时间、提示词生成 8 位任务 ID，短到适合聊天里复制，重复概率也足够低。"""

        seed = f"{time.time()}|{userId}|{prompt[:80]}"
        return hashlib.md5(seed.encode("utf-8")).hexdigest()[:8]

    def _latestTaskIdForUser(self, pointKey: str) -> str:
        """找到这个用户最近一个未完成生图任务；/取消生图 不需要用户复制任务 ID。"""

        for taskId, task in reversed(list(self.tasks.items())):
            info = self.taskInfo.get(taskId, {})
            if taskId != "clean" and info.get("pointKey") == pointKey and not task.done():
                return taskId
        return ""

    def _queueFullReason(self) -> str:
        """检查后台任务表是否已满；满了就返回原因，没满返回空字符串。"""

        running = sum(1 for taskId, task in self.tasks.items() if taskId != "clean" and not task.done())
        return f"当前生图队列已满（{running}/{self.data.maxQueueSize}），请稍后再试。" if running >= self.data.maxQueueSize else ""

    def _latestTaskIdForUser(self, pointKey: str) -> str:
        """找到这个用户最近一个未完成生图任务；/取消生图 不需要用户复制任务 ID。"""

        for taskId, task in reversed(list(self.tasks.items())):
            info = self.taskInfo.get(taskId, {})
            if info.get("pointKey") == pointKey and not task.done():
                return taskId
        return ""

    def _acceptedText(self, taskId: str, request: dict[str, Any]) -> str:
        """任务受理后返回给用户的短提示，告诉他任务 ID、图片数量和参考图数量。"""

        parts = [f"已开始生图：{taskId}"]
        if request.get("preset"):
            parts.append(f"预设：{request['preset']}")
        if request.get("images"):
            parts.append(f"已自动参考图片：{len(request['images'])}张")
        parts.append(f"发送 {self.data.formatCommand(self.data.cancelCommands[0])} 可取消你最近的任务。")
        return "\n".join(parts)

    def _isBadRequestError(self, error: Exception) -> bool:
        """
        判断这次失败是不是 400 类错误；这类错误通常代表提示词违规或请求内容不被接口接受，需要按规则扣分。

        不同 SDK 暴露错误的方式不同：OpenAI 可能有 status_code 字段，也可能只把 “Error code: 400” 写进文本。
        所以这里同时看属性和文本，让插件面对多家兼容接口时更稳。
        """

        statusCode = getattr(error, "status_code", None) or getattr(error, "status", None)
        if str(statusCode) == "400":
            return True

        text = str(error).lower()
        return "error code: 400" in text or "status code: 400" in text or "content_policy_violation" in text

    def _safeError(self, error: Exception) -> str:
        """把异常整理成适合发到群里的短文本，避免泄露 API Key 或输出超长堆栈。"""

        text = str(error).replace("\n", " ")
        text = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-***", text)
        return text[:300] or error.__class__.__name__

    def _addPreset(self, body: str) -> str:
        """处理 /预设 添加 的正文，格式正确就保存，格式错误就给例子。"""

        if ":" not in body:
            return "格式错误：/预设 添加 名称:内容，例如 /预设 添加 水彩:柔和水彩风格。"

        name, content = body.split(":", 1)
        if not name.strip() or not content.strip():
            return "预设名称和内容都不能为空。"

        self.data.addPreset(name.strip(), content.strip())
        return f"预设已保存：{name.strip()}"

    def _helpText(self) -> str:
        """返回极简帮助；所有命令词都从 WebUI 配置读取，所以帮助文本会跟随管理员配置变化。"""

        draw = self.data.formatCommand(self.data.drawCommands[0])
        cancel = self.data.formatCommand(self.data.cancelCommands[0])
        points = self.data.formatCommand(self.data.pointsCommands[0])
        rank = self.data.formatCommand(self.data.rankCommands[0])
        preset = self.data.formatCommand(self.data.presetCommands[0])
        model = self.data.formatCommand(self.data.modelCommands[0])
        toggle = self.data.formatCommand(self.data.toggleCommands[0])
        optimize = self.data.formatCommand(self.data.optimizeCommands[0])
        return "\n".join(
            [
                "生图用法：",
                f"{draw} 一只猫坐在窗边看雨，画成手机壁纸",
                f"{draw} 参考这张图，做成水彩头像",
                f"{draw} 画一张16:9电影海报，高清，两张候选图",
                f"{optimize} 猫咪头像 先优化提示词",
                f"{cancel} 取消你最近的任务",
                f"{points} 看余额，{rank} 看排行",
                f"{preset} 看预设，{model} 2 换模型，{toggle} 开关生图",
            ]
        )
