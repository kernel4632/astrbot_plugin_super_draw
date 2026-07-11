# AstrBot 超级生图插件 4.0.0

给 AstrBot 用的群聊生图插件。用户只需要发 `/生图`，插件自动判断文生图还是图生图。Bot 也可以通过 LLM 工具自动调用生图能力。

## 快速开始

1. 把本插件放进 AstrBot 插件目录
2. 安装依赖：`pip install -r requirements.txt`
3. 在 AstrBot WebUI 的插件配置面板填写至少一个 API 供应商的 Key 和模型
4. 重启 AstrBot

验证：在群里发 `/生图 一只猫`，如果收到任务 ID 和图片就说明配置成功。

## 用户命令

| 命令                       | 说明                             | 示例                                 |
| -------------------------- | -------------------------------- | ------------------------------------ |
| `/生图 描述`               | 生成图片                         | `/生图 一只猫坐在窗边看雨`           |
| `/取消生图`                | 取消自己最近一个任务             | `/取消生图`                          |
| `/生图积分`                | 查看自己的积分                   | `/生图积分`                          |
| `/生图预设`                | 查看预设列表                     | `/生图预设`                          |
| `/生图预设 添加 名称:内容` | 添加预设                         | `/生图预设 添加 手办化:变成手办风格` |
| `/生图预设 删除 名称`      | 删除预设                         | `/生图预设 删除 手办化`              |
| `/生图预设 查看 名称`      | 查看预设详情                     | `/生图预设 查看 手办化`              |
| `/生图模型`                | 查看可用模型列表                 | `/生图模型`                          |
| `/生图模型 编号`           | 切换模型                         | `/生图模型 2`                        |
| `/生图开关`                | 切换总开关（关闭时取消所有任务） | `/生图开关`                          |
| `/生图赠分 @用户 数量`     | 管理员给用户加分                 | `/生图赠分 @张三 50`                 |

所有命令使用 AstrBot 标准命令系统（`@filter.command`），发送后会立即停止事件传播（`event.stop_event()`），不会再交给 LLM 处理。

## 图生图

不需要单独的命令。所有场景都用 `/生图`：

```
/生图 参考这张图，做成水彩头像
/生图 保持人物特征，换成赛博朋克风格
/生图 把这张图做成表情包
```

插件会自动从以下位置收集参考图：
- 当前消息里的图片
- 回复消息里的图片
- `@用户` 的头像
- 文本里的图片 URL

## 预设

预设是预先配置好的提示词模板，用户可以用编号快速套用：

```
/生图预设                    → 查看所有预设
/生图 1号 一只猫             → 套用第 1 个预设
/生图 手办化 我的头像        → 按名称套用预设
```

预设可以在 WebUI 配置面板预配置，也可以用命令动态添加。

## 积分制

积分按用户 QQ 号全局存储，同一个人在不同群共享积分。

| 事件                | 积分变化                     |
| ------------------- | ---------------------------- |
| 新用户首次出现      | +10（可配置）                |
| 群聊有效发言        | +1（可配置，有冷却时间）     |
| 生图请求            | -5（可配置）                 |
| 生图取消 / 普通失败 | 退回已扣积分                 |
| 400 内容安全错误    | 按配置扣除惩罚分，余额最低 0 |

管理员可以用 `/生图赠分 @用户 数量` 手动加分或扣分。

## LLM 工具

Bot 在对话中可以自动调用以下工具：

### super_draw — 生图

| 参数     | 类型   | 说明                       |
| -------- | ------ | -------------------------- |
| `prompt` | string | 必填，图片描述             |
| `urls`   | string | 可选，参考图 URL，逗号分隔 |

工具生图成功后，如果开启了"生图后评价"，Bot 会结合会话上下文自然追加一句评价。用户命令生图不触发评价。

### super_draw_data — 数据操作

| action          | 说明             | 额外参数                              |
| --------------- | ---------------- | ------------------------------------- |
| `summary`       | 插件概要         | —                                     |
| `my_points`     | 当前用户积分     | —                                     |
| `user_points`   | 指定用户积分     | `user_key`                            |
| `change_points` | 增减积分         | `user_key`, `delta`, `reason`         |
| `set_points`    | 设置积分到指定值 | `user_key`, `delta`(目标值), `reason` |
| `rank`          | 积分排行榜       | —                                     |

### super_draw_ban — 黑名单管理

| action   | 说明       | 额外参数  |
| -------- | ---------- | --------- |
| `list`   | 查看黑名单 | —         |
| `add`    | 加入黑名单 | `user_id` |
| `remove` | 移出黑名单 | `user_id` |

## WebUI 配置

