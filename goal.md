# 超级生图 V5 兼容迁移目标

## 目标

在不破坏当前 v4.0.0 行为的前提下，为插件增加可选的 QQ 富反馈模式：

- 生图指令接单后，直接对原消息发送 QQ 表情。
- 生图完成、失败、违规或取消时，引用原始生图指令消息回复。
- 默认保留现有文字反馈模式，由配置开关控制迁移。
- 先以 v4.0.0 的可选功能验证，QQ 实际稳定后再考虑将其作为 v5 默认行为。

## 已确认的 AstrBot API

官方组件源码：

`https://raw.githubusercontent.com/AstrBotDevs/AstrBot/master/astrbot/core/message/components.py`

真实定义：

```python
Comp.Face(id=21)
Comp.Reply(id=source_message_id)
```

注意事项：

- QQ 表情组件是 `Face`，字段是整数 `id`。
- 引用组件是 `Reply`，字段是 `id`，不是 `message_id`。
- `Reply.toDict()` 输出 OneBot V11 格式：`{"type": "reply", "data": {"id": "..."}}`。
- Reply 必须和至少一个其他内容组件一起发送，不能只发送 Reply。
- 后台任务使用 `self.context.send_message(umo, chain)`。
- 直接响应使用 `yield event.chain_result(chain)`。
- `Face` 主要适用于 QQ 适配器，其他平台必须允许降级。

## 本机插件参考

本机其他 AstrBot 插件没有 `Face` 或 `Reply` 的实现。

`astrbot_plugin_model_connectivity` 提供了主动发送的兼容模式：

1. 优先使用 `MessageChain`。
2. 不可用时退回组件列表，例如 `[Comp.Image.fromFileSystem(path)]`。
3. 发送结果可能是协程，也可能是普通值，需要判断是否可 await。

富反馈实现必须遵循相同的兼容思路，不能假设所有适配器和 AstrBot 版本都支持同一种发送对象。

## 配置开关

在 `_conf_schema.json` 增加一个默认关闭的布尔开关：

```json
"rich_task_feedback": {
  "description": "使用表情和引用回复反馈生图任务",
  "type": "bool",
  "default": false
}
```

`Data` 中读取为 `richTaskFeedback` 或同等语义的字段。

默认关闭时，所有现有文字反馈、任务 ID、积分、退款、取消和评价逻辑保持不变。

## 消息 ID 获取

增加统一辅助方法 `_messageId(event)`，按以下顺序获取：

1. `event.message_obj.message_id`
2. `event.message_obj.raw_message["message_id"]`
3. 对象形式的 `raw_message.message_id`
4. 获取不到时返回空字符串

消息 ID 支持字符串和整数，发送 Reply 前统一转为字符串或直接交给 `Comp.Reply`。

任务请求只保存必要元数据，不长期持有 command event：

```python
{
    "umo": event.unified_msg_origin,
    "messageId": message_id,
    ...
}
```

LLM 工具场景仍可按现有逻辑保存 event，用于生图后评价；引用回复只依赖 `umo` 和 `messageId`。

## 兼容发送策略

增加少量统一辅助方法，避免命令、成功、失败和取消路径重复拼消息：

- `_sendFace(umo, face_id)`：发送接单表情。
- `_sendReplyStatus(umo, message_id, text, paths=...)`：发送引用状态消息。
- `_messageId(event)`：提取来源消息 ID。

推荐的完成消息组件顺序：

```python
[
    Comp.Reply(id=source_message_id),
    Comp.Plain("生图完成"),
    Comp.Image.fromFileSystem(path),
]
```

失败和取消：

```python
[
    Comp.Reply(id=source_message_id),
    Comp.Plain("生图失败，积分已退回：..."),
]
```

降级规则：

- 开关关闭：沿用当前 `MessageChain().message(...).file_image(...)` 逻辑。
- 开关开启但没有 message ID：发送普通主动消息，不构造 Reply。
- `Comp.Face` 或 `Comp.Reply` 不存在：记录 warning，退回文字模式，插件不能因此加载失败。
- 组件列表发送失败：退回当前版本的 `MessageChain` 或纯文字消息。
- 非 QQ 平台不强行发送 QQ Face；优先走普通文字反馈。

## 行为设计

### 普通 `/生图` 命令

- 旧模式：继续发送“生图任务已开始：任务 ID”文字。
- 富反馈模式：任务成功创建后发送 `Face`，不再发送接单文字。
- 参数错误、黑名单、积分不足、队列已满等未创建任务的情况仍发送文字。
- 任务完成、失败、400 内容违规、取消均引用原始生图指令。

### `super_draw` LLM 工具

- 工具方法仍返回文字给 LLM，避免模型误判工具调用失败。
- 用户侧可以额外收到接单表情和引用结果。
- 工具事件没有有效 message ID 时自动使用普通主动消息。

### 其他命令

`/生图积分`、`/生图取消`、`/生图模型`、`/生图预设`、`/生图开关`、`/生图改分` 等非生图任务命令保持现有文字回复，不受富反馈开关影响。

## 不应做的改动

