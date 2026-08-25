# 文件结构重构计划

## 目标

让刚开始学编程的人只看文件名，就能找到想修改的功能。

文件夹和文件只使用一个具体的英文单词，不使用 `service`、`manager`、`adapter`、`effect` 等抽象术语。

## 目标结构

```text
astrbot_plugin_super_draw/
├─ main.py
├─ draw/
│  ├─ __init__.py
│  ├─ flow.py
│  ├─ picture.py
│  ├─ model.py
│  ├─ task.py
│  ├─ send.py
│  └─ temp.py
├─ user/
│  ├─ __init__.py
│  ├─ point.py
│  ├─ preset.py
│  └─ ban.py
├─ setting/
│  ├─ __init__.py
│  └─ config.py
└─ tests/
```

## 文件含义

| 文件 | 实际内容 |
| --- | --- |
| `main.py` | 接收 AstrBot 命令和工具调用 |
| `draw/flow.py` | 从开始生图到发送结果的完整顺序 |
| `draw/picture.py` | 收集、下载和转换参考图片 |
| `draw/model.py` | 请求 OpenAI、OpenAI Chat 和 Gemini |
| `draw/task.py` | 记录、限制和取消正在运行的任务 |
| `draw/send.py` | 发送成功、失败和取消消息 |
| `draw/temp.py` | 保存并删除一次性临时图片 |
| `user/point.py` | 用户积分、退款和发言奖励 |
| `user/preset.py` | 提示词预设的查看、添加和删除 |
| `user/ban.py` | 生图黑名单的查看、添加和删除 |
| `setting/config.py` | WebUI 配置和当前生图模型 |

## 移动顺序

1. 建立 `draw`、`user`、`setting` 三个目录。
2. 先移动职责已经单一的图片、模型、任务、消息、临时文件、积分和配置代码。
3. 从旧 `app.py` 中拆出预设和黑名单。
4. 将剩余的完整生图顺序放进 `draw/flow.py`。
5. 更新 `main.py` 和测试的导入路径。
6. 删除旧源码文件、`goal.md` 和 `plan.txt`。
7. 更新 README 架构说明和插件版本信息。

## 验收

- 根目录只保留 `main.py` 一个 Python 源码入口。
- 想修改参考图时，只需要打开 `draw/picture.py`。
- 想修改模型请求时，只需要打开 `draw/model.py`。
- 想修改积分时，只需要打开 `user/point.py`。
- 从 `main.py` 到 `draw/flow.py` 能看清完整生图顺序。
- 全部测试、Python 编译和配置 JSON 检查通过。

## 状态

计划已经执行完成。旧的根目录业务文件已全部迁移，README 和插件版本已同步到 `5.1.0`。
