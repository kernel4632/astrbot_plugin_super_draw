"""超级生图的业务编排层。"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .files import files
from .images import images
from .jobs import jobs
from .points import Points
from .providers import ProviderFailure, provider
from .reply import reply
from .settings import ProviderConfig, Settings


@dataclass(slots=True)
class DrawRequest:
    user_id: str
    origin: str
    message_id: str
    prompt: str
    images: list[bytes] = field(default_factory=list)
    from_tool: bool = False
    source: Any = None
    urls: list[str] = field(default_factory=list)
    message_text: str = ""


@dataclass(slots=True)
class DrawJob:
    id: str
    request: DrawRequest
    reserved_points: int
    provider: ProviderConfig
    task: asyncio.Task[None] | None = None


class App:
    """按业务顺序调用各个小模块，不实现协议和平台细节。"""

    def __init__(self, context: Any, config: Any, data_dir: str | Path):
        self.context = context
        self.settings = Settings(config)
        points_config = config.get("points", {}) or {}
        self.points = Points(Path(data_dir) / "points.json", points_config)
        self.tasks: dict[str, DrawJob] = {}
        self.keys: dict[str, int] = {}
        self.closing = False
        jobs.limit = self.settings.maxQueue

    async def draw(self, request: DrawRequest) -> str:
        """检查请求、收集参考图、预扣积分并启动后台任务。"""
        if not self.settings.enabled:
            return "生图关了，等管理员打开"
        if request.from_tool and not self.settings.enableTool:
            return "LLM 生图工具当前关闭。"
        if not request.prompt.strip():
            return self.help()
        if self.settings.ban(request.user_id):
            return "你在黑名单，画不了"

        collected: list[bytes] = []
        for url in request.urls:
            if data := await images.download(url):
                collected.append(data)
        if request.source is not None:
            collected.extend(await images.collect(request.source))
        request.source = None
        request.images = (request.images + collected)[:8]

        if message := self.points.check(request.user_id):
            return message
        selected = self.settings.select()
        if selected is None:
            return "没有可用的生图模型。"
        if jobs.active() >= jobs.limit:
            return f"队列已满（{jobs.active()}/{jobs.limit}），请稍后"

        reserved = self.points.spend(request.user_id)
        task_id = self.identify(request)
        job = DrawJob(task_id, request, reserved, selected)
        work = self.run(job)
        task = jobs.start(work)
        if task is None:
            work.close()
            self.points.refund(request.user_id, reserved)
            return f"队列已满（{jobs.active()}/{jobs.limit}），请稍后"

        job.task = task
        self.tasks[task_id] = job
        text = f"生图任务已开始：{task_id}\n模型：{selected.name}/{selected.model}"
        if request.images:
            text += f"\n参考图：{len(request.images)}张"
        return text

    async def run(self, job: DrawJob) -> None:
        """执行一次生图；所有出口都完成积分结算和临时文件删除。"""
        request = job.request
        paths: list[str] = []
        try:
            output = await provider.draw(
                job.provider,
                request.prompt,
                request.images,
                key_getter=self.key,
            )
            for data in output:
                if path := files.save(data):
                    paths.append(path)
            if not paths:
                raise ProviderFailure("unavailable", "模型没有返回可发送的图片。")

            await reply.success(
                self.context,
                request.origin,
                f"生图完成：{job.id}\n模型：{job.provider.model}",
                paths,
                request.message_id if self.settings.richTaskFeedback else "",
            )
            if request.from_tool and self.settings.enableComment:
                await self.comment(job, len(paths))
        except asyncio.CancelledError:
            self.refund(job)
            if not self.closing:
                await reply.cancel(
                    self.context,
                    request.origin,
                    f"生图任务 {job.id} 已取消，积分退给你了",
                    request.message_id if self.settings.richTaskFeedback else "",
                )
        except ProviderFailure as error:
            if error.kind == "policy":
                self.refund(job)
                penalty = self.points.penalize(
                    request.user_id,
                    self.settings.penalty400,
                    policy=True,
                )
                text = f"生图失败（{job.id}）：内容违规，扣 {penalty} 分\n{self.safe(error)}"
            else:
                self.refund(job)
                text = f"生图失败（{job.id}），积分退给你了：{self.safe(error)}"
            logger.error(f"[SuperDraw] {job.id} 失败: {error}")
            await reply.failure(
                self.context,
                request.origin,
                text,
                request.message_id if self.settings.richTaskFeedback else "",
            )
        except Exception as error:
            self.refund(job)
            logger.error(f"[SuperDraw] {job.id} 失败: {error}")
            await reply.failure(
                self.context,
                request.origin,
                f"生图失败（{job.id}），积分退给你了：{self.safe(error)}",
                request.message_id if self.settings.richTaskFeedback else "",
            )
        finally:
            for path in paths:
                files.remove(path)
            self.tasks.pop(job.id, None)

    async def comment(self, job: DrawJob, count: int) -> None:
        """工具生图成功后发送一条短评价，失败不影响生图结果。"""
        request = job.request
        try:
            history = ""
            manager = getattr(self.context, "conversation_manager", None)
            if manager is not None:
                try:
                    conversation_id = await manager.get_curr_conversation_id(request.origin)
                    if conversation_id:
                        conversation = await manager.get_conversation(
                            request.origin, conversation_id
                        )
                        history = str(getattr(conversation, "history", "") or "")[-2000:]
                except Exception:
                    history = ""
            if not history:
                history = request.message_text[-500:]

            template = self.settings.commentTemplate or (
                "你是群聊里的 Bot，刚刚完成了一次生图。结合群聊上下文和用户需求，"
                "自然接一句短回复。\n用户需求：{prompt}\n使用模型：{model}\n"
                "群聊上下文：\n{context}\n图片信息：\n{images}\n"
                "要求：最多 {max_length} 字，接续之前发起请求的人的那种回复，"
                "尽可能模仿群友说话风格。"
            )
            prompt = (
                template.replace("{prompt}", request.prompt)
                .replace("{model}", job.provider.name + "/" + job.provider.model)
                .replace("{context}", history or "无")
                .replace("{images}", f"已发送 {count} 张图片。")
                .replace("{max_length}", str(self.settings.commentMaxLen))
            )[:4000]
            provider_id = self.settings.commentProvider or await self.context.get_current_chat_provider_id(
                umo=request.origin
            )
            if not provider_id:
                return
            response = await self.context.llm_generate(
                chat_provider_id=provider_id, prompt=prompt
            )
            text = str(getattr(response, "completion_text", "") or "").strip()[
                : self.settings.commentMaxLen
            ]
            if text:
                await reply.success(self.context, request.origin, text)
        except Exception as error:
            logger.warning(f"[SuperDraw] 评价失败: {error}")

    async def close(self) -> None:
        self.closing = True
        running = [job.task for job in self.tasks.values() if job.task and not job.task.done()]
        for job in list(self.tasks.values()):
            if job.task and not job.task.done():
                jobs.cancel(job.task)
                self.refund(job)
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        self.tasks.clear()

    def cancel(self, user_id: str, task_id: str = "", admin: bool = False) -> str:
        candidates = list(reversed(self.tasks.values()))
        if task_id and admin:
            candidates = [self.tasks[task_id]] if task_id in self.tasks else []
        for job in candidates:
            if not admin and job.request.user_id != user_id:
                continue
            if job.task and jobs.cancel(job.task):
                self.refund(job)
                return f"已取消任务 {job.id}" if task_id and admin else "已取消你最近的生图任务，积分会自动退回"
        return f"任务 {task_id} 不存在或已完成" if task_id and admin else "你当前没有正在运行的任务。"

    def refund(self, job: DrawJob) -> int:
        if job.reserved_points <= 0:
            return 0
        amount = self.points.refund(job.request.user_id, job.reserved_points)
        job.reserved_points = 0
        return amount

    def toggle(self) -> bool:
        self.settings.enabled = not self.settings.enabled
        self.settings.raw["enabled"] = self.settings.enabled
        self.persist("切换开关")
        if not self.settings.enabled:
            for job in list(self.tasks.values()):
                if job.task and jobs.cancel(job.task):
                    self.refund(job)
        return self.settings.enabled

    def model(self, argument: str = "") -> str:
        if not self.settings.providers:
            return "未配置任何生图模型。"
        argument = argument.strip()
        if not argument:
            lines = ["可用模型："]
            for index, name in enumerate(self.settings.models, 1):
                current = " ← 当前" if index - 1 == self.settings.modelIndex else ""
                lines.append(f"  {index}. {name}{current}")
            lines.append("\n发送 /生图模型 编号 切换")
            return "\n".join(lines)
        try:
            index = int(argument) - 1
        except ValueError:
            return f"请输入编号（1~{len(self.settings.models)}）。"
        if self.settings.select(index) is None:
            return f"编号超出范围（1~{len(self.settings.models)}）。"
        generation = self.settings.raw.get("generation", {}) or {}
        generation["model"] = self.settings.modelKey
        self.settings.raw["generation"] = generation
        self.persist("切换模型")
        return f"已切换到：{self.settings.modelKey}"

    def resolve(self, text: str) -> tuple[str, str | None]:
        text = text.strip()
        for index, (name, prompt) in enumerate(self.settings.presets.items(), 1):
            marker = f"{index}号"
            if text.startswith(marker):
                return f"{prompt} {text[len(marker):].strip()}".strip(), name
            if text.startswith(name):
                return f"{prompt} {text[len(name):].strip()}".strip(), name
        return text, None

    def preset(self, argument: str = "") -> str:
        argument = argument.strip()
        presets = self.settings.presets
        if not argument:
            if not presets:
                return "暂无预设。用 /生图预设 添加 名称:内容 创建。"
            lines = ["预设列表："]
            for index, (name, prompt) in enumerate(presets.items(), 1):
                suffix = "…" if len(prompt) > 30 else ""
                lines.append(f"  {index}号 {name}：{prompt[:30]}{suffix}")
            return "\n".join(lines)
        if argument.startswith("添加 "):
            value = argument[3:].strip()
            if ":" not in value:
                return "格式：名称:提示词内容"
            name, prompt = (part.strip() for part in value.split(":", 1))
            if not name or not prompt:
                return "名称和内容都不能为空。"
            presets[name] = prompt
            self.store()
            return f"预设已添加：{name}"
        if argument.startswith("删除 "):
            name = argument[3:].strip()
            if name not in presets:
                return f"预设不存在：{name}"
            del presets[name]
            self.store()
            return f"预设已删除：{name}"
        if argument.startswith("查看 "):
            name = argument[3:].strip()
            return f"{name}：{presets[name]}" if name in presets else f"预设不存在：{name}"
        return "格式：/生图预设、/生图预设 添加 名称:内容、/生图预设 删除 名称、/生图预设 查看 名称"

    def ban(self, action: str, user_id: str = "") -> str:
        action, user_id = action.strip().lower(), user_id.strip()
        values = self.settings.banList
        if action == "list":
            return "黑名单：\n" + "\n".join(f"  - {uid}" for uid in values) if values else "黑名单为空。"
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
        self.settings.raw["ban_list"] = values
        self.persist("更新黑名单")
        return result

    def balance(self, user_id: str) -> str:
        value = self.points.give(user_id, 0)
        user = self.points.users[user_id]
        return f"积分：{value} 分\n发言：{user.get('talk', 0)} 次\n生图消耗：{user.get('spent', 0)} 分"

    def data(self, action: str, user_id: str, delta: int = 0, reason: str = "") -> str:
        if not self.settings.enableDataTool:
            return "数据工具当前关闭。"
        action = action.strip().lower()
        if action == "summary":
            return f"状态：{'开启' if self.settings.enabled else '关闭'}\n模型：{self.settings.modelKey or '未配置'}\n用户数：{len(self.points.users)}\n预设数：{len(self.settings.presets)}\n黑名单：{len(self.settings.banList)} 人"
        if action in {"my_points", "user_points"}:
            return self.balance(user_id)
        if action == "change_points":
            before = self.points.give(user_id, 0)
            after = self.points.give(user_id, delta)
            note = f"（{reason}）" if reason else ""
            return f"积分：{before} → {after}{note}"
        if action == "set_points":
            before = self.points.give(user_id, 0)
            after = self.points.set(user_id, delta)
            note = f"（{reason}）" if reason else ""
            return f"积分已设置：{before} → {after}{note}"
        if action == "rank":
            rows = self.points.rank()
            if not rows:
                return "暂无积分记录。"
            return "\n".join(["积分排行："] + [f"  {index}. {row.get('name') or '群友'}：{row.get('points', 0)} 分" for index, row in enumerate(rows, 1)])
        return "可用 action：summary、my_points、user_points、change_points、set_points、rank"

    def give(self, user_id: str, amount: int, reason: str = "") -> str:
        before = self.points.give(user_id, 0)
        after = self.points.give(user_id, amount)
        note = f"（{reason}）" if reason else ""
        return f"积分：{before} → {after}{note}"

    def talk(self, user_id: str, name: str) -> int:
        return self.points.talk(user_id, name)

    def key(self, selected: ProviderConfig) -> str:
        values = selected.apiKeys or []
        if not values:
            raise ProviderFailure("request", f"{selected.name} 没有 API Key。")
        marker = f"{selected.name}/{selected.model}"
        index = self.keys.get(marker, 0)
        self.keys[marker] = (index + 1) % len(values)
        return values[index % len(values)]

    def identify(self, request: DrawRequest) -> str:
        seed = f"{time.time_ns()}|{request.user_id}|{request.prompt[:80]}"
        return hashlib.md5(seed.encode()).hexdigest()[:4]

    def safe(self, error: Exception) -> str:
        return re.sub(r"(sk-|key-|AIza)[A-Za-z0-9_-]{8,}", "***", str(error))

    def store(self) -> None:
        self.settings.raw["presets"] = [
            {"name": name, "prompt": prompt}
            for name, prompt in self.settings.presets.items()
        ]
        self.persist("更新预设")

    def persist(self, action: str) -> None:
        try:
            self.settings.raw.save_config()
        except Exception as error:
            logger.warning(f"[SuperDraw] {action}保存失败: {error}")

    @staticmethod
    def help() -> str:
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


__all__ = ["App", "DrawJob", "DrawRequest"]
