"""缓存清理功能的回归测试。

背景：上游合并时把定时清缓存的 while 循环粘在了类体里（不在任何函数内），
导致 'await' outside async function，插件整体加载失败。
这组测试锁住三件事：cleanCache 行为正确、配置字段存在且有边界、
_cacheLoop 能被 initialize 启动并被 terminate 干净地停掉。
"""

import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "astrbot_plugin_super_draw"


def _load(relPath: str, name: str):
    """把仓库里的单个 py 文件按包内模块加载，和既有测试的加载方式一致。"""
    package = sys.modules.get(PACKAGE) or types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    sys.modules.setdefault(PACKAGE, package)
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", ROOT / relPath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fileTool = _load(os.path.join("tool", "file.py"), "tool.file")
dataMod = _load("data.py", "data")
mainMod = _load("main.py", "main")


def _config(**overrides):
    cfg = dataMod.AstrBotConfig()
    cfg.update(overrides)
    return cfg


def _plugin(tmp_path: Path):
    plugin = object.__new__(mainMod.SuperDraw)
    plugin.cacheDir = tmp_path / "cache"
    plugin.cacheDir.mkdir(parents=True, exist_ok=True)
    plugin.tasks = {}
    plugin.taskMeta = {}
    plugin._tasks = {}
    plugin.data = SimpleNamespace(
        providers=[{"name": "p"}],
        modelKey="test/model",
        maxCacheCount=3,
        cleanupIntervalHours=24,
        debug=False,
        enabled=True,
    )
    return plugin


# ==================== cleanCache 本体 ====================

def test_clean_cache_keeps_newest_files(tmp_path):
    for i in range(5):  # f0 最旧 … f4 最新
        p = tmp_path / f"f{i}.png"
        p.write_bytes(b"x")
        os.utime(p, (1000 + i, 1000 + i))
    deleted = asyncio.run(fileTool.cleanCache(tmp_path, 3))
    assert deleted == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "f2.png",
        "f3.png",
        "f4.png",
    ]


def test_clean_cache_noop_under_limit(tmp_path):
    (tmp_path / "only.png").write_bytes(b"x")
    assert asyncio.run(fileTool.cleanCache(tmp_path, 5)) == 0


def test_clean_cache_missing_dir(tmp_path):
    assert asyncio.run(fileTool.cleanCache(tmp_path / "nope", 3)) == 0


# ==================== 配置读取 ====================

def test_data_reads_cache_settings_with_defaults(tmp_path):
    d = dataMod.Data(_config(), tmp_path)
    assert d.maxCacheCount == 200
    assert d.cleanupIntervalHours == 24


def test_data_clamps_invalid_cache_settings(tmp_path):
    d = dataMod.Data(
        _config(
            generation={"max_cache_files": 999999, "cleanup_interval_hours": 0}
        ),
        tmp_path,
    )
    assert d.maxCacheCount == 10000
    assert d.cleanupIntervalHours == 1


# ==================== 后台循环与生命周期 ====================

async def test_cache_loop_cleans_then_exits_on_cancel(monkeypatch, tmp_path):
    calls = []

    async def fakeClean(cacheDir, maxCount):
        calls.append((cacheDir, maxCount))
        return len(calls)

    monkeypatch.setattr(mainMod, "cleanCache", fakeClean)
    plugin = _plugin(tmp_path)
    task = asyncio.create_task(plugin._cacheLoop())
    for _ in range(200):  # 等第一次清理执行
        if calls:
            break
        await asyncio.sleep(0.01)
    assert calls and calls[0] == (plugin.cacheDir, 3)
    task.cancel()
    result = await asyncio.gather(task, return_exceptions=True)
    assert result[0] is None  # CancelledError 被捕获 → 正常退出而非抛错


async def test_initialize_starts_and_terminate_stops_loop(monkeypatch, tmp_path):
    monkeypatch.setattr(mainMod, "cleanCache", AsyncMock(return_value=0))
    plugin = _plugin(tmp_path)
    await plugin.initialize()
    name = "cacheLoop"
    assert name in plugin._tasks
    assert not plugin._tasks[name].done()
    await plugin.terminate()  # terminate 应连带取消常驻任务
    assert plugin._tasks[name].done()


async def test_terminate_tolerates_missing_background_tasks(tmp_path):
    plugin = _plugin(tmp_path)
    await plugin.terminate()  # 没有 _tasks 时也不应报错
