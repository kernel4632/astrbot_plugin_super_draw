"""
超级生图插件的数据中心。

这个文件只做三件事：读取配置、保存用户数据、整理可给用户看的文字。
它不接收聊天事件，不调用生图接口，也不发送消息，所以新手可以放心把它当成“插件记忆本”来看。

触发事件在 main.py 里发生，main.py 会调用这里的 Data 来检查用户能不能画图、该用哪个模型、预设怎么拼。
指令真正生图时会把这里整理好的 providers 传给 generate.py。
图片生成成功后，main.py 再调用 recordUsage(userId, count) 把用量写回 usage.json。

调用示例：
    data = Data(config, dataDir)
    data.checkDrawPoints("group_123_user_456", 1)        # 检查积分够不够生图
    data.resolvePreset("手办化 一只猫")                  # 把预设名和自由描述拼成完整提示词
    data.chooseModel("2")                                # 按用户输入切换到第 2 个模型
    data.formatStatus(3, 1)                               # 给 Bot 数据工具返回可读状态
    data.recordUsage("group_123_user_456", 2)            # 生图成功后记 2 张用量
"""

from __future__ import annotations

import datetime  # 按天统计用量时要知道今天是哪一天
import json  # usage.json 和 JSON 预设都用它读写
import random  # provider 失败时可选择随机 key，避免所有请求挤到同一个 key
import time  # 冷却时间和任务耗时都用秒级时间戳
from pathlib import Path
from typing import Any

from astrbot.api import logger  # AstrBot 日志，保存配置失败时能在控制台看到原因
from astrbot.core.config.astrbot_config import AstrBotConfig  # AstrBot 传进来的插件配置对象