- 不重写现有任务队列。
- 不改变积分扣除、退款、400 惩罚和取消语义。
- 不把任务 ID 当作消息 ID。
- 不把 `unified_msg_origin` 当作 Reply 的 `id`。
- 不强制所有平台支持 QQ 表情。
- 不默认开启新模式。
- 不在未完成 QQ 实测前把 metadata 版本直接改为 v5。

## 测试要求

新增或更新测试覆盖：

- 默认配置走旧文字模式。
- 开启开关后，成功接单发送 `Face`。
- 正确从 `message_obj.message_id` 获取消息 ID。
- raw message 字典和对象形式的 message ID fallback。
- 完成消息顺序为 `Reply -> Plain -> Image`。
- 失败、400 内容违规和取消都引用原消息。
- 没有 message ID 时自动降级。
- `Comp.Face` 或 `Comp.Reply` 不存在时插件仍可加载并发送旧模式消息。
- LLM 工具仍返回可用文本给模型。
- 现有全部测试继续通过。

## 当前版本基线

- 当前插件版本：`v4.0.0`
- 实现状态：已完成第一版兼容迁移，仍保持 v4 默认行为。
- 当前实现测试基线：`24 passed`
- 当前提交会在本次实现完成后更新。

## 协议与模型能力扩展目标

V5 不仅增加消息反馈方式，还需要扩展生图协议和模型配置方式，同时保持旧配置可用。

### 支持的协议

当前协议：

- `openai`：OpenAI Images API / OpenAI 兼容生图接口
- `gemini`：Gemini 官方生图接口

新增协议：

- `openai_chat`：OpenAI Chat Completions API，通过 Chat 接口调用支持图片生成或图片输出的模型

`api_type` 的可选值应明确区分为：

```text
openai
openai_chat
gemini
```

OpenAI Chat 协议需要单独的请求适配器，不能直接复用 Images API 的请求格式。返回结果至少要兼容图片 URL、base64 和 data URI；具体响应解析应集中在 `generate.py`，不要散落到 `main.py`。

### 供应商模板中的模型拆分

旧模板只有一个模型列表：

```text
available_models
```

新模板应分为：

```text
generation_models
edit_models
```

含义：

- `generation_models`：文生图模型
- `edit_models`：图生图、改图模型

模型列表不能再把生图能力和改图能力混成一个含义不明确的列表。

### 模型选择规则

最终模型由 `Data` 统一解析，建议提供类似：

```python
provider, model = self.data.resolveModel(has_images)
```

选择规则：

```text
没有参考图
    -> 使用 generation_model

有参考图
    -> edit_models 已配置：使用 edit_model
    -> edit_models 为空：使用 generation_model
```

改图模型为空不代表禁用改图，而是表示：

> 默认认为生图模型同时具备改图能力。

如果某个协议或模型实际不支持改图，应沿用现有失败、退款和错误反馈流程，不在配置层静默切换到其他协议或其他供应商。

### 旧配置迁移

旧配置必须继续工作：

```text
available_models -> generation_models
edit_models      -> 空
```

升级后旧用户应得到以下行为：

- 文生图继续使用原来的模型。
- 改图默认复用原来的模型。
- 不要求用户重新填写模型配置。
- 旧字段只作为兼容读取来源，新模板使用新字段。

### 内部职责边界

- `data.py`：解析配置、保存模型列表、根据是否有参考图选择最终 provider 和 model。
- `generate.py`：实现 `openai`、`gemini`、`openai_chat` 三种协议适配器，并统一输出 `list[bytes]`。
- `main.py`：只负责判断是否有参考图、传递请求和处理任务结果，不判断具体协议请求格式。

建议让 `generate.py` 接收已经解析好的 provider 和 model，避免三种协议适配器各自重复判断“默认模型/改图模型”。

### 协议适配要求

- `openai`：无参考图走生成接口，有参考图走编辑接口。
- `gemini`：保留当前 Gemini 请求方式，并按是否有参考图传递输入图片。
- `openai_chat`：使用 Chat Completions 请求格式，单独处理消息内容、图片输入和图片输出。
- 三种协议最终都转换为图片 bytes 返回给 `main.py`。
- 现有重试、Key 轮换、积分扣除、退款和 400 处理逻辑保持不变。

### 配置字段建议

新供应商模板主要字段：

```text
name
api_type
base_url
api_keys
edit_models
```

字段名称可以在实现阶段根据 AstrBot WebUI 模板规范微调，但语义必须保持“协议、生成模型、改图模型”三者分离。

### 扩展测试要求

除富反馈测试外，还需要覆盖：

- `openai`、`gemini`、`openai_chat` 三种协议都能被识别。
- 旧 `available_models` 配置自动迁移为生成模型列表。
- 改图模型为空时回退到生成模型。
- 改图模型存在时，有参考图使用改图模型。
- 无参考图始终使用生成模型。
- 三种协议的返回 URL、base64、data URI 都能统一为图片 bytes。
- 协议或模型改图失败时沿用现有退款/错误处理。
- 多供应商、多 Key 轮换和现有 17 个测试继续通过。
