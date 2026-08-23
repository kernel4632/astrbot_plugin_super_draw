"""
超级生图插件数据中心。读配置、管积分、管预设、管黑名单、管模型。
不接收事件，不调 API，不发消息。main.py 调这里的方法拿数据和文本。
"""

from __future__ import annotations

import json  # 积分文件用 JSON 存储
import time  # 发言冷却用时间戳
from pathlib import Path
from typing import Any

from astrbot.api import logger  # AstrBot 日志
from astrbot.core.config.astrbot_config import AstrBotConfig  # WebUI 配置对象


class Data:
    """插件全部数据。初始化时读配置和积分文件，之后 main.py 只调方法不直接碰字段。"""

    def __init__(self, config: AstrBotConfig, dataDir: Path):
        self.raw = config  # 原始配置对象，切模型/开关时要写回它
        self.pointsFile = dataDir / "points.json"  # 积分持久化文件路径
        self.users: dict[str, dict] = (
            {}
        )  # {QQ号: {points, talk, earned, spent, lastTalk, name}}
        self.keyIndex: dict[str, int] = (
            {}
        )  # {供应商_模型: 下次用第几个 key}，用于多 key 轮换

        # ===== 从 WebUI 配置读取所有字段 =====
        self.enabled = bool(config.get("enabled", True))  # 总开关
        self.enableTool = bool(config.get("enable_llm_tool", True))  # LLM 工具开关
        self.banList = [
            str(x).strip() for x in (config.get("ban_list") or []) if str(x).strip()
        ]  # 黑名单
        self.debug = bool(config.get("debug_mode", False))  # 调试日志

        gen = config.get("generation", {}) or {}  # 生图配置区块
        self.maxRetry = self._int(
            gen.get("max_retry_attempts", 3), 3, 1, 10
        )  # 失败重试次数
        self.timeout = self._int(gen.get("timeout", 180), 180, 30, 600)  # API 超时秒数
        self.maxQueue = self._int(
            gen.get("max_queue_size", 20), 20, 1, 100
        )  # 最大排队任务数
        self.maxCacheCount = self._int(
            gen.get("max_cache_files", 200), 200, 10, 10000
        )  # 缓存目录最多保留的图片数，超出的旧图定时删除
        self.cleanupIntervalHours = self._int(
            gen.get("cleanup_interval_hours", 24), 24, 1, 720
        )  # 缓存清理间隔（小时）

        pts = config.get("points", {}) or {}  # 积分配置区块
        self.enablePoints = bool(pts.get("enable_points", True))  # 是否启用积分
        self.earnPerMsg = self._int(
            pts.get("points_per_message", 1), 1, 0, 50
        )  # 每次发言加多少分
        self.cooldown = self._int(
            pts.get("message_point_cooldown_seconds", 30), 30, 0, 3600
        )  # 发言冷却秒
        self.drawCost = self._int(
            pts.get("draw_cost_per_image", 5), 5, 0, 100
        )  # 每次生图扣多少分
        self.penalty400 = self._int(
            pts.get("bad_request_penalty_points", 5), 5, 0, 100
        )  # 400 错误扣多少分
        self.initPoints = self._int(
            pts.get("new_user_points", 10), 10, 0, 1000
        )  # 新用户初始积分
        self.enableDataTool = bool(
            pts.get("enable_data_tools", True)
        )  # Bot 数据工具开关

        cmt = config.get("commentary", {}) or {}  # 生图后评价区块
        self.enableComment = bool(cmt.get("enable_commentary", True))  # 是否启用评价
        self.commentProvider = str(
            cmt.get("commentary_provider_id", "") or ""
        ).strip()  # 评价模型 ID
        self.commentTemplate = str(
            cmt.get("commentary_template", "") or ""
        ).strip()  # 评价模板
        self.commentMaxLen = self._int(
            cmt.get("commentary_max_length", 150), 150, 20, 500
        )  # 评价最大字数

        # ===== 解析供应商和模型 =====
        self.providers: list[dict] = []  # 展平后的供应商列表，每个模型一条
        self.models: list[str] = []  # 给用户看的模型名列表，格式 "供应商/模型"
        self.modelIndex = 0  # 当前选中的模型在 providers 里的下标
        self.modelKey = ""  # 当前模型名，如 "OpenAI/gpt-image-2"
        for item in config.get("api_providers") or []:  # 遍历 WebUI 配置的供应商
            if not isinstance(item, dict):
                continue  # 跳过非法条目
            name = str(item.get("name") or "Provider").strip()  # 供应商名称
            apiType = (
                "gemini"
                if str(item.get("api_type", "")).lower() == "gemini"
                else "openai"
            )  # 接口类型
            baseUrl = str(item.get("base_url") or "").strip().rstrip("/")  # 请求地址
            if apiType == "openai" and baseUrl.endswith("/v1"):
                baseUrl = baseUrl[:-3].rstrip("/")  # 去掉多余 /v1
            keys = [
                str(k).strip() for k in (item.get("api_keys") or []) if str(k).strip()
            ]  # API Key 列表
            for model in (
                item.get("available_models") or []
            ):  # 每个模型展开成一条 provider
                model = str(model).strip()
                if model and keys:  # 没 key 或没模型名就跳过
                    self.providers.append(
                        {
                            "name": name,
                            "apiType": apiType,
                            "baseUrl": baseUrl,
                            "apiKeys": keys,
                            "model": model,
                            "timeout": self.timeout,
                            "maxRetry": self.maxRetry,
                        }
                    )
                    self.models.append(f"{name}/{model}")  # 记录可读模型名

        saved = str(gen.get("model", "") or "")  # WebUI 上次保存的模型名
        if saved in self.models:  # 找到就选中
            self.modelIndex = self.models.index(saved)
            self.modelKey = saved
        elif self.models:  # 找不到就用第一个
            self.modelIndex = 0
            self.modelKey = self.models[0]

        # ===== 解析预设 =====
        self.presets: dict[str, str] = {}  # {预设名: 提示词内容}
        for item in config.get("presets") or []:
            if isinstance(item, dict):  # WebUI template_list 格式
                n, p = (
                    str(item.get("name") or "").strip(),
                    str(item.get("prompt") or "").strip(),
                )
                if n and p:
                    self.presets[n] = p
            elif isinstance(item, str) and ":" in item:  # 兼容旧的 "名称:内容" 格式
                n, p = item.split(":", 1)
                if n.strip() and p.strip():
                    self.presets[n.strip()] = p.strip()

        self._loadUsers()  # 从磁盘加载积分数据

    # ========== 积分 ==========

    def check(self, uid: str) -> str | None:
        """检查积分够不够。够返回 None，不够返回提示文本。"""
        if not self.enablePoints or self.drawCost <= 0:
            return None  # 积分关闭时不检查
        u = self._user(uid)  # 拿到用户数据（没有就自动创建）
        if u["points"] < self.drawCost:
            return f"积分不足：当前 {u['points']} 分，需要 {self.drawCost} 分。"
        return None

    def spend(self, uid: str) -> int:
        """预扣积分，返回实际扣了多少。生图开始前调用。"""
        if not self.enablePoints or self.drawCost <= 0:
            return 0
        u = self._user(uid)
        u["points"] = max(0, u["points"] - self.drawCost)  # 扣分，最低 0
        u["spent"] += self.drawCost  # 累计消费
        self._saveUsers()
        return self.drawCost

    def refund(self, uid: str, amount: int) -> None:
        """退回积分。取消任务或普通失败时调用。"""
        if amount <= 0 or not self.enablePoints:
            return
        u = self._user(uid)
        u["points"] += amount  # 加回积分
        u["spent"] = max(0, u["spent"] - amount)  # 减少消费记录
        self._saveUsers()

    def settle400(self, uid: str, prepaid: int) -> int:
        """400 错误结算：最终只扣惩罚分，多扣的退回，不够的从余额补。返回实际扣了多少。"""
        if not self.enablePoints or self.penalty400 <= 0:
            self.refund(uid, prepaid)  # 不惩罚就全退
            return 0
        if prepaid >= self.penalty400:  # 预扣够了，退多余的
            self.refund(uid, prepaid - self.penalty400)
            return self.penalty400
        u = self._user(uid)  # 预扣不够，从余额补
        extra = min(u["points"], self.penalty400 - prepaid)  # 最多扣到 0
        u["points"] -= extra
        u["spent"] += extra
        self._saveUsers()
        return prepaid + extra

    def give(self, uid: str, delta: int, reason: str = "") -> str:
        """增减积分（正数加，负数扣）。管理员改分和 Bot 工具都用这个。"""
        u = self._user(uid)
        before = u["points"]
        u["points"] = max(0, before + delta)  # 最低 0
        diff = u["points"] - before  # 实际变化量
        if diff > 0:
            u["earned"] += diff  # 正向记入收入
        elif diff < 0:
            u["spent"] += abs(diff)  # 负向记入消费
        self._saveUsers()
        note = f"（{reason}）" if reason else ""
        return f"积分：{before} → {u['points']}{note}"

    def setPoints(self, uid: str, value: int, reason: str = "") -> str:
        """直接设置积分到指定值。Bot 工具用。"""
        u = self._user(uid)
        before = u["points"]
        u["points"] = max(0, value)
        diff = u["points"] - before
        if diff > 0:
            u["earned"] += diff
        elif diff < 0:
            u["spent"] += abs(diff)
        self._saveUsers()
        note = f"（{reason}）" if reason else ""
        return f"积分已设置：{before} → {u['points']}{note}"

    def addTalk(self, uid: str, name: str = "") -> int:
        """群聊发言加积分。冷却内不重复加。返回实际加了多少。"""
        if not self.enablePoints or self.earnPerMsg <= 0:
            return 0
        u = self._user(uid, name)
        if time.time() - u["lastTalk"] < self.cooldown:
            return 0  # 冷却中
        u["lastTalk"] = time.time()
        u["points"] += self.earnPerMsg
        u["talk"] += 1
        u["earned"] += self.earnPerMsg
        self._saveUsers()
        return self.earnPerMsg

    def points(self, uid: str) -> str:
        """格式化个人积分文本。"""
        u = self._user(uid)
        return (
            f"积分：{u['points']} 分\n发言：{u['talk']} 次\n生图消耗：{u['spent']} 分"
        )

    # ========== 预设 ==========

    def preset(self, action: str, args: str = "") -> str:
        """预设的增删查，一个方法搞定。action: 空=列表, 添加/删除/查看。"""
        if not action:  # 无参数 → 列表
            if not self.presets:
                return "暂无预设。用 /生图预设 添加 名称:内容 创建。"
            lines = ["预设列表："]
            for i, (n, p) in enumerate(self.presets.items(), 1):
                lines.append(f"  {i}号 {n}：{p[:30]}{'…' if len(p) > 30 else ''}")
            return "\n".join(lines)
        if action.startswith("添加 "):  # 添加
            text = action[3:].strip()
            if ":" not in text:
                return "格式：名称:提示词内容"
            n, p = text.split(":", 1)
            if not n.strip() or not p.strip():
                return "名称和内容都不能为空。"
            self.presets[n.strip()] = p.strip()
            self._savePresets()
            return f"预设已添加：{n.strip()}"
        if action.startswith("删除 "):  # 删除
            n = action[3:].strip()
            if n not in self.presets:
                return f"预设不存在：{n}"
            del self.presets[n]
            self._savePresets()
            return f"预设已删除：{n}"
        if action.startswith("查看 "):  # 查看
            n = action[3:].strip()
            return (
                f"{n}：{self.presets[n]}" if n in self.presets else f"预设不存在：{n}"
            )
        return "格式：/生图预设、/生图预设 添加 名称:内容、/生图预设 删除 名称、/生图预设 查看 名称"

    def resolvePreset(self, text: str) -> tuple[str, str | None]:
        """从用户输入匹配预设。返回 (最终提示词, 预设名或None)。"""
        text = text.strip()
        if not text:
            return ("", None)
        for i, (name, prompt) in enumerate(
            self.presets.items(), 1
        ):  # 按编号匹配：1号、2号
            if text.startswith(f"{i}号"):
                return (f"{prompt} {text[len(f'{i}号'):].strip()}".strip(), name)
        for name, prompt in self.presets.items():  # 按名称匹配
            if text.startswith(name):
                return (f"{prompt} {text[len(name):].strip()}".strip(), name)
        return (text, None)  # 没匹配到，原文返回

    # ========== 模型 ==========

    def model(self, args: str = "") -> str:
        """模型的查/切换，一个方法搞定。无参数列表，有数字切换。"""
        if not self.providers:
            return "未配置任何生图模型。"
        args = args.strip()
        if not args:  # 列表
            lines = ["可用模型："]
            for i, name in enumerate(self.models, 1):
                lines.append(
                    f"  {i}. {name}{' ← 当前' if i - 1 == self.modelIndex else ''}"
                )
            lines.append("\n发送 /生图模型 编号 切换")
            return "\n".join(lines)
        try:
            idx = int(args) - 1  # 切换
        except ValueError:
            return f"请输入编号（1~{len(self.models)}）。"
        if idx < 0 or idx >= len(self.models):
            return f"编号超出范围（1~{len(self.models)}）。"
        self.modelIndex, self.modelKey = idx, self.models[idx]
        gen = self.raw.get("generation", {}) or {}
        gen["model"] = self.modelKey
        self.raw["generation"] = gen
        self._save("切换模型")
        return f"已切换到：{self.modelKey}"

    # ========== 黑名单 ==========

    def isBanned(self, uid: str) -> bool:
        return uid in self.banList

    def ban(self, action: str, uid: str = "") -> str:
        """黑名单增删查。action: list/add/remove。"""
        if action == "list":
            return (
                "黑名单：\n" + "\n".join(f"  - {u}" for u in self.banList)
                if self.banList
                else "黑名单为空。"
            )
        if action == "add":
            uid = uid.strip()
            if not uid:
                return "用户 ID 不能为空。"
            if uid in self.banList:
                return f"{uid} 已在黑名单中。"
            self.banList.append(uid)
            self.raw["ban_list"] = self.banList
            self._save("更新黑名单")
            return f"已将 {uid} 加入黑名单。"
        if action == "remove":
            uid = uid.strip()
            if uid not in self.banList:
                return f"{uid} 不在黑名单中。"
            self.banList.remove(uid)
            self.raw["ban_list"] = self.banList
            self._save("更新黑名单")
            return f"已将 {uid} 移出黑名单。"
        return "可用 action：list、add、remove"

    # ========== Bot 数据工具 ==========

    def toolAction(
        self, action: str, uid: str, delta: int = 0, reason: str = ""
    ) -> str:
        """Bot 数据工具的所有操作，一个入口。"""
        if not self.enableDataTool:
            return "数据工具当前关闭。"
        if not action:
            return "请提供 action。可用：summary、my_points、user_points、change_points、set_points、rank"
        a = action.strip().lower()
        if a == "summary":
            return f"状态：{'开启' if self.enabled else '关闭'}\n模型：{self.modelKey or '未配置'}\n用户数：{len(self.users)}\n预设数：{len(self.presets)}\n黑名单：{len(self.banList)} 人"
        if a == "my_points":
            return self.points(uid)
        if a == "user_points":
            return self.points(uid)
        if a == "change_points":
            return self.give(uid, delta, reason)
        if a == "set_points":
            return self.setPoints(uid, delta, reason)
        if a == "rank":
            return self._rank()
        return "可用 action：summary、my_points、user_points、change_points、set_points、rank"

    # ========== 开关 ==========

    def toggle(self) -> None:
        self.enabled = not self.enabled
        self.raw["enabled"] = self.enabled
        self._save("切换开关")

    # ========== 评价模板 ==========

    def commentPrompt(self, prompt: str, context: str, images: str) -> str:
        """构建生图后评价的 LLM 输入。"""
        t = (
            self.commentTemplate
            or "你是群聊里的 Bot，刚刚完成了一次生图。结合群聊上下文和用户需求，自然接一句短回复。\n用户需求：{prompt}\n使用模型：{model}\n群聊上下文：\n{context}\n图片信息：\n{images}\n要求：最多 {max_length} 字，接续之前发起请求的人的那种回复，尽可能的模仿群友说话风格，根据上下文。"
        )
        return (
            t.replace("{prompt}", prompt)
            .replace("{model}", self.modelKey)
            .replace("{context}", context or "无")
            .replace("{images}", images or "已生成")
            .replace("{max_length}", str(self.commentMaxLen))
            .strip()[:4000]
        )

    # ========== Key 轮换 ==========

    def nextKey(self, provider: dict) -> str:
        """取下一个 API Key，多 key 自动轮换。"""
        keys = provider.get("apiKeys") or []
        if not keys:
            raise RuntimeError(f"{provider.get('name', '?')} 没有 API Key。")
        if len(keys) == 1:
            return keys[0]
        k = f"{provider.get('name', '')}_{provider.get('model', '')}"  # 按供应商+模型分别轮换
        i = self.keyIndex.get(k, 0)
        self.keyIndex[k] = (i + 1) % len(keys)
        return keys[i % len(keys)]

    # ========== 内部方法 ==========

    def _user(self, uid: str, name: str = "") -> dict:
        """拿用户数据，没有就创建。"""
        if uid not in self.users:
            self.users[uid] = {
                "points": self.initPoints,
                "talk": 0,
                "earned": self.initPoints,
                "spent": 0,
                "lastTalk": 0.0,
                "name": "",
            }
        if name:
            self.users[uid]["name"] = name
        return self.users[uid]

    def _rank(self, limit: int = 10) -> str:
        if not self.users:
            return "暂无积分记录。"
        rows = sorted(
            self.users.items(), key=lambda x: x[1].get("points", 0), reverse=True
        )[:limit]
        lines = ["积分排行："]
        for i, (_, u) in enumerate(rows, 1):
            lines.append(f"  {i}. {u.get('name') or '群友'}：{u.get('points', 0)} 分")
        return "\n".join(lines)

    def _int(self, v: Any, d: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(v)))
        except:
            return d

    def _loadUsers(self) -> None:
        try:
            if self.pointsFile.exists():
                self.users = json.loads(self.pointsFile.read_text("utf-8"))
        except:
            self.users = {}

    def _saveUsers(self) -> None:
        try:
            self.pointsFile.write_text(
                json.dumps(self.users, ensure_ascii=False, indent=2), "utf-8"
            )
        except Exception as e:
            logger.warning(f"[SuperDraw] 保存积分失败: {e}")

    def _savePresets(self) -> None:
        self.raw["presets"] = [
            {"name": k, "prompt": v} for k, v in self.presets.items()
        ]
        self._save("更新预设")

    def _save(self, action: str) -> None:
        try:
            self.raw.save_config()
        except Exception as e:
            logger.warning(f"[SuperDraw] {action}保存失败: {e}")