所有配置在 `_conf_schema.json` 中定义，通过 AstrBot WebUI 可视化编辑。

| 配置项                                  | 类型          | 说明                             |
| --------------------------------------- | ------------- | -------------------------------- |
| `enabled`                               | bool          | 总开关                           |
| `enable_llm_tool`                       | bool          | 是否允许 LLM 调用工具            |
| `ban_list`                              | list          | 黑名单用户 ID                    |
| `debug_mode`                            | bool          | 调试日志                         |
| `api_providers`                         | template_list | API 供应商（OpenAI/Gemini 模板） |
| `generation.model`                      | string        | 当前模型（供应商名/模型名）      |
| `generation.max_retry_attempts`         | int           | 最大重试次数                     |
| `generation.timeout`                    | int           | 超时秒数                         |
| `generation.max_queue_size`             | int           | 最大队列长度                     |
| `points.enable_points`                  | bool          | 启用积分制                       |
| `points.points_per_message`             | int           | 发言积分                         |
| `points.message_point_cooldown_seconds` | int           | 发言冷却秒数                     |
| `points.draw_cost_per_image`            | int           | 生图消耗积分                     |
| `points.bad_request_penalty_points`     | int           | 400 错误扣分                     |
| `points.new_user_points`                | int           | 新用户初始积分                   |
| `points.enable_data_tools`              | bool          | 允许 Bot 数据工具                |
| `commentary.enable_commentary`          | bool          | 工具生图后评价                   |
| `commentary.commentary_provider_id`     | string        | 评价模型 ID                      |
| `commentary.commentary_template`        | text          | 评价模板                         |
| `commentary.commentary_max_length`      | int           | 评价最大字数                     |
| `presets`                               | template_list | 预设提示词                       |

## 架构说明

### 整体设计

插件遵循 **事件 → 指令 → 数据 → 反馈** 的单向数据流：

```
用户发 /生图 猫
  ↓
main.py 的 @filter.command("生图") 捕获事件
  ↓ event.stop_event() 阻止 LLM 接管
  ↓
main.py._handleDraw() 编排生图流程
  ├── data.py.checkPoints() 检查积分
  ├── data.py.spendPoints() 预扣积分
  ├── main.py._collectImages() 收集参考图
  └── main.py._startTask() 启动后台任务
        ↓
main.py._runTask() 在后台执行
  ├── generate.py.makeImages() 调用 API 拿图片 bytes
  ├── main.py._sendImages() 保存图片并发回聊天
  └── 失败时 data.py.refundPoints() 退回积分
```

Bot 通过 LLM 工具生图时走类似路径，但多一步评价：

```
LLM 决定调用 super_draw 工具
  ↓
main.py.toolDraw() 接收工具调用
  ↓ _realEvent() 兼容 ContextWrapper
  ↓
同样的 checkPoints → spendPoints → _startTask 流程
  ↓
_runTask() 完成后额外调用 _sendCommentary()
  ├── 读取会话历史
  ├── 调用 AstrBot 聊天模型生成评价
  └── 发送评价到聊天
```

### 文件职责

```
main.py           617 行  入口。接收命令和 LLM 工具调用，编排生图任务，收集参考图，发送结果。
                           不做数据存储，不做 API 调用。
                           关键类：SuperDraw(Star)
                           关键方法：
                             cmdDraw()          /生图 命令入口
                             toolDraw()         super_draw 工具入口
                             _handleDraw()      统一生图编排
                             _runTask()         后台执行生图
                             _sendImages()      发送图片
                             _sendCommentary()  工具生图后评价
                             _collectImages()   从消息收集参考图
                             _realEvent()       兼容 AstrBot v4.26 ContextWrapper

data.py           507 行  数据中心。读配置、管积分、管预设、管黑名单、管模型切换。
                           不接收事件，不调 API，不发消息。
                           关键类：Data
                           关键方法：
                             _loadConfig()      从 WebUI 配置读取所有字段
                             checkPoints()      检查积分是否足够
                             spendPoints()      预扣积分
                             refundPoints()     退回积分
                             settleBadRequest() 400 错误结算
                             changePoints()     Bot 工具增减积分
                             setPoints()        Bot 工具设置积分
                             chooseModel()      切换模型
                             resolvePreset()    匹配预设
                             isBanned()         检查黑名单

generate.py       320 行  生图指令。调用 OpenAI 兼容接口或 Gemini 官方接口，返回图片 bytes。
                           不认识 AstrBot，不知道用户是谁，纯粹的 API 调用层。
                           关键函数：
                             makeImages()       统一生图入口，支持重试和 Key 轮换
                             _callOpenAi()      OpenAI 兼容接口
                             _callGemini()      Gemini 官方接口
                           兼容三种图片返回格式：
                             b64_json           标准 base64 编码
                             url                HTTP URL（自动下载）
                             data:image URI     data URI（自动解码）

tool/file.py               图片保存。把 bytes 写成临时文件供 AstrBot 发送。
tool/picture.py             图片格式处理。检测 MIME 类型，把动态图转成静态 PNG。

_conf_schema.json           WebUI 配置面板定义。使用 AstrBot 的 template_list 类型。
metadata.yaml               插件市场元信息。
```

