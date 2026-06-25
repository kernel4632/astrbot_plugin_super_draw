"""
超级生图插件数据中心 4.0.0。

只做三件事：读配置、管积分、管预设。不接收事件，不调接口，不发消息。

调用示例：
    data = Data(config, dataDir)
    data.checkPoints("group_123:user_456", 1)    # 检查积分够不够
    data.spendPoints("group_123:user_456", 1)    # 预扣积分
    data.chooseModel("2")                         # 切换模型
    data.isBanned("user_456")                     # 检查黑名单
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig


class Data:
    """插件数据中心：配置字段 + 积分 + 预设 + 模型选择。"""

    def __init__(self, config: AstrBotConfig, dataDir: Path):
        self.rawConfig = config
        self.dataDir = dataDir
        self.pointsFile = dataDir / "points.json"

        # ===== 配置字段（全部有安全默认值）=====
        self.enabled = True
        self.enableTool = True
        self.banList: list[str] = []
        self.debugMode = False

        # 生图配置
        self.providers: list[dict[str, Any]] = []
        self.models: list[dict[str, str]] = []
        self.currentModelKey = ""
        self.currentProviderIndex = 0
        self.maxRetry = 3
        self.timeout = 180
        self.maxQueueSize = 20

        # 积分
        self.enablePoints = True
        self.pointsPerMessage = 1
        self.messageCooldown = 30
        self.drawCost = 5
        self.badRequestPenalty = 5
        self.newUserPoints = 10
        self.enableDataTools = True

        # 生图后评价
        self.enableCommentary = True
        self.commentaryProviderId = ""
        self.commentaryTemplate = ""
        self.commentaryMaxLength = 150

        # 预设
        self.presets: dict[str, str] = {}

        # 运行时数据
        self.pointsByUser: dict[str, dict[str, Any]] = {}
        self.keyIndexByProvider: dict[str, int] = {}

        self._loadConfig()
        self._loadPoints()

    # ========== 配置加载 ==========

    def _loadConfig(self) -> None:
        """从 AstrBot WebUI 配置读取所有字段。"""

        self.enabled = bool(self.rawConfig.get("enabled", True))
        self.enableTool = bool(self.rawConfig.get("enable_llm_tool", True))
        self.banList = [str(x).strip() for x in (self.rawConfig.get("ban_list") or []) if str(x).strip()]
        self.debugMode = bool(self.rawConfig.get("debug_mode", False))

        # 生图配置
        gen = self.rawConfig.get("generation", {}) or {}
        self.maxRetry = self._int(gen.get("max_retry_attempts", 3), 3, 1, 10)
        self.timeout = self._int(gen.get("timeout", 180), 180, 30, 600)
        self.maxQueueSize = self._int(gen.get("max_queue_size", 20), 20, 1, 100)

        # 积分
        pts = self.rawConfig.get("points", {}) or {}
        self.enablePoints = bool(pts.get("enable_points", True))
        self.pointsPerMessage = self._int(pts.get("points_per_message", 1), 1, 0, 50)
        self.messageCooldown = self._int(pts.get("message_point_cooldown_seconds", 30), 30, 0, 3600)
        self.drawCost = self._int(pts.get("draw_cost_per_image", 5), 5, 0, 100)
        self.badRequestPenalty = self._int(pts.get("bad_request_penalty_points", 5), 5, 0, 100)
        self.newUserPoints = self._int(pts.get("new_user_points", 10), 10, 0, 1000)
        self.enableDataTools = bool(pts.get("enable_data_tools", True))

        # 生图后评价
        cmt = self.rawConfig.get("commentary", {}) or {}
        self.enableCommentary = bool(cmt.get("enable_commentary", True))
        self.commentaryProviderId = str(cmt.get("commentary_provider_id", "") or "").strip()
        self.commentaryTemplate = str(cmt.get("commentary_template", "") or "").strip()
        self.commentaryMaxLength = self._int(cmt.get("commentary_max_length", 150), 150, 20, 500)

        # 供应商和模型
        self.providers = self._parseProviders(self.rawConfig.get("api_providers", []))
        self.models = [{"key": f"{p['name']}/{p['model']}", "provider": p["name"], "model": p["model"]} for p in self.providers]
        self._applyModel(str(gen.get("model", "") or ""))

        # 预设
        self.presets = self._parsePresets(self.rawConfig.get("presets", []))

    def _parseProviders(self, raw: Any) -> list[dict[str, Any]]:
        """把供应商配置展开成一个模型一条记录。"""

        result: list[dict[str, Any]] = []
        for order, item in enumerate(raw if isinstance(raw, list) else []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or f"Provider{order + 1}").strip()
            apiType = "gemini" if str(item.get("api_type", "openai")).lower() == "gemini" else "openai"
            baseUrl = str(item.get("base_url") or "").strip().rstrip("/")
            if apiType == "openai" and baseUrl.endswith("/v1"):
                baseUrl = baseUrl[:-3].rstrip("/")
            apiKeys = [str(k).strip() for k in (item.get("api_keys") or []) if str(k).strip()]
            models = [str(m).strip() for m in (item.get("available_models") or []) if str(m).strip()]

            for model in models:
                if apiKeys:
                    result.append({"name": name, "apiType": apiType, "baseUrl": baseUrl, "apiKeys": apiKeys, "model": model, "timeout": self.timeout, "maxRetry": self.maxRetry})
        return result

    def _parsePresets(self, raw: Any) -> dict[str, str]:
        """读取预设列表。"""

        result: dict[str, str] = {}
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                prompt = str(item.get("prompt") or item.get("content") or "").strip()
                if name and prompt:
                    result[name] = prompt
            elif isinstance(item, str) and ":" in item:
                name, prompt = item.split(":", 1)
                if name.strip() and prompt.strip():
                    result[name.strip()] = prompt.strip()
        return result

    def _applyModel(self, key: str) -> None:
        """按 key 选中模型，没找到就用第一个。"""

        target = key or (self.models[0]["key"] if self.models else "")
        for index, m in enumerate(self.models):
            if m["key"] == target:
                self.currentModelKey = m["key"]
                self.currentProviderIndex = index
                return
        if self.models:
            self.currentModelKey = self.models[0]["key"]
            self.currentProviderIndex = 0

    # ========== 模型切换 ==========

    def chooseModel(self, text: str) -> str:
        """用户发送 /生图模型 时调用。无参数列出模型，有数字就切换。"""

        if not self.providers:
            return "未配置任何生图模型。"

        text = text.strip()
        if not text:
            return self.formatModelList()

        try:
            index = int(text) - 1
        except ValueError:
            return f"请输入模型编号（1~{len(self.models)}）。"

        if index < 0 or index >= len(self.models):
            return f"编号超出范围，可选 1~{len(self.models)}。"

        self.currentModelKey = self.models[index]["key"]
        self.currentProviderIndex = index
        self._saveModel()
        return f"已切换到：{self.currentModelKey}"

    def formatModelList(self) -> str:
        """格式化模型列表。"""

        lines = ["可用模型："]
        for i, m in enumerate(self.models, 1):
            marker = " ← 当前" if m["key"] == self.currentModelKey else ""
            lines.append(f"  {i}. {m['key']}{marker}")
        lines.append("\n发送 /生图模型 编号 切换")
        return "\n".join(lines)

    # ========== 黑名单 ==========

    def isBanned(self, userId: str) -> bool:
        """检查用户是否在黑名单中。"""

        return userId in self.banList

    def addBan(self, userId: str) -> str:
        """添加黑名单。"""

        userId = userId.strip()
        if not userId:
            return "用户 ID 不能为空。"
        if userId in self.banList:
            return f"{userId} 已在黑名单中。"
        self.banList.append(userId)
        self._saveBanList()
        return f"已将 {userId} 加入黑名单。"

    def removeBan(self, userId: str) -> str:
        """移除黑名单。"""

        userId = userId.strip()
        if userId not in self.banList:
            return f"{userId} 不在黑名单中。"
        self.banList.remove(userId)
        self._saveBanList()
        return f"已将 {userId} 移出黑名单。"

    def formatBanList(self) -> str:
        """格式化黑名单。"""

        if not self.banList:
            return "黑名单为空。"
        return "黑名单：\n" + "\n".join(f"  - {uid}" for uid in self.banList)

    # ========== 积分 ==========

    def checkPoints(self, userKey: str, count: int = 1) -> str | None:
        """检查积分是否足够，不够返回提示文本，够返回 None。"""

        if not self.enablePoints or self.drawCost <= 0:
            return None

        user = self._pointUser(userKey)
        cost = self.drawCost * max(1, count)
        current = int(user.get("points", 0))
        if current < cost:
            return f"积分不足：当前 {current} 分，需要 {cost} 分。多聊天可获得积分。"
        return None

    def spendPoints(self, userKey: str, count: int = 1) -> int:
        """预扣积分，返回实际扣除数额。"""

        if not self.enablePoints or self.drawCost <= 0:
            return 0

        user = self._pointUser(userKey)
        cost = self.drawCost * max(1, count)
        user["points"] = max(0, int(user.get("points", 0)) - cost)
        user["spent"] = int(user.get("spent", 0)) + cost
        self._savePoints()
        return cost

    def refundPoints(self, userKey: str, amount: int) -> None:
        """退回积分。"""

        if amount <= 0 or not self.enablePoints:
            return

        user = self._pointUser(userKey)
        user["points"] = int(user.get("points", 0)) + amount
        user["spent"] = max(0, int(user.get("spent", 0)) - amount)
        self._savePoints()

    def settleBadRequest(self, userKey: str, prepaid: int) -> int:
        """400 错误时结算：最终扣除惩罚分，多扣的退回。"""

        if not self.enablePoints or self.badRequestPenalty <= 0:
            self.refundPoints(userKey, prepaid)
            return 0

        user = self._pointUser(userKey)
        penalty = self.badRequestPenalty

        if prepaid >= penalty:
            self.refundPoints(userKey, prepaid - penalty)
            return penalty

        extra = min(int(user.get("points", 0)), penalty - prepaid)
        user["points"] = max(0, int(user.get("points", 0)) - extra)
        user["spent"] = int(user.get("spent", 0)) + extra
        self._savePoints()
        return prepaid + extra

    def addTalkPoint(self, userKey: str, displayName: str = "") -> int:
        """群聊发言加积分，冷却内不重复加。返回实际加的分。"""

        if not self.enablePoints or self.pointsPerMessage <= 0:
            return 0

        user = self._pointUser(userKey, displayName)
        now = time.time()
        if now - float(user.get("lastTalk", 0)) < self.messageCooldown:
            return 0

        user["lastTalk"] = now
        user["points"] = int(user.get("points", 0)) + self.pointsPerMessage
        user["talk"] = int(user.get("talk", 0)) + 1
        user["earned"] = int(user.get("earned", 0)) + self.pointsPerMessage
        self._savePoints()
        return self.pointsPerMessage

    def formatPoints(self, userKey: str) -> str:
        """格式化个人积分。"""

        user = self._pointUser(userKey)
        return "\n".join(
            [
                f"积分：{user.get('points', 0)} 分",
                f"发言：{user.get('talk', 0)} 次",
                f"生图消耗：{user.get('spent', 0)} 分",
            ]
        )

    def changePoints(self, userKey: str, delta: int, reason: str = "") -> str:
        """Bot 工具修改积分。"""

        user = self._pointUser(userKey)
        before = int(user.get("points", 0))
        after = max(0, before + delta)
        user["points"] = after
        if delta > 0:
            user["earned"] = int(user.get("earned", 0)) + (after - before)
        elif delta < 0:
            user["spent"] = int(user.get("spent", 0)) + (before - after)
        self._savePoints()
        note = f"（{reason}）" if reason else ""
        return f"积分：{before} → {after}{note}"

    def formatSummary(self) -> str:
        """给 Bot 工具的插件概要。"""

        return "\n".join(
            [
                f"状态：{'开启' if self.enabled else '关闭'}",
                f"模型：{self.currentModelKey or '未配置'}",
                f"积分用户数：{len(self.pointsByUser)}",
                f"预设数：{len(self.presets)}",
                f"黑名单：{len(self.banList)} 人",
            ]
        )

    # ========== 预设 ==========

    def resolvePreset(self, text: str) -> tuple[str, str | None]:
        """从用户输入中匹配预设。返回 (最终提示词, 预设名或None)。"""

        text = text.strip()
        if not text:
            return ("", None)

        # 按编号匹配：1号、2号
        for i, (name, prompt) in enumerate(self.presets.items(), 1):
            if text.startswith(f"{i}号"):
                rest = text[len(f"{i}号") :].strip()
                return (f"{prompt} {rest}".strip(), name)

        # 按名称匹配
        for name, prompt in self.presets.items():
            if text.startswith(name):
                rest = text[len(name) :].strip()
                return (f"{prompt} {rest}".strip(), name)

        return (text, None)

    def formatPresetList(self) -> str:
        """格式化预设列表。"""

        if not self.presets:
            return "暂无预设。使用 /生图预设 添加 名称:内容 来创建。"

        lines = ["预设列表："]
        for i, (name, prompt) in enumerate(self.presets.items(), 1):
            lines.append(f"  {i}号 {name}：{prompt[:30]}{'…' if len(prompt) > 30 else ''}")
        return "\n".join(lines)

    def addPreset(self, text: str) -> str:
        """添加预设，格式：名称:内容。"""

        if ":" not in text:
            return "格式错误，应为：名称:提示词内容"
        name, prompt = text.split(":", 1)
        name, prompt = name.strip(), prompt.strip()
        if not name or not prompt:
            return "名称和内容都不能为空。"
        self.presets[name] = prompt
        self._savePresets()
        return f"预设已添加：{name}"

    def removePreset(self, name: str) -> str:
        """删除预设。"""

        name = name.strip()
        if name not in self.presets:
            return f"预设不存在：{name}"
        del self.presets[name]
        self._savePresets()
        return f"预设已删除：{name}"

    # ========== 评价模板 ==========

    def buildCommentaryPrompt(self, prompt: str, modelKey: str, contextText: str, imageText: str) -> str:
        """构建生图后评价的 LLM 输入。"""

        template = self.commentaryTemplate or self._defaultCommentaryTemplate()
        return template.replace("{prompt}", prompt).replace("{model}", modelKey).replace("{context}", contextText or "无").replace("{images}", imageText or "已生成").replace("{max_length}", str(self.commentaryMaxLength)).strip()[:4000]

    def _defaultCommentaryTemplate(self) -> str:
        return "你是群聊里的 Bot，刚刚完成了一次生图。结合群聊上下文和用户需求，自然接一句短回复。\n用户需求：{prompt}\n使用模型：{model}\n群聊上下文：\n{context}\n图片信息：\n{images}\n要求：最多 {max_length} 字，像群友聊天一样自然。"

    # ========== Key 轮换 ==========

    def getNextKey(self, provider: dict[str, Any]) -> str:
        """按轮换方式取下一个 Key。"""

        keys = provider.get("apiKeys") or []
        if not keys:
            raise RuntimeError(f"{provider.get('name', '?')} 没有配置 API Key。")
        if len(keys) == 1:
            return keys[0]

        provKey = f"{provider.get('name', '')}_{provider.get('model', '')}"
        idx = self.keyIndexByProvider.get(provKey, 0)
        key = keys[idx % len(keys)]
        self.keyIndexByProvider[provKey] = (idx + 1) % len(keys)
        return key

    # ========== 开关 ==========

    def setEnabled(self, value: bool) -> None:
        self.enabled = value
        self.rawConfig["enabled"] = value
        self._saveConfig("切换开关")

    # ========== 内部工具 ==========

    def _int(self, value: Any, default: int, low: int, high: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            n = default
        return max(low, min(high, n))

    def _pointUser(self, userKey: str, displayName: str = "") -> dict[str, Any]:
        if userKey not in self.pointsByUser:
            self.pointsByUser[userKey] = {"points": self.newUserPoints, "talk": 0, "earned": self.newUserPoints, "spent": 0, "lastTalk": 0}
        user = self.pointsByUser[userKey]
        if displayName:
            user["name"] = displayName
        return user

    def _loadPoints(self) -> None:
        try:
            if self.pointsFile.exists():
                self.pointsByUser = json.loads(self.pointsFile.read_text(encoding="utf-8"))
        except Exception:
            self.pointsByUser = {}

    def _savePoints(self) -> None:
        try:
            self.pointsFile.write_text(json.dumps(self.pointsByUser, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[SuperDraw] 保存积分失败: {e}")

    def _saveModel(self) -> None:
        gen = self.rawConfig.get("generation", {}) or {}
        gen["model"] = self.currentModelKey
        self.rawConfig["generation"] = gen
        self._saveConfig("切换模型")

    def _saveBanList(self) -> None:
        self.rawConfig["ban_list"] = self.banList
        self._saveConfig("更新黑名单")

    def _savePresets(self) -> None:
        self.rawConfig["presets"] = [{"name": k, "prompt": v} for k, v in self.presets.items()]
        self._saveConfig("更新预设")

    def _saveConfig(self, action: str) -> None:
        try:
            self.rawConfig.save_config()
        except Exception as e:
            logger.warning(f"[SuperDraw] {action}保存配置失败: {e}")
