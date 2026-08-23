"""插件配置的轻量读取器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ProviderConfig:
    name: str = "Provider"
    apiType: str = "openai"
    baseUrl: str = ""
    apiKeys: list[str] | None = None
    model: str = ""
    timeout: int = 180
    maxRetry: int = 3

    def __post_init__(self) -> None:
        self.apiKeys = list(self.apiKeys or [])


class Settings:
    """读取生图插件需要的配置，不负责发请求或保存 WebUI 配置。"""

    def __init__(self, config: Any):
        self.raw = config
        self.enabled = bool(self.read("enabled", True))
        self.enableTool = bool(self.read("enable_llm_tool", True))
        self.richTaskFeedback = bool(self.read("rich_task_feedback", False))
        self.debug = bool(self.read("debug_mode", False))
        self.banList = self.strings(self.read("ban_list", []))

        generation = self.mapping(self.read("generation", {}))
        self.maxRetry = self.number(generation.get("max_retry_attempts", 3), 3, 1, 10)
        self.timeout = self.number(generation.get("timeout", 180), 180, 1, 600)
        self.maxQueue = self.number(generation.get("max_active_jobs", 3), 3, 1, 100)
        self.modelKey = str(generation.get("model", "") or "").strip()

        commentary = self.mapping(self.read("commentary", {}))
        self.enableComment = bool(commentary.get("enable_commentary", True))
        self.commentProvider = str(commentary.get("commentary_provider_id", "") or "").strip()
        self.commentTemplate = str(commentary.get("commentary_template", "") or "").strip()
        self.commentMaxLen = self.number(commentary.get("commentary_max_length", 150), 150, 20, 500)

        points = self.mapping(self.read("points", {}))
        self.enablePoints = bool(points.get("enable_points", True))
        self.earnPerMsg = self.number(points.get("points_per_message", 1), 1, 0, 50)
        self.cooldown = self.number(points.get("message_point_cooldown_seconds", 30), 30, 0, 3600)
        self.drawCost = self.number(points.get("draw_cost_per_image", 5), 5, 0, 100)
        self.penalty400 = self.number(points.get("bad_request_penalty_points", 5), 5, 0, 100)
        self.initPoints = self.number(points.get("new_user_points", 10), 10, 0, 1000)
        self.enableDataTool = bool(points.get("enable_data_tools", True))

        self.providers: list[ProviderConfig] = []
        self.presets = self.parse(self.read("presets", []))
        self.load(self.read("api_providers", []))
        self.models = [f"{item.name}/{item.model}" for item in self.providers]
        self.modelIndex = self.models.index(self.modelKey) if self.modelKey in self.models else 0
        if self.providers and not self.modelKey:
            self.modelKey = self.models[0]
        elif self.providers and self.modelKey not in self.models:
            self.modelKey = self.models[0]

    def model(self, index: int) -> ProviderConfig | None:
        """返回指定编号的模型，编号从 0 开始。"""
        return self.providers[index] if 0 <= index < len(self.providers) else None

    def select(self, index: int | None = None) -> ProviderConfig | None:
        """返回当前模型；传入编号时先切换当前模型。"""
        if index is not None and 0 <= index < len(self.providers):
            self.modelIndex = index
            self.modelKey = self.models[index]
        return self.model(self.modelIndex)

    def preset(self, name: str = "") -> str | dict[str, str] | None:
        """按名称返回预设提示词；不传名称时返回预设副本。"""
        return dict(self.presets) if not name else self.presets.get(name)

    def ban(self, uid: str) -> bool:
        """判断用户是否在黑名单中。"""
        return str(uid).strip() in self.banList

    def read(self, name: str, default: Any) -> Any:
        if isinstance(self.raw, dict):
            return self.raw.get(name, default)
        return getattr(self.raw, "get", lambda key, fallback: fallback)(name, default)

    def load(self, values: Any) -> None:
        for item in values or []:
            if not isinstance(item, dict):
                continue
            keys = self.strings(item.get("api_keys", []))
            for model in self.strings(item.get("available_models", [])):
                if keys:
                    api_type = str(item.get("api_type", "openai")).lower()
                    if api_type not in {"openai", "openai_chat", "gemini"}:
                        continue
                    base = str(item.get("base_url", "") or "").strip().rstrip("/")
                    if api_type == "openai" and base.endswith("/v1"):
                        base = base[:-3].rstrip("/")
                    self.providers.append(ProviderConfig(
                        name=str(item.get("name", "Provider") or "Provider").strip(),
                        apiType=api_type,
                        baseUrl=base,
                        apiKeys=keys,
                        model=model,
                        timeout=self.timeout,
                        maxRetry=self.maxRetry,
                    ))

    @staticmethod
    def mapping(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def strings(value: Any) -> list[str]:
        return [str(item).strip() for item in (value or []) if str(item).strip()]

    @staticmethod
    def number(value: Any, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def parse(values: Any) -> dict[str, str]:
        result: dict[str, str] = {}
        for item in values or []:
            if isinstance(item, dict):
                name, prompt = item.get("name", ""), item.get("prompt", "")
            elif isinstance(item, str) and ":" in item:
                name, prompt = item.split(":", 1)
            else:
                continue
            if str(name).strip() and str(prompt).strip():
                result[str(name).strip()] = str(prompt).strip()
        return result
