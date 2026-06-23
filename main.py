"""
AstrBot 超级生图插件入口。

这个文件是整个插件和 AstrBot 框架的唯一连接点。
它接收用户命令和 LLM 工具调用，调用 generate.py 生图，再把结果发回聊天。

流程很简单：
    用户触发 → 检查权限 → 收集提示词和参考图 → 后台调用生图接口 → 保存图片 → 发回聊天

支持的命令：
    /生图 提示词          文生图或图生图（消息里带图就自动变成图生图）
    /生图模型 [数字]       查看或切换生图模型
    /生图队列             查看正在运行的生图任务
    /生图开关             开启或关闭生图功能
    /生图取消 任务ID       取消一个正在运行的任务
    /预设 [子命令]         查看/添加/删除预设

LLM 工具：
    super_draw            LLM 自动调用的生图工具，参数更精细
"""

from __future__ import annotations

import asyncio  # 后台任务和并发控制
import hashlib  # 生成任务 ID
import time  # 任务计时
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp  # 消息组件：Image、Forward、Reply 等
from astrbot.api import logger  # 日志
from astrbot.api.event import AstrMessageEvent, MessageChain, filter  # 事件和消息链
from astrbot.api.star import Context, Star  # 插件基类
from astrbot.core.config.astrbot_config import AstrBotConfig  # 配置对象
from astrbot.core.star.star_tools import StarTools  # 获取插件数据目录
from astrbot.core.utils.io import download_image_by_url  # 下载网络图片

from .data import PluginData  # 配置和数据层
from .generate import makeImages, closeClients  # 生图函数
from .tool.file import cleanCache, saveImage  # 文件工具