class Data:
    """
    插件的“数据中心”。

    初始化时会立刻读取 AstrBot 配置和 usage.json，之后主入口 main.py 只需要读这里整理好的字段。
    如果你想新增一个配置项，通常按这条路改：_conf_schema.json 加字段 → 这里加默认值 → _loadConfig() 读取。
    """

    def __init__(self, config: AstrBotConfig, dataDir: Path):
        self.rawConfig = config  # 原始配置对象，模型切换、开关、预设保存时要写回它
        self.dataDir = dataDir  # AstrBot 分配给本插件的数据目录，usage.json 和缓存都放这里
        self.usageFile = dataDir / "usage.json"  # 每日用量文件，插件重启后仍能记住今天用过多少次
        self.pointsFile = dataDir / "points.json"  # 积分文件，记录群友发言、收入、消费和余额

        self.enabled = True  # 插件总开关，关闭后命令不再真正生图
        self.enableTool = True  # 是否允许 LLM 自动调用 super_draw 工具
        self.debugMode = False  # 调试开关，打开后会在日志里写更多链路信息

        self.providers: list[dict[str, Any]] = []  # 展平后的模型列表，每个模型都是一个可直接调用的 provider
        self.models: list[dict[str, str]] = []  # 给用户看的模型列表：供应商名、模型名、完整 key
        self.currentModelKey = ""  # 当前模型名，格式是“供应商/模型”
        self.currentProviderIndex = 0  # 当前模型在 providers 里的下标，generate.py 按它找到实际配置

        self.maxConcurrent = 3  # 同时运行的任务数，避免群里刷屏时把接口打爆
        self.maxQueueSize = 20  # 后台任务最多排多少个，超过就拒绝新任务
        self.maxImagesPerTask = 4  # 单次最多生成几张图，防止一次请求消耗过多额度
        self.maxReferenceImages = 8  # 单次最多收集几张参考图，太多会拖慢接口并让模型困惑
        self.defaultQuality = "medium"  # 默认质量，用户不指定时使用它
        self.defaultSize = "auto"  # 默认比例，用户不指定时使用它
        self.saveFormat = "png"  # 发送图片前保存成什么格式
        self.promptPrefix = ""  # 管理员可配置的全局提示词前缀，例如统一加“高质量、清晰”
        self.negativePrompt = ""  # 管理员可配置的反向提示词，会拼到用户提示词末尾
        self.maxPromptLength = 2000  # 太长的提示词会截断，避免 API 拒绝或日志过大
        self.maxRetry = 3  # 一个 provider 失败后内部最多尝试多少次
        self.keyMode = "round_robin"  # 多 key 使用方式：round_robin 或 random

        self.enablePoints = True  # 是否启用积分制，开启后生图前会检查余额
        self.pointsPerMessage = 1  # 每条有效群聊发言给多少积分
        self.messagePointCooldownSeconds = 30  # 同一个人在同一个群里多久最多记一次发言积分
        self.drawCostPerImage = 5  # 每生成 1 张图消耗多少积分
        self.badRequestPenaltyPoints = 5  # 接口返回 400 时保留扣除多少积分，用来惩罚明显违规或错误提示词
        self.newUserPoints = 10  # 第一次见到用户时给的初始积分，避免新人完全不能画
        self.enablePrivatePoints = False  # 私聊是否也使用积分；默认只管群聊成员

        self.commandPrefix = "/"  # 命令前缀，QQ 上保持 / 最容易被新人识别
        self.drawCommands = ["生图", "画图", "生成"]  # 生图入口别名，管理员可在 WebUI 改成自己群里最顺口的词
        self.helpCommands = ["生图帮助", "画图帮助"]  # 帮助命令别名
        self.queueCommands = ["生图队列", "画图队列"]  # 队列命令别名
        self.cancelCommands = ["取消生图", "取消画图", "生图取消"]  # 取消命令别名
        self.modelCommands = ["生图模型", "画图模型"]  # 模型切换命令别名
        self.toggleCommands = ["生图开关"]  # 插件开关命令别名
        self.pointsCommands = ["生图积分", "积分", "分"]  # 个人积分命令别名
        self.rankCommands = ["积分排行", "积分榜", "榜"]  # 积分榜命令别名
        self.presetCommands = ["生图预设", "画图预设", "预设"]  # 预设命令别名
        self.optimizeCommands = ["提示词优化", "优化提示词"]  # 提示词优化命令别名

        self.enableDataTools = True  # 是否允许 LLM 工具读写插件数据和积分
        self.enablePromptOptimize = True  # 是否启用提示词优化命令和工具
        self.promptOptimizeProviderId = ""  # 提示词优化用的 AstrBot 聊天模型 ID，留空就用当前会话模型
        self.promptOptimizeTemplate = ""  # 提示词优化模板，留空时使用 _defaultOptimizeTemplate()
        self.enableToolCommentary = True  # LLM 工具生图完成后，是否让 Bot 结合上下文自然评价
        self.toolCommentaryProviderId = ""  # 生图后评价用的 AstrBot 聊天模型 ID，留空就用当前会话模型
        self.toolCommentaryTemplate = ""  # 生图后评价模板，留空时使用 _defaultToolCommentaryTemplate()
        self.toolCommentaryMaxLength = 180  # Bot 评价最长字数，避免图片发出后又刷一大段文字

        self.presets: dict[str, str] = {}  # 预设名 -> 预设内容，用户输入“手办化 猫”会自动拼接
        self.usageByDate: dict[str, dict[str, int]] = {}  # {日期: {用户ID: 已生成张数}}
        self.lastReqTime: dict[str, float] = {}  # {用户ID: 上次请求时间}，用于冷却限制
        self.keyIndexByProvider: dict[str, int] = {}  # {providerKey: 下一次要用的 key 下标}，用于轮换 key
        self.pointsByUser: dict[str, dict[str, Any]] = {}  # {群ID:用户ID: {points,talk,earned,spent,lastTalk}}

        self._loadConfig()
        self._loadUsage()
        self._loadPoints()

    # ========== 读取配置 ==========

    def _loadConfig(self) -> None:
        """把 AstrBot 面板里的配置读成容易使用的普通字段，字段都有默认值，所以缺配置也不会崩。"""

        self.enabled = bool(self.rawConfig.get("enabled", True))  # 总开关，配置缺失时默认开启
        self.enableTool = bool(self.rawConfig.get("enable_llm_tool", True))  # LLM 工具开关，默认允许
        self.debugMode = bool(self.rawConfig.get("debug_mode", False))  # 调试模式默认关闭

        gen = self.rawConfig.get("generation", {}) or {}  # 生图行为配置，缺失时用空 dict 防止 get 报错
        self.maxConcurrent = self._safeInt(gen.get("max_concurrent_tasks", 3), 3, 1, 20)
        self.maxQueueSize = self._safeInt(gen.get("max_queue_size", 20), 20, 1, 200)
        self.maxImagesPerTask = self._safeInt(gen.get("max_images_per_task", 4), 4, 1, 8)
        self.maxReferenceImages = self._safeInt(gen.get("max_reference_images", 8), 8, 0, 16)
        self.defaultQuality = self._choose(gen.get("default_quality", "medium"), {"auto", "low", "medium", "high"}, "medium")
        self.defaultSize = self._choose(gen.get("default_size", "auto"), {"auto", "1:1", "16:9", "9:16", "3:2", "2:3", "1024x1024", "1536x1024", "1024x1536"}, "auto")
        self.saveFormat = self._choose(gen.get("save_format", "png"), {"png", "jpeg", "webp"}, "png")
        self.promptPrefix = str(gen.get("prompt_prefix", "") or "").strip()
        self.negativePrompt = str(gen.get("negative_prompt", "") or "").strip()
        self.maxPromptLength = self._safeInt(gen.get("max_prompt_length", 2000), 2000, 100, 8000)
        self.maxRetry = self._safeInt(gen.get("max_retry_attempts", 3), 3, 1, 10)
        self.keyMode = self._choose(gen.get("key_mode", "round_robin"), {"round_robin", "random"}, "round_robin")

        points = self.rawConfig.get("points", {}) or {}  # 积分配置，把群聊活跃度转换成生图额度
        self.enablePoints = bool(points.get("enable_points", True))
        self.pointsPerMessage = self._safeInt(points.get("points_per_message", 1), 1, 0, 100)
        self.messagePointCooldownSeconds = self._safeInt(points.get("message_point_cooldown_seconds", 30), 30, 0, 86400)
        self.drawCostPerImage = self._safeInt(points.get("draw_cost_per_image", 5), 5, 0, 1000)
        self.badRequestPenaltyPoints = self._safeInt(points.get("bad_request_penalty_points", 5), 5, 0, 1000)
        self.newUserPoints = self._safeInt(points.get("new_user_points", 10), 10, 0, 10000)
        self.enablePrivatePoints = bool(points.get("enable_private_points", False))

        commands = self.rawConfig.get("commands", {}) or {}  # 命令别名配置，让管理员可以在 WebUI 里把“生图”改成“生成”等词
        self.commandPrefix = str(commands.get("command_prefix", "/") or "/").strip() or "/"
        self.drawCommands = self._safeList(commands.get("draw", ["生图", "画图", "生成"]), ["生图", "画图", "生成"])
        self.helpCommands = self._safeList(commands.get("help", ["生图帮助", "画图帮助"]), ["生图帮助", "画图帮助"])
        self.queueCommands = self._safeList(commands.get("queue", ["生图队列", "画图队列"]), ["生图队列", "画图队列"])
        self.cancelCommands = self._safeList(commands.get("cancel", ["取消生图", "取消画图", "生图取消"]), ["取消生图", "取消画图", "生图取消"])
        self.modelCommands = self._safeList(commands.get("model", ["生图模型", "画图模型"]), ["生图模型", "画图模型"])
        self.toggleCommands = self._safeList(commands.get("toggle", ["生图开关"]), ["生图开关"])
        self.pointsCommands = self._safeList(commands.get("points", ["生图积分", "积分", "分"]), ["生图积分", "积分", "分"])
        self.rankCommands = self._safeList(commands.get("rank", ["积分排行", "积分榜", "榜"]), ["积分排行", "积分榜", "榜"])
        self.presetCommands = self._safeList(commands.get("preset", ["生图预设", "画图预设", "预设"]), ["生图预设", "画图预设", "预设"])
        self.optimizeCommands = self._safeList(commands.get("optimize", ["提示词优化", "优化提示词"]), ["提示词优化", "优化提示词"])

        tools = self.rawConfig.get("data_tools", {}) or {}  # Bot 数据工具配置，控制 LLM 是否能查改积分和读取插件状态
        self.enableDataTools = bool(tools.get("enable_data_tools", True))

        optimize = self.rawConfig.get("prompt_optimize", {}) or {}  # 提示词优化配置，管理员可改开关、模型和模板
        self.enablePromptOptimize = bool(optimize.get("enable_prompt_optimize", True))
        self.promptOptimizeProviderId = str(optimize.get("optimize_provider_id", "") or "").strip()
        self.promptOptimizeTemplate = str(optimize.get("optimize_template", "") or "").strip()
        self.enableToolCommentary = bool(optimize.get("enable_tool_commentary", True))
        self.toolCommentaryProviderId = str(optimize.get("tool_commentary_provider_id", "") or "").strip()
        self.toolCommentaryTemplate = str(optimize.get("tool_commentary_template", "") or "").strip()
        self.toolCommentaryMaxLength = self._safeInt(optimize.get("tool_commentary_max_length", 180), 180, 20, 800)

        self.providers = self._parseProviders(self.rawConfig.get("api_providers", []))
        self.models = [{"key": f"{p['name']}/{p['model']}", "provider": p["name"], "model": p["model"]} for p in self.providers]
        self._applyModel(str(gen.get("model", "") or ""))

        self.presets = self._parsePresets(self.rawConfig.get("presets", []))

    def _parseProviders(self, raw: Any) -> list[dict[str, Any]]:
        """把配置里的供应商展开成“一个模型一条 provider”，这样切模型就是切 provider 下标。"""

        result: list[dict[str, Any]] = []

        for order, item in enumerate(raw if isinstance(raw, list) else []):
            if not isinstance(item, dict):
                continue  # 配置面板一般不会给错，但跳过坏数据能让插件更稳

            name = str(item.get("name") or f"Provider{order + 1}").strip()
            apiType = "gemini" if str(item.get("api_type", "openai")).lower() == "gemini" else "openai"
            baseUrl = self._cleanBaseUrl(str(item.get("base_url") or ""), apiType)
            apiKeys = [str(key).strip() for key in item.get("api_keys", []) if str(key).strip()]
            models = [str(model).strip() for model in item.get("available_models", []) if str(model).strip()]
            timeout = self._safeInt(item.get("timeout", 180), 180, 30, 900)

            for model in models:
                if not apiKeys:
                    continue  # 没 key 的模型不能调用，直接不放进模型列表，避免用户误选
                result.append({"name": name, "apiType": apiType, "baseUrl": baseUrl, "apiKeys": apiKeys, "model": model, "timeout": timeout, "maxRetry": self.maxRetry})

        return result

    def _parsePresets(self, raw: Any) -> dict[str, str]:
        """读取预设列表，支持“名称:内容”和 {"name": "名称", "prompt": "内容"} 两种格式。"""

        result: dict[str, str] = {}

        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                prompt = str(item.get("prompt") or item.get("content") or "").strip()
                if name and prompt:
                    result[name] = prompt
                continue

            text = str(item or "")
            if ":" in text:
                name, prompt = text.split(":", 1)
                if name.strip() and prompt.strip():
                    result[name.strip()] = prompt.strip()

        return result

    def _cleanBaseUrl(self, baseUrl: str, apiType: str) -> str:
        """清理接口地址，OpenAI 兼容接口内部会自动补 /v1，所以这里先去掉用户多写的 /v1。"""

        if apiType == "gemini":
            return baseUrl.rstrip("/")

        cleanUrl = (baseUrl or "https://api.openai.com").rstrip("/")
        return cleanUrl[:-3].rstrip("/") if cleanUrl.endswith("/v1") else cleanUrl

    def _safeInt(self, value: Any, default: int, low: int, high: int) -> int:
        """把配置值安全转成整数，并夹在 low 到 high 之间，避免用户填错导致插件启动失败。"""

        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default

        return max(low, min(high, number))

    def _choose(self, value: Any, choices: set[str], default: str) -> str:
        """从固定选项里挑一个合法值；如果用户手写配置写错，就回到安全默认值。"""

        text = str(value or default).strip()
        return text if text in choices else default

    def _safeList(self, value: Any, default: list[str]) -> list[str]:
        """把 WebUI 的列表配置整理成去重字符串列表；用户清空时回到默认值，避免所有命令都失效。"""

        items = value if isinstance(value, list) else default
        result = []
        for item in items:
            text = str(item or "").strip().lstrip("/")
            if text and text not in result:
                result.append(text)
        return result or default

    def _applyModel(self, key: str) -> None:
        """按完整模型 key 选中模型；如果 key 为空或不存在，就自动选第一个可用模型。"""

        target = key or (self.models[0]["key"] if self.models else "")
        matched = next((model for model in self.models if model["key"] == target), self.models[0] if self.models else None)

        if not matched:
            self.currentModelKey = ""
            self.currentProviderIndex = 0
            return

        self.currentModelKey = matched["key"]
        self.currentProviderIndex = next((index for index, provider in enumerate(self.providers) if provider["name"] == matched["provider"] and provider["model"] == matched["model"]), 0)

    # ========== 历史用量 ==========

    def recordUsage(self, userId: str, imageCount: int = 1) -> None:
        """生图成功后记录用量；现在不做每日限制，只保留轻量统计，避免成功发图后因为旧配置字段报错。"""

        today = datetime.date.today().isoformat()
        todayUsage = self.usageByDate.setdefault(today, {})
        todayUsage[userId] = todayUsage.get(userId, 0) + max(1, imageCount)
        self._saveUsage()

    def _loadUsage(self) -> None:
        """启动时读取 usage.json，并清理 14 天前的记录，让文件长期保持很小。"""

        if not self.usageFile.exists():
            return

        try:
            self.usageByDate = json.loads(self.usageFile.read_text("utf-8"))
            today = datetime.date.today()
            oldDays = [day for day in self.usageByDate if (today - datetime.date.fromisoformat(day)).days > 14]
            for day in oldDays:
                del self.usageByDate[day]
            self._saveUsage()
        except Exception as error:
            logger.warning(f"[SuperDraw] 读取用量文件失败，已从空记录开始: {error}")
            self.usageByDate = {}

    def _saveUsage(self) -> None:
        """把每日用量写回磁盘；失败只记日志，不影响用户拿图。"""

        try:
            self.dataDir.mkdir(parents=True, exist_ok=True)
            self.usageFile.write_text(json.dumps(self.usageByDate, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as error:
            logger.error(f"[SuperDraw] 保存用量失败: {error}")

    # ========== 积分 ==========

    def addTalkPoint(self, userKey: str, displayName: str = "") -> int:
        """用户有效发言后加积分；返回本次实际增加的积分，冷却中就返回 0。"""

        if not self.enablePoints or self.pointsPerMessage <= 0:
            return 0

        user = self._pointUser(userKey, displayName)
        now = time.time()
        if now - float(user.get("lastTalk", 0)) < self.messagePointCooldownSeconds:
            return 0

        user["talk"] = int(user.get("talk", 0)) + 1
        user["points"] = int(user.get("points", 0)) + self.pointsPerMessage
        user["earned"] = int(user.get("earned", 0)) + self.pointsPerMessage
        user["lastTalk"] = now
        if displayName:
            user["name"] = displayName
        self._savePoints()
        return self.pointsPerMessage

    def checkDrawPoints(self, userKey: str, imageCount: int, isPrivate: bool = False) -> str | None:
        """检查用户积分是否足够生图；足够返回 None，不足返回给用户看的原因。"""

        if not self._shouldUsePoints(isPrivate):
            return None

        cost = self.drawCostPerImage * max(1, imageCount)
        user = self._pointUser(userKey)
        if int(user.get("points", 0)) >= cost:
            return None

        return f"积分不足：当前 {user.get('points', 0)} 分，本次需要 {cost} 分。多在群里聊天可获得积分，发 {self.formatCommand(self.pointsCommands[0])} 查看余额。"

    def spendDrawPoints(self, userKey: str, imageCount: int, isPrivate: bool = False) -> int:
        """生图排队前预扣积分；返回实际扣掉的积分，失败或取消时可用 refundPoints() 退回。"""

        if not self._shouldUsePoints(isPrivate):
            return 0

        cost = self.drawCostPerImage * max(1, imageCount)
        user = self._pointUser(userKey)
        user["points"] = int(user.get("points", 0)) - cost
        user["spent"] = int(user.get("spent", 0)) + cost
        self._savePoints()
        return cost

    def refundPoints(self, userKey: str, points: int) -> None:
        """生图失败或取消时退回预扣积分，避免用户因为接口错误白白损失。"""

        if points <= 0:
            return

        user = self._pointUser(userKey)
        user["points"] = int(user.get("points", 0)) + points
        user["spent"] = max(0, int(user.get("spent", 0)) - points)
        self._savePoints()

    def settleBadRequestPoints(self, userKey: str, prepaidPoints: int) -> int:
        """
        接口返回 400 时结算积分；最终只让用户损失 WebUI 配置的惩罚分，余额永远不会低于 0。

        生图开始前通常已经预扣 drawCostPerImage，所以这里要先算清楚“已经扣了多少”和“还要不要退”。
        如果惩罚分小于预扣分，就退回多扣的部分；如果惩罚分大于预扣分，就从当前余额继续扣一点，但最多扣到 0。
        """

        if not self.enablePoints or self.badRequestPenaltyPoints <= 0:
            self.refundPoints(userKey, prepaidPoints)
            return 0

        user = self._pointUser(userKey)
        targetPenalty = self.badRequestPenaltyPoints  # 管理员在 WebUI 设置的“400 错误希望扣多少分”
        currentPoints = max(0, int(user.get("points", 0)))  # 用户当前余额，预扣后的余额已经体现在这里

        if prepaidPoints >= targetPenalty:
            refund = prepaidPoints - targetPenalty  # 预扣多于惩罚，差额退回，最终只保留 targetPenalty
            self.refundPoints(userKey, refund)
            return targetPenalty

        extraPenalty = min(currentPoints, targetPenalty - prepaidPoints)  # 预扣不够惩罚时，继续扣余额；余额不够就扣到 0
        user["points"] = currentPoints - extraPenalty
        user["spent"] = max(0, int(user.get("spent", 0))) + extraPenalty
        self._savePoints()
        return prepaidPoints + extraPenalty

    def formatPoints(self, userKey: str) -> str:
        """格式化个人积分，手机用户发送 /分 时直接返回这一段。"""

        user = self._pointUser(userKey)
        return "\n".join(
            [
                f"积分：{user.get('points', 0)} 分",
                f"发言：{user.get('talk', 0)} 次，累计获得 {user.get('earned', 0)} 分",
                f"生图已用：{user.get('spent', 0)} 分",
                f"规则：每次有效发言 +{self.pointsPerMessage}，每次生图 -{self.drawCostPerImage}，400错误 -{self.badRequestPenaltyPoints}",
            ]
        )

    def changePoints(self, userKey: str, delta: int, reason: str = "") -> str:
        """给 Bot 工具使用的积分修改指令；delta 可正可负，扣分时最低扣到 0。"""

        user = self._pointUser(userKey)
        before = max(0, int(user.get("points", 0)))
        after = max(0, before + int(delta))
        user["points"] = after
        if delta > 0:
            user["earned"] = int(user.get("earned", 0)) + (after - before)
        if delta < 0:
            user["spent"] = int(user.get("spent", 0)) + (before - after)
        self._savePoints()
        note = f"，原因：{reason}" if reason else ""
        return f"积分已更新：{before} → {after}{note}"

    def getUserData(self, userKey: str) -> dict[str, Any]:
        """给 Bot 工具读取单个用户数据；返回复制后的普通 dict，避免外部误改内存。"""

        return dict(self._pointUser(userKey))

    def formatAllDataSummary(self) -> str:
        """给 Bot 工具读取插件总览；只返回摘要，避免一次把 points.json 全部塞进上下文。"""

        return "\n".join(
            [
                f"插件：{'开启' if self.enabled else '关闭'}",
                f"模型：{self.currentModelKey or '未配置'}",
                f"积分用户数：{len(self.pointsByUser)}",
                f"预设数量：{len(self.presets)}",
                f"命令前缀：{self.commandPrefix}",
                f"生图命令：{'、'.join(self.formatCommand(name) for name in self.drawCommands)}",
            ]
        )

    def buildOptimizePrompt(self, text: str) -> str:
        """把用户短描述套进 WebUI 模板，交给 AstrBot 聊天模型真正改写。"""

        cleanText = str(text or "").strip()
        template = self.promptOptimizeTemplate or self._defaultOptimizeTemplate()
        return template.replace("{prompt}", cleanText).strip()[: self.maxPromptLength]

    def optimizePrompt(self, text: str) -> str:
        """同步兜底优化；当 AstrBot 模型调用失败时，至少把模板结果返回给用户。"""

        if not self.enablePromptOptimize:
            return "提示词优化功能当前关闭。"

        cleanText = str(text or "").strip()
        if not cleanText:
            return f"请在提示词优化命令后面写你想画什么，例如 {self.formatCommand(self.optimizeCommands[0])} 猫咪头像。"

        return self.buildOptimizePrompt(cleanText)

    def buildToolCommentaryPrompt(self, request: dict[str, Any], contextText: str, imageText: str) -> str:
        """把生图任务、聊天上下文和图片信息套进评价模板，让 Bot 像自然接话一样发言。"""

        template = self.toolCommentaryTemplate or self._defaultToolCommentaryTemplate()
        values = {
            "prompt": str(request.get("prompt", "")),
            "model": self.currentModelKey or "未配置",
            "context": contextText.strip() or "暂无可读取的群聊上下文。",
            "images": imageText.strip() or "图片已生成并发送到当前聊天。",
            "max_length": str(self.toolCommentaryMaxLength),
        }
        for key, value in values.items():
            template = template.replace("{" + key + "}", value)
        return template.strip()[: max(self.maxPromptLength, 4000)]

    def _defaultOptimizeTemplate(self) -> str:
        """默认提示词优化模板；管理员可以在 WebUI 里完全替换成自己的风格。"""

        return "请把下面这句话优化成适合图像生成模型的中文提示词，保留用户原意，补充主体、构图、风格、光影、细节和用途，不要加入违规内容，只输出优化后的提示词：{prompt}"

    def _defaultToolCommentaryTemplate(self) -> str:
        """默认生图后评价模板；只给 LLM 工具生图使用，用户命令生图不会触发。"""

        return "你是群聊里的 Bot，刚刚亲自完成了一次生图。请结合群聊上下文、用户原始需求和生成图片信息，自然接一句短回复，不要像评审报告，不要复述任务编号，不要说自己无法看图。\n用户生图需求：{prompt}\n使用模型：{model}\n群聊上下文：\n{context}\n生成图片信息：\n{images}\n回复要求：最多 {max_length} 字，像群友聊天一样自然，可以点评亮点、提醒如果想改哪里可以继续说。"

    def formatCommand(self, name: str) -> str:
        """把命令词加上当前前缀；WebUI 改前缀后，帮助文本和 Bot 工具看到的是同一套命令。"""

        return f"{self.commandPrefix}{str(name or '').strip().lstrip('/')}"

    def formatPointRank(self, limit: int = 10) -> str:
        """格式化积分排行榜，按余额从高到低展示前几名。"""

        if not self.pointsByUser:
            return "暂无积分记录。"

        rows = sorted(self.pointsByUser.items(), key=lambda item: int(item[1].get("points", 0)), reverse=True)[:limit]
        lines = ["积分榜："]
        for index, (_, user) in enumerate(rows, 1):
            name = user.get("name") or "群友"
            lines.append(f"{index}. {name}：{user.get('points', 0)} 分｜发言 {user.get('talk', 0)}")
        return "\n".join(lines)

    def _shouldUsePoints(self, isPrivate: bool) -> bool:
        """判断当前场景是否启用积分；生图不再限制私聊/群聊，只按积分规则决定是否扣分。"""

        return self.enablePoints and self.drawCostPerImage > 0

    def _pointUser(self, userKey: str, displayName: str = "") -> dict[str, Any]:
        """读取或创建一个用户积分档案；新用户会获得初始积分。"""

        user = self.pointsByUser.setdefault(userKey, {"points": self.newUserPoints, "talk": 0, "earned": self.newUserPoints, "spent": 0, "lastTalk": 0, "name": displayName})
        if displayName and not user.get("name"):
            user["name"] = displayName
        return user

    def _loadPoints(self) -> None:
        """启动时读取 points.json；读坏了就从空积分表开始，避免插件启动失败。"""

        if not self.pointsFile.exists():
            return

        try:
            self.pointsByUser = json.loads(self.pointsFile.read_text("utf-8"))
        except Exception as error:
            logger.warning(f"[SuperDraw] 读取积分文件失败，已从空积分开始: {error}")
            self.pointsByUser = {}

    def _savePoints(self) -> None:
        """把积分表写回磁盘；积分是用户资产，所以使用缩进 JSON 方便人工检查。"""

        try:
            self.dataDir.mkdir(parents=True, exist_ok=True)
            self.pointsFile.write_text(json.dumps(self.pointsByUser, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as error:
            logger.error(f"[SuperDraw] 保存积分失败: {error}")

    # ========== 提示词与预设 ==========

    def resolvePreset(self, text: str) -> tuple[str, str | None]:
        """
        如果文本第一个词是预设名或预设编号，就把预设内容拼到自由描述前面。

        手机 QQ 上打“手办化”三个字比打“1号”麻烦很多，所以 /画图 1号 猫 会直接套用第 1 个预设。
        """

        cleanText = str(text or "").strip()
        if not cleanText:
            return "", None

        parts = cleanText.split(maxsplit=1)
        presetName = self._findPresetName(parts[0])
        if not presetName:
            return self.buildPrompt(cleanText), None

        presetPrompt = self._readPresetPrompt(self.presets[presetName])
        userPrompt = parts[1] if len(parts) > 1 else ""
        return self.buildPrompt(f"{presetPrompt} {userPrompt}".strip()), presetName

    def _findPresetName(self, word: str) -> str | None:
        """按预设名或数字编号找预设；数字编号从 1 开始，和 /生图预设 展示顺序一致。"""

        cleanWord = str(word or "").strip().removesuffix("号")
        if cleanWord.isdigit():
            index = int(cleanWord) - 1
            names = list(self.presets)
            return names[index] if 0 <= index < len(names) else None

        return next((name for name in self.presets if name.lower() == cleanWord.lower()), None)

    def buildPrompt(self, text: str) -> str:
        """把全局前缀、用户提示词、反向提示词合成最终 prompt，并按最大长度截断。"""

        parts = []
        if self.promptPrefix:
            parts.append(self.promptPrefix)
        if text.strip():
            parts.append(text.strip())
        if self.negativePrompt:
            parts.append(f"避免：{self.negativePrompt}")

        prompt = "，".join(parts).strip()
        return prompt[: self.maxPromptLength]

    def _readPresetPrompt(self, content: str) -> str:
        """预设内容可以直接写文本，也可以写 JSON；JSON 里优先读取 prompt 字段。"""

        text = str(content or "").strip()
        if not text.startswith("{"):
            return text

        try:
            data = json.loads(text)
            return str(data.get("prompt") or data.get("content") or text).strip()
        except Exception:
            return text

    def formatPresetList(self) -> str:
        """把预设列表整理成聊天里容易读的文本。"""

        if not self.presets:
            return "暂无预设。电脑：/预设 添加 水彩:柔和水彩风格。手机也可先让管理员在配置里加常用预设。"

        lines = ["预设编号："]
        for index, name in enumerate(self.presets, 1):
            lines.append(f"{index}. {name}")
        lines.append("\n新人用法：/画图 1号 猫、/画图 2号 头像、/画图 3号 表情包")
        lines.append("管理用法：/预设 查看 名称、/预设 添加 名称:内容、/预设 删除 名称")
        return "\n".join(lines)

    def getPresetDetail(self, name: str) -> str:
        """查看单个预设的完整内容，方便用户知道它到底会给模型加什么话。"""

        content = self.presets.get(name)
        if not content:
            return f"预设不存在：{name}"
        return f"预设「{name}」内容：\n{content}"

    def addPreset(self, name: str, content: str) -> None:
        """添加或覆盖一个预设，并立刻写回配置。"""

        self.presets[name] = content
        self._savePresets()

    def removePreset(self, name: str) -> bool:
        """删除一个预设；真的删到了返回 True，没找到返回 False。"""

        if name not in self.presets:
            return False

        del self.presets[name]
        self._savePresets()
        return True

    def _savePresets(self) -> None:
        """把当前预设写回 AstrBot 配置，保持重启后仍然存在。"""

        self.rawConfig["presets"] = [f"{name}:{prompt}" for name, prompt in self.presets.items()]
        self._saveConfig("保存预设失败")

    # ========== 模型与状态 ==========

    def chooseModel(self, userText: str) -> str:
        """根据用户输入切换模型；支持数字编号，也支持完整模型 key。"""

        text = str(userText or "").strip()
        if not text:
            return self.formatModelList()

        if text.isdigit():
            index = int(text)
            if index < 1 or index > len(self.models):
                return f"编号无效，请输入 1 到 {len(self.models)}。"
            self._applyModel(self.models[index - 1]["key"])
            self._saveModel()
            return f"已切换模型：{self.currentModelKey}"

        matched = next((model for model in self.models if model["key"].lower() == text.lower()), None)
        if not matched:
            return "没有找到这个模型。发送 /画图模型 查看可选模型。"

        self._applyModel(matched["key"])
        self._saveModel()
        return f"已切换模型：{self.currentModelKey}"

    def formatModelList(self) -> str:
        """把可用模型整理成编号列表，当前模型后面加 ✅。"""

        if not self.models:
            return "无可用模型。请先在配置面板添加 api_providers、api_keys 和 available_models。"

        lines = ["可用生图模型："]
        for index, model in enumerate(self.models, 1):
            mark = " ✅" if model["key"] == self.currentModelKey else ""
            lines.append(f"  {index}. {model['key']}{mark}")
        lines.append("\n新人用法：/画图模型 2")
        return "\n".join(lines)

    def formatStatus(self, runningCount: int, waitingCount: int) -> str:
        """整理插件状态，给 LLM 数据工具查看当前开关、模型、队列和限制。"""

        switch = "开启" if self.enabled else "关闭"
        tool = "开启" if self.enableTool else "关闭"
        return "\n".join(
            [
                f"超级生图 3.0.0：{switch}",
                f"当前模型：{self.currentModelKey or '未配置'}",
                f"LLM 工具：{tool}",
                f"运行/排队：{runningCount}/{waitingCount}",
                f"默认发送：自然语言提示词 + 自动参考图",
                f"积分规则：发言 +{self.pointsPerMessage} 分，每张图 -{self.drawCostPerImage} 分",
            ]
        )

    def getNextKey(self, provider: dict[str, Any]) -> str:
        """为 provider 取下一个 API Key；round_robin 适合均摊额度，random 适合简单打散请求。"""

        keys = provider.get("apiKeys") or []
        if not keys:
            raise RuntimeError(f"{provider.get('name', 'provider')} 没有配置 apiKeys")

        if self.keyMode == "random":
            return random.choice(keys)

        providerKey = f"{provider.get('name')}/{provider.get('model')}"
        index = self.keyIndexByProvider.get(providerKey, 0) % len(keys)
        self.keyIndexByProvider[providerKey] = index + 1
        return keys[index]

    def setEnabled(self, enabled: bool) -> None:
        """更新插件开关并保存配置，/生图开关 会调用它。"""

        self.enabled = enabled
        self.rawConfig["enabled"] = enabled
        self._saveConfig("保存开关失败")

    def _saveModel(self) -> None:
        """把当前模型写回 generation.model，重启后继续使用同一个模型。"""

        gen = self.rawConfig.get("generation", {}) or {}
        gen["model"] = self.currentModelKey
        self.rawConfig["generation"] = gen
        self._saveConfig("保存模型配置失败")

    def _saveConfig(self, action: str) -> None:
        """统一保存 AstrBot 配置；失败只写日志，避免命令直接崩溃。"""

        try:
            self.rawConfig.save_config()
        except Exception as error:
            logger.error(f"[SuperDraw] {action}: {error}")


PluginData = Data  # 兼容旧代码或外部脚本里仍然 import PluginData 的写法