### 数据流向

```
_conf_schema.json  →  AstrBot WebUI  →  data.py._loadConfig()  →  各字段
                                                                      ↓
用户消息  →  main.py 命令/工具  →  data.py 检查积分  →  generate.py 调 API
                                                                      ↓
                                                              图片 bytes
                                                                      ↓
                                              tool/file.py 保存  →  main.py 发送
                                                                      ↓
                                                              data.py 记录用量
```

### 积分数据

积分存储在 `{dataDir}/points.json`，按用户 QQ 号为 key：

```json
{
  "3091949883": {
    "points": 45,
    "talk": 120,
    "earned": 130,
    "spent": 85,
    "lastTalk": 1783584000.0,
    "name": "张三"
  }
}
```

### 关键设计决策

1. **命令直接触发，不经过 LLM**：所有 `/生图` 等命令用 `@filter.command` 注册，处理后调用 `event.stop_event()` 阻止事件继续传播到 LLM。这样用户发 `/生图 猫` 不会被 Bot 理解成"用户想画猫"然后再调用 `super_draw` 工具，避免重复生图。

2. **兼容 ContextWrapper**：AstrBot v4.26 的 LLM 工具调用传入的 `event` 可能是 `ContextWrapper` 而不是 `AstrMessageEvent`。`_realEvent()` 方法会递归提取真实事件对象。

3. **三种图片格式兼容**：不同的 OpenAI 兼容接口和 Gemini 代理返回图片的方式不同（b64_json、HTTP URL、data URI），`generate.py` 统一处理。

4. **积分全局存储**：积分按用户 QQ 号存储，不区分群聊，同一个人在所有群共享积分。

5. **400 错误精确识别**：只匹配 `content_policy`、`error code: 400`、`bad request` 等明确关键词，避免错误信息中的 base64 数据误触发惩罚扣分。

6. **工具参数防御**：所有 LLM 工具的必填参数都有默认空值，当 LLM 返回格式错误的 JSON 导致参数缺失时，返回友好提示而不是 crash。

## 新手开发指南

### 想加一个新命令？

1. 在 `main.py` 的 `# ========== 用户命令 ==========` 区域添加：
```python
@filter.command("新命令")
async def cmdNew(self, event: AstrMessageEvent):
    yield event.plain_result("响应内容")
    event.stop_event()
```

### 想加一个新配置项？

1. 在 `_conf_schema.json` 里加字段定义
2. 在 `data.py.__init__()` 里加默认值
3. 在 `data.py._loadConfig()` 里读取配置

### 想加一个新 LLM 工具？

1. 在 `main.py` 的 `# ========== LLM 工具 ==========` 区域添加：
```python
@filter.llm_tool(name="super_draw_xxx")
async def toolXxx(self, event: AstrMessageEvent, param: str = "") -> str:
    """工具描述，写清楚什么时候调用。
    Args:
        param(string): 参数描述
    """
    realEvent = self._realEvent(event)  # 兼容 ContextWrapper
    return "结果"
```

### 想修改积分逻辑？

积分相关方法全在 `data.py`：`checkPoints`、`spendPoints`、`refundPoints`、`settleBadRequest`、`changePoints`、`setPoints`。

### 想支持新的生图 API？

在 `generate.py` 里添加新的 `_callXxx()` 函数，然后在 `makeImages()` 里根据 `apiType` 分发。

## 依赖

```
openai>=1.0.0          OpenAI 兼容接口客户端
google-genai>=1.33.0   Gemini 官方 SDK
Pillow>=10.0.0         图片格式转换
pydantic>=2.0.0        数据校验
```

## 排查问题

1. `/生图开关` — 确认插件开启
2. `/生图模型` — 确认有可用模型
3. `/生图积分` — 确认积分足够
4. 检查 WebUI 里 `api_providers` 的 `api_keys` 是否填写
5. 开启 `debug_mode` 查看详细日志
6. 如果 Gemini 报依赖缺失：`pip install google-genai>=1.33.0`
7. 如果报"响应里没有图片数据"：检查模型名是否正确，该供应商是否支持该模型
