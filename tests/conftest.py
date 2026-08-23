"""为本地 pytest 提供最小的 astrbot 桩模块。

插件代码 import 了 astrbot.*，但真实 AstrBot 只在机器人运行环境里存在，
本地跑单测时用这些桩补齐接口。如果环境里已装了真 astrbot，则完全不干预。
"""

import os
import sys
import types
from pathlib import Path


def _makeModule(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


try:  # 真 AstrBot 已安装 → 什么都不做
    import astrbot  # noqa: F401
except ImportError:
    root = _makeModule("astrbot")
    api = _makeModule("astrbot.api")
    root.api = api

    # ---- 日志 ----
    class _StubLogger:
        def _log(self, *args, **kwargs):
            pass

        info = warning = error = debug = _log

    api.logger = _StubLogger()

    # ---- 消息组件：任意 kwargs 存成属性，够测试构造消息用 ----
    comp = _makeModule("astrbot.api.message_components")
    api.message_components = comp

    class _Component:
        # 和真 AstrBot 组件保持一致的字段默认值，测试只覆盖关心的字段
        _defaults = {
            "Image": {"url": "", "file": "", "path": ""},
            "Reply": {"id": "", "chain": []},
            "Node": {"content": []},
            "Nodes": {"nodes": []},
            "Forward": {"id": ""},
            "At": {"qq": ""},
        }

        def __init__(self, **kwargs):
            for k, v in self._defaults.get(type(self).__name__, {}).items():
                setattr(self, k, v)
            for k, v in kwargs.items():
                setattr(self, k, v)

    for _name in ("Image", "Reply", "Node", "Nodes", "Forward", "At"):
        setattr(comp, _name, type(_name, (_Component,), {}))

    # ---- 事件与过滤器 ----
    eventMod = _makeModule("astrbot.api.event")
    api.event = eventMod

    class AstrMessageEvent:
        pass

    class MessageChain:
        """链式构造消息，记录调用顺序方便断言。"""

        def __init__(self):
            self.items = []

        def message(self, text):
            self.items.append(("message", text))
            return self

        def file_image(self, path):
            self.items.append(("file_image", path))
            return self

    class _StubFilter:
        PermissionType = types.SimpleNamespace(ADMIN="admin")
        EventMessageType = types.SimpleNamespace(GROUP_MESSAGE="group")

        @staticmethod
        def command(*args, **kwargs):
            return lambda fn: fn

        @staticmethod
        def llm_tool(*args, **kwargs):
            return lambda fn: fn

        @staticmethod
        def permission_type(*args, **kwargs):
            return lambda fn: fn

        @staticmethod
        def event_message_type(*args, **kwargs):
            return lambda fn: fn

    eventMod.AstrMessageEvent = AstrMessageEvent
    eventMod.MessageChain = MessageChain
    eventMod.filter = _StubFilter()

    # ---- 插件基类 ----
    starMod = _makeModule("astrbot.api.star")
    api.star = starMod

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    starMod.Context = Context
    starMod.Star = Star

    # ---- core 层 ----
    _makeModule("astrbot.core")
    cfgParent = _makeModule("astrbot.core.config")
    cfgMod = _makeModule("astrbot.core.config.astrbot_config")
    cfgParent.astrbot_config = cfgMod

    class AstrBotConfig(dict):
        """dict 版配置对象：Data 里 config.get / raw['x']=v / save_config 都能用。"""

        def save_config(self):
            pass

    cfgMod.AstrBotConfig = AstrBotConfig

    starCore = _makeModule("astrbot.core.star")
    toolsMod = _makeModule("astrbot.core.star.star_tools")
    starCore.star_tools = toolsMod

    class StarTools:
        @staticmethod
        def get_data_dir() -> Path:
            return Path(os.environ.get("SUPERDRAW_DATA_DIR", "test_output/data"))

    toolsMod.StarTools = StarTools

    utilsParent = _makeModule("astrbot.core.utils")
    ioMod = _makeModule("astrbot.core.utils.io")
    utilsParent.io = ioMod

    async def download_image_by_url(url, path=None):
        return None

    ioMod.download_image_by_url = download_image_by_url