class SuperDraw(Star):
    """AstrBot 生图插件主类。框架要求继承 Star，所以用类包装，但内部逻辑是过程化的。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.dataDir = StarTools.get_data_dir()
        self.data = PluginData(config, self.dataDir)

        # 这些属性必须无条件初始化。即使插件被禁用，/生图开关 等命令仍可能被调用，
        # 如果 _tasks、_taskMeta 不存在，访问时会抛出 AttributeError。
        self.cacheDir = self.dataDir / "cache"  # 生成的图片缓存在这里
        self.semaphore = asyncio.Semaphore(self.data.maxConcurrent)  # 控制同时跑多少个生图任务
        self._tasks: dict[str, asyncio.Task] = {}  # 后台任务表：任务ID -> Task
        self._taskMeta: dict[str, dict[str, Any]] = {}  # 任务元信息：任务ID -> {uid, prompt, time}

        if not self.data.enabled:
            logger.info("[SuperDraw] 插件已禁用。")
            return

    # ========== 生命周期 ==========

    async def initialize(self):
        """插件启动时调用。"""

        if not self.data.enabled:
            return

        if not self.data.providers:
            logger.error("[SuperDraw] 未配置 provider，请在配置面板添加 api_providers。")

        # 启动后台缓存清理循环
        self._startBg(self._cleanLoop(), "clean")
        logger.info(f"[SuperDraw] 启动完成，当前模型: {self.data.currentModelKey}")

    async def terminate(self):
        """插件关闭时调用。取消所有后台任务，关闭 HTTP 客户端。"""

        if not self.data.enabled:
            return

        # 取消所有还在跑的任务
        for t in list(self._tasks.values()):
            if not t.done():
                t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

        await closeClients()  # 关闭生图接口的 HTTP 客户端

    # ========== 命令入口：/生图 ==========

    @filter.command("生图")
    async def cmdDraw(self, event: AstrMessageEvent):
        """
        /生图 提示词
        用户手动生图命令。消息里带图片就自动变成图生图。
        """

        if not self.data.enabled:
            return

        uid = event.unified_msg_origin

        # 检查用户限制（冷却、每日次数）
        if reason := self.data.checkUser(uid):
            yield event.plain_result(reason)
            return

        # 取出提示词（命令名后面的内容）
        raw = (event.message_str or "").strip()
        body = raw.split(maxsplit=1)[-1] if " " in raw else ""

        # 检查预设：如果提示词以预设名开头，就把预设内容拼上去
        prompt, presetName = self.data.resolvePreset(body)

        if not prompt:
            yield event.plain_result("请提供提示词。")
            return

        # 从消息里收集参考图
        imgs = await self._collectImages(event)

        # 创建任务 ID，告诉用户任务已经排上了
        tid = hashlib.md5(f"{time.time()}{uid}".encode()).hexdigest()[:8]
        parts = [f"任务ID:{tid}"]
        if imgs:
            parts.append(f"参考图:{len(imgs)}")
        if presetName:
            parts.append(f"预设:{presetName}")
        yield event.plain_result(" ".join(parts))

        # 后台开始生图
        self._taskMeta[tid] = {"uid": uid, "prompt": prompt[:30], "time": time.time()}
        self._startBg(
            self._runDrawTask(tid, uid, prompt, imgs, self.data.defaultSize, self.data.defaultQuality, self.data.saveFormat, 1),
            tid,
        )

    # ========== 命令入口：/生图模型 ==========

    @filter.command("生图模型")
    async def cmdModel(self, event: AstrMessageEvent):
        """
        /生图模型         查看所有可用模型
        /生图模型 2       切换到第 2 个模型
        """

        if not self.data.enabled:
            return

        arg = (event.message_str or "").strip().split(maxsplit=1)[-1] if " " in (event.message_str or "") else ""

        if not arg:
            yield event.plain_result(self.data.formatModelList())  # 不带参数就展示列表
        elif arg.isdigit():
            msg = self.data.switchModel(int(arg))  # 带数字就切换
            yield event.plain_result(msg)
        else:
            yield event.plain_result("格式: /生图模型 [数字]")

    # ========== 命令入口：/生图队列 ==========

    @filter.command("生图队列")
    async def cmdQueue(self, event: AstrMessageEvent):
        """/生图队列    查看当前运行中的生图任务。"""

        if not self.data.enabled:
            return

        active = [k for k, t in self._tasks.items() if not t.done()]

        if not active:
            yield event.plain_result("当前没有运行中的生图任务。")
            return

        lines = [f"运行中任务: {len(active)}"]
        for tid in active[-5:]:  # 最多显示最近 5 个
            meta = self._taskMeta.get(tid, {})
            elapsed = int(time.time() - meta.get("time", 0))
            lines.append(f"  {tid} | {meta.get('prompt', '?')}... | {elapsed}s")
        yield event.plain_result("\n".join(lines))

    # ========== 命令入口：/生图开关 ==========

    @filter.command("生图开关")
    async def cmdToggle(self, event: AstrMessageEvent):
        """/生图开关    切换生图功能的开关状态。"""

        newState = not self.data.enabled
        self.data.enabled = newState
        self.data.rawConfig["enabled"] = newState

        try:
            self.data.rawConfig.save_config()
        except Exception as e:
            logger.error(f"[SuperDraw] 保存配置失败: {e}")

        # 关闭时取消所有任务
        if not newState:
            for t in list(self._tasks.values()):
                if not t.done():
                    t.cancel()

        yield event.plain_result(f"生图功能已{'开启' if newState else '关闭'}。")

    # ========== 命令入口：/生图取消 ==========

    @filter.command("生图取消")
    async def cmdCancel(self, event: AstrMessageEvent):
        """/生图取消 任务ID    取消一个正在运行的生图任务。"""

        if not self.data.enabled:
            return

        arg = (event.message_str or "").strip().split(maxsplit=1)[-1] if " " in (event.message_str or "") else ""

        if not arg:
            yield event.plain_result("请提供任务ID。")
            return

        tid = arg.strip()

        if tid not in self._tasks:
            yield event.plain_result(f"任务 {tid} 不存在。")
            return

        if self._tasks[tid].done():
            yield event.plain_result(f"任务 {tid} 已经跑完了。")
            return

        self._tasks[tid].cancel()
        self._taskMeta.pop(tid, None)
        yield event.plain_result(f"任务 {tid} 已取消。")

    # ========== 命令入口：/预设 ==========

    @filter.command("预设")
    async def cmdPreset(self, event: AstrMessageEvent):
        """
        /预设                查看预设列表
        /预设 查看 名称       查看预设详情
        /预设 添加 名称:内容   添加预设
        /预设 删除 名称       删除预设
        """

        if not self.data.enabled:
            return

        text = (event.message_str or "").strip().split(maxsplit=1)[-1] if " " in (event.message_str or "") else ""

        # 没有参数就展示列表
        if not text:
            yield event.plain_result(self.data.formatPresetList())
            return

        # 查看预设详情
        if text.startswith("查看 "):
            yield event.plain_result(self.data.getPresetDetail(text[3:].strip()))
            return

        # 添加预设
        if text.startswith("添加 "):
            body = text[3:]
            if ":" not in body:
                yield event.plain_result("格式错误：/预设 添加 名称:内容")
                return
            name, content = body.split(":", 1)
            if not name.strip() or not content.strip():
                yield event.plain_result("名称和内容不能为空。")
                return
            self.data.addPreset(name.strip(), content.strip())
            yield event.plain_result(f"预设已添加：{name.strip()}")
            return

        # 删除预设
        if text.startswith("删除 "):
            name = text[3:].strip()
            if not name:
                yield event.plain_result("请提供要删除的预设名称。")
                return
            if self.data.removePreset(name):
                yield event.plain_result(f"预设已删除：{name}")
            else:
                yield event.plain_result(f"预设不存在：{name}")
            return

        yield event.plain_result("格式：/预设、/预设 查看 名称、/预设 添加 名称:内容、/预设 删除 名称")

    # ========== LLM 工具入口 ==========

    @filter.llm_tool(name="super_draw")
    async def llmDraw(
        self,
        event: AstrMessageEvent,
        prompt: str,
        size: str = "auto",
        quality: str = "auto",
        n: int = 1,
        urls: str = "",
    ) -> str:
        """
        当用户想要画画、生成图片、修图、P图、改图、AI绘画时调用本工具。
        支持文生图和图生图，会自动从聊天记录中提取参考图。

        Args:
            prompt(string): 用户想要的图片内容描述，必填
            size(string): 生成图片的比例，可选 auto、1:1、16:9、9:16、3:2、2:3
            quality(string): 图片质量，可选 auto、low、medium、high
            n(int): 生成数量，范围 1-4
            urls(string): 参考图 URL，多个地址用英文逗号分隔
        """

        if not self.data.enabled:
            return "插件禁用中。"

        uid = event.unified_msg_origin

        if reason := self.data.checkUser(uid):
            return reason

        prompt = prompt.strip()
        if not prompt:
            return "请提供 prompt。"

        # 收集参考图：先从 urls 参数里取，再从聊天上下文里取
        imgs: list[bytes] = []
        if urls:
            for u in urls.split(","):
                if b := await self._downloadImage(u.strip()):
                    imgs.append(b)
        imgs.extend(await self._collectImages(event))  # 再从消息里收集

        # 归一化参数
        size = size or self.data.defaultSize
        quality = quality or self.data.defaultQuality
        n = max(1, min(4, int(n)))

        # 后台开始生图
        tid = hashlib.md5(f"{time.time()}{uid}".encode()).hexdigest()[:8]
        self._taskMeta[tid] = {"uid": uid, "prompt": prompt[:30], "time": time.time()}
        self._startBg(
            self._runDrawTask(tid, uid, prompt, imgs, size, quality, self.data.saveFormat, n),
            tid,
        )

        return f"已启动生图任务(ID:{tid})，稍后会把图片发到聊天里。"

    # ========== 核心流程：执行生图任务 ==========

    async def _runDrawTask(self, tid: str, uid: str, prompt: str, imgs: list[bytes], size: str, quality: str, fmt: str, n: int):
        """
        后台执行生图的完整流程：调用接口 → 保存图片 → 发回消息。
        用信号量控制并发，避免同时跑太多任务。
        """

        async with self.semaphore:
            # 在控制台打印当前使用的生图模型，方便排查问题和确认调用链路
            logger.info(f"[SuperDraw] 开始生图 | 模型: {self.data.currentModelKey} | 提示词: {prompt[:40]}...")

            try:
                # 调用生图接口
                result = await makeImages(self.data.providers, self.data.currentProviderIdx, prompt, imgs, size, quality, n)

                # 记录用量
                self.data.recordUsage(uid)

                # 保存图片并发回消息
                chain = MessageChain()
                for imageBytes in result:
                    path = saveImage(self.cacheDir, imageBytes, fmt)
                    if path:
                        chain.file_image(path)
                await self.context.send_message(uid, chain)

            except Exception as e:
                logger.error(f"[SuperDraw] 生图失败: {e}")
                await self.context.send_message(uid, MessageChain().message(f"生图失败: {e}"))

            finally:
                self._taskMeta.pop(tid, None)  # 清理任务元信息

    # ========== 参考图收集 ==========

    async def _collectImages(self, event: AstrMessageEvent) -> list[bytes]:
        """
        从消息中收集所有参考图。
        会检查：消息里的图片、被回复的消息、合并转发消息、@某人的头像、正文里的图片 URL。
        """

        if not event.message_obj or not event.message_obj.message:
            return []

        imgs: list[bytes] = []

        # 遍历消息组件，提取图片
        for i, comp in enumerate(event.message_obj.message):
            if i == 0 and isinstance(comp, Comp.At):  # 跳过开头的 @机器人
                continue
            imgs.extend(await self._extractFromComp(comp, event))

        # 正文里的 HTTP(S) 图片 URL 也当参考图
        text = event.message_str or ""
        for token in text.split():
            if token.startswith(("http://", "https://")) and not token.startswith("https://q4.qlogo.cn"):
                if b := await self._downloadImage(token):
                    imgs.append(b)

        return imgs

    async def _extractFromComp(self, comp: Any, event: AstrMessageEvent | None = None) -> list[bytes]:
        """从单个消息组件里提取图片。递归处理转发、回复等嵌套结构。"""

        # 普通图片
        if isinstance(comp, Comp.Image):
            return [b] if (b := await self._downloadImage(comp.url or comp.file)) else []

        # 合并转发消息：需要调用 bot API 拉取内容
        if isinstance(comp, Comp.Forward):
            return await self._extractFromForward(comp, event)

        # 转发节点列表
        if isinstance(comp, Comp.Nodes):
            result = []
            for node in comp.nodes:
                result.extend(await self._extractFromComp(node, event))
            return result

        # 单个转发节点
        if isinstance(comp, Comp.Node):
            result = []
            for item in comp.content or []:
                result.extend(await self._extractFromComp(item, event))
            return result

        # 回复消息：从被回复的消息链里提取图片
        if isinstance(comp, Comp.Reply) and comp.chain:
            result = []
            for item in comp.chain:
                result.extend(await self._extractFromComp(item, event))
            return result

        # @某人：把他的头像当参考图（@在开头的已经跳过了，这里只处理正文里的 @）
        if isinstance(comp, Comp.At) and str(getattr(comp, "qq", "")) not in ("", "all"):
            return [b] if (b := await self._downloadImage(f"https://q4.qlogo.cn/headimg_dl?dst_uin={comp.qq}&spec=640")) else []

        return []

    async def _extractFromForward(self, comp: Comp.Forward, event: AstrMessageEvent | None) -> list[bytes]:
        """从合并转发消息中提取图片。需要调用 bot 的 get_forward_msg 接口拉取完整内容。"""

        if event is None:
            return []

        bot = getattr(event, "bot", None)
        if not bot or not callable(getattr(bot, "call_action", None)):
            return []

        # 获取转发消息 ID
        forwardId = comp.id or self._findForwardId(event)
        if not forwardId:
            return []

        # 调用 bot API 拉取转发内容
        try:
            resp = await bot.call_action("get_forward_msg", id=forwardId)
            nodes = resp.get("messages") or resp.get("data", {}).get("messages") or []
        except Exception as e:
            logger.warning(f"[SuperDraw] 拉取合并转发消息失败: {e}")
            return []

        # 从每个节点的内容里提取图片
        imgs: list[bytes] = []
        for node in nodes:
            content = node.get("content") or node.get("message") or []
            if not isinstance(content, list):
                continue
            for seg in content:
                if seg.get("type") != "image":
                    continue
                url = seg.get("data", {}).get("url") or seg.get("data", {}).get("file")
                if b := await self._downloadImage(url):
                    imgs.append(b)

        return imgs

    def _findForwardId(self, event: AstrMessageEvent) -> str:
        """从原始消息中找合并转发的 ID（有些平台不在 Forward 组件里提供 ID，要从原始数据里挖）。"""

        msgObj = getattr(event, "message_obj", None)
        raw = getattr(msgObj, "raw_message", None) if msgObj else None
        if raw is None:
            return ""

        segs = getattr(raw, "message", None) if hasattr(raw, "message") else raw.get("message", [])
        for seg in segs or []:
            if seg.get("type") == "forward":
                return seg.get("data", {}).get("id") or seg.get("data", {}).get("resid") or ""
        return ""

    # ========== 图片下载 ==========

    async def _downloadImage(self, source: str | None) -> bytes | None:
        """
        把 URL 或本地路径转成图片字节。
        网络图片会先下载到缓存目录再读取；本地文件直接读取。
        """

        if not source:
            return None

        try:
            # 本地文件直接读
            if not source.startswith("http"):
                p = Path(source)
                return p.read_bytes() if p.is_file() else None

            # 网络图片先下载到缓存目录
            fn = str(self.cacheDir / f"ref_{hashlib.md5(source.encode()).hexdigest()[:8]}")
            downloaded = await download_image_by_url(source, path=fn)
            if downloaded:
                return Path(downloaded).read_bytes()

        except Exception:
            pass

        return None

    # ========== 后台任务管理 ==========

    def _startBg(self, coro, name: str):
        """启动一个后台协程任务，并清理已完成的旧任务。"""

        # 清理已完成的任务，防止 _tasks 字典无限增长
        for done in [k for k, t in self._tasks.items() if t.done()]:
            del self._tasks[done]

        self._tasks[name] = asyncio.create_task(coro)

    async def _cleanLoop(self):
        """后台循环：定期清理缓存目录里的旧文件。"""

        while True:
            try:
                await cleanCache(self.cacheDir, self.data.maxCacheCount)
                await asyncio.sleep(self.data.cleanupIntervalHours * 3600)  # 按配置的小时数等待
            except asyncio.CancelledError:
                break  # 插件关闭时正常退出
            except Exception:
                await asyncio.sleep(60)  # 出错了等一分钟再试