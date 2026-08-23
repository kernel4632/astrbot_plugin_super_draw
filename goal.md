# 超级生图插件重构目标

## 总目标

对插件做一次破坏性重构，让代码优先服务于“以后快速修改”，而不是继续堆积旧版兼容逻辑。

必须遵循：

- KISS：能直接写清楚，就不增加抽象。
- 奥克姆剃刀：删除没有实际用途的分支、配置和兼容层。
- 所属关系明确：复杂名称通过对象或模块表达，方法本身只使用一个简单单词。
- 局部可读：主流程读起来像业务步骤，不需要跳过多层辅助函数才能理解。
- 一个事实来源：配置、README、测试和代码不能互相矛盾。

本次重构允许破坏旧配置和旧内部 API。旧用户需要按照新配置重新填写模型。

## 业务规则

保留这些实际功能：

- `/生图`：文生图；消息中有参考图时改图。
- `/生图取消`：取消当前用户最近的任务。
- `/生图积分`：查询积分。
- `/生图预设`：管理提示词预设。
- `/生图模型`：管理员查看和切换模型。
- `/生图开关`：管理员开关插件。
- `/生图改分`：管理员修改积分。
- `super_draw`：LLM 调用生图。
- `super_draw_data`：LLM 查询或修改积分。
- `super_draw_ban`：LLM 管理黑名单。
- OpenAI Images、OpenAI Chat、Gemini 三种协议。
- 任务完成、失败、取消时可引用原始消息回复。

删除这些功能或概念：

- QQ `Face` 接单表情。
- 长期图片缓存。
- 缓存命中和图片去重。
- 定时缓存清理任务。
- 独立的文生图模型槽位。
- 独立的改图模型槽位。
- 为未知中转服务猜测请求格式。
- 为旧版本保留的多层运行时兼容分支。

## 核心数据流

```text
用户消息 / LLM 工具
        │
        ▼
app.draw()
        │
        ├── images.collect()
        ├── points.check()
        ├── points.spend()
        └── jobs.start()
                │
                ▼
             provider.draw()
                │
                ├── provider.openai()
                ├── provider.chat()
                └── provider.gemini()
                │
                ▼
             files.save()
                │
                ▼
             reply.success()
                │
                ▼
             files.remove()
```

失败数据流：

```text
provider.draw()
        │
        ▼
ProviderFailure
        │
        ├── kind = policy
        │       └── points.penalize()
        │
        └── 其他错误
                └── points.refund()
        │
        ▼
reply.failure()
```

## 目标模块

最终结构保持简单，不建立万能基类和复杂继承树：

```text
app.py
settings.py
points.py
images.py
providers.py
jobs.py
files.py
reply.py
main.py
```

### `main.py`

只保留 AstrBot 边界：

- 命令装饰器。
- LLM 工具装饰器。
- 从事件取出最少的数据。
- 调用 `app`。
- 把结果交给 `reply`。
- 启动和关闭插件。

`main.py` 不负责：

- 拼装 OpenAI、Gemini 请求。
- 解析协议响应。
- 读写积分文件。
- 下载图片。
- 判断 HTTP 400 是否违规。
- 管理临时文件清理。

### `app.py`

负责一条完整的生图业务流程。它是业务编排层，不负责具体实现细节。

推荐调用方式：

```python
await app.draw(request)
```

它只按顺序调用：

```text
images.collect()
points.check()
points.spend()
jobs.start()
provider.draw()
files.save()
reply.success()
files.remove()
```

### `settings.py`

只负责读取和校验配置，输出明确的数据对象。

每个配置项只代表一个供应商和一个模型：

```text
Provider:
    name
    protocol
    base_url
    api_keys
    model
    timeout
    retries
```

配置只保留：

```text
available_models
```

不再读取或生成：

```text
generation_models
generationModel
editModel
```

是否有参考图，只决定调用生成接口还是编辑接口，不决定切换模型：

```text
无参考图 -> /images/generations
有参考图 -> /images/edits
```

### `points.py`

只负责积分账本和积分文件：

```text
points.check()
points.spend()
points.refund()
points.penalize()
points.give()
points.set()
points.rank()
points.talk()
```

积分结算规则：

```text
成功       -> 保留预扣积分
取消       -> 全额退款
超时       -> 全额退款
网络错误   -> 全额退款
参数错误   -> 全额退款
模型不支持 -> 全额退款
明确违规   -> 只扣违规罚分
```

绝对不能再使用“所有 HTTP 400 都是违规”的规则。

协议层必须返回结构化失败：

```text
ProviderFailure:
    kind: policy | request | network | timeout | unavailable
    status: int | None
    code: str | None
    message: str
```

只有 `kind = policy` 才能调用 `points.penalize()`。

### `images.py`

只负责参考图：

```text
images.collect()
images.download()
images.validate()
```

规则：

- 从 AstrBot 标准图片组件取图片来源。
- 需要发送给模型时统一转换为图片 bytes。
- 限制图片数量、大小和格式。
- 外部地址只允许明确允许的 HTTPS 地址。
- 不把任意字符串当作本地路径读取。
- 不允许请求 localhost、私有地址或明显的内网地址。
- 不在这个模块处理模型请求。

### `providers.py`

只负责协议调用和结果转换，输出统一的 `list[bytes]`。

不创建 `BaseProvider`、工厂树或多层适配器。只保留一个明确分发：

```python
provider.draw(request)
```

内部按协议调用三个简单方法：

```text
provider.openai()
provider.chat()
provider.gemini()
```

协议规则：

```text
openai
    无参考图 -> Images Generate
    有参考图 -> Images Edit，multipart/form-data

openai_chat
    -> Chat Completions
    -> 使用该协议明确支持的图片输入和输出格式

gemini
    -> Generate Content
    -> 使用 inline_data 读取图片
```

不要递归扫描任意响应对象，不要从任意文本中猜 URL，不要为未知服务自动切换格式。

不符合标准协议的中转服务视为配置或服务错误，直接返回错误并退款。

### `jobs.py`

只负责任务生命周期：

```text
jobs.start()
jobs.cancel()
jobs.active()
jobs.clean()
```

当前代码的 `max_queue_size` 实际是并发上限，不是真正的排队队列。重构时改成：

```text
max_active_jobs
```

不实现真正的等待队列，除非之后有明确需求。

任务对象只保存必要信息：

```text
DrawRequest:
    user_id
    origin
    message_id
    prompt
    images
    from_tool

DrawJob:
    id
    request
    reserved_points
    task
```

不长期持有完整 AstrBot event。LLM 评价需要的信息单独保存，不把框架对象塞进通用任务数据。

### `files.py`

只处理一次发送所需的临时文件：

```text
files.save()
files.remove()
```

流程必须是：

```text
图片 bytes
    -> 临时文件
    -> 发送
    -> 删除
```

删除：

- 固定 `cache` 目录。
- `max_cache_files`。
- `cleanup_interval_hours`。
- `_cacheLoop()`。
- 图片 MD5 去重命名。
- 定时清理任务。

如果 AstrBot 的发送方法异步返回且能确认上传已完成，发送完成后立即删除。若发送方法只入队，则使用单次短延迟删除，不建立长期缓存系统。

### `reply.py`

只负责把业务结果发送给聊天平台：

```text
reply.start()
reply.success()
reply.failure()
reply.cancel()
```

完成、失败和取消都可以引用原消息：

```text
Reply(source_message_id)
Plain(status_text)
Image(path)
```

不再发送 `Face`。

引用不可用时退回普通文字或图片消息。这个降级只放在 `reply.py`，不散落到任务和协议代码中。

## 配置重构

新的供应商配置只保留一个模型列表：

```json
{
  "name": "OpenAI",
  "api_type": "openai",
  "base_url": "https://api.openai.com",
  "api_keys": ["..."],
  "available_models": ["gpt-image-1"]
}
```

删除：

```text
generation_models
edit_models
task_face_id
max_cache_files
cleanup_interval_hours
```

配置迁移采用破坏性方式：

- 不在运行时读取旧字段。
- 不自动把旧字段转换成新字段。
- README 明确说明升级后需要重新填写配置。
- schema、代码、测试和文档只描述新配置。

## 删除的旧复杂度

重构时必须搜索并删除以下内容：

- `_callOpenAiJsonEdit` 这种与实际协议不符的命名。
- 所有 JSON 改图尝试。
- `Face`、`task_face_id` 和接单表情逻辑。
- `generation_models`、`edit_models` 和对应 model role。
- `_is400()` 对所有 400 的宽泛判断。
- 递归猜测协议响应图片的通用解析器。
- 多层 `ContextWrapper` 猜测。
- MessageChain 多版本探测回退，除非当前支持版本确实需要。
- OneBot 多种参数名反复尝试，除非官方接口明确要求。
- 未使用依赖。
- 过期 README、测试、版本目标和注释。

## 命名规则

优先使用所属对象表达含义：

```text
settings.read()
images.collect()
images.download()
points.refund()
provider.openai()
jobs.cancel()
files.remove()
reply.failure()
```

避免：

```text
_prepareOpenAiImageDataUris()
_collectImagesRecursive()
_sendReplyStatus()
_callOpenAiJsonEdit()
```

一个单词不足以表达含义时，优先增加所属对象，不要把多个含义拼进方法名。

不要为了满足命名规则创建空壳对象。封装必须有真实职责，且至少满足以下一个条件：

- 拥有自己的状态。
- 被多个流程使用。
- 可以独立测试。
- 可以被单独替换。
- 能明显缩短主流程。

## 重构步骤

1. 修改 `goal.md` 后冻结新业务规则，不再继续增加旧兼容要求。
2. 先补积分结算测试，确保普通 400 全额退款，只有明确 policy 才扣罚。
3. 新建 `ProviderFailure` 和统一 provider 返回结果。
4. 拆出 `settings.py`，删除独立模型槽位和旧配置读取。
5. 拆出 `points.py`，让积分结算脱离 `main.py`。
6. 拆出 `images.py`，集中图片提取、校验和下载。
7. 拆出 `providers.py`，用三个明确协议函数替代混杂分支。
8. 拆出 `jobs.py`，把任务生命周期从 AstrBot 入口移走。
9. 删除长期缓存，改为一次发送对应一次临时文件。
10. 拆出 `reply.py`，集中引用回复和普通消息降级。
11. 将 `main.py` 收缩为命令、工具和业务编排入口。
12. 同步 README、schema、测试和版本说明。
13. 运行语法检查、配置 JSON 检查、单元测试和三种协议的手工冒烟测试。

## 验收标准

架构：

- `main.py` 不再包含协议请求、积分账本、图片下载和文件清理。
- 每个模块有一个清晰职责。
- 主流程能按业务顺序阅读。
- 方法名主要是单个简单单词，所属关系由对象或模块表达。
- 没有为旧版本保留的无效分支。
- 没有长期缓存系统。

业务：

- 无参考图使用 Images Generate。
- 有参考图使用标准 Images Edit multipart 请求。
- 三种协议都能返回图片 bytes。
- 成功保留积分。
- 取消、超时、网络错误、参数错误、模型不支持时全额退款。
- 只有明确内容安全拦截才扣违规罚分。
- 完成、失败、取消可以引用原消息。
- 引用失败时只在 `reply.py` 中降级。

测试：

- 普通 400 不扣罚分。
- policy 错误才扣罚分。
- 取消任务会退款。
- 并发上限有效。
- OpenAI 文生图请求正确。
- OpenAI 改图请求使用 multipart 文件。
- OpenAI Chat 图片输入输出正确。
- Gemini 图片输入输出正确。
- 图片临时文件发送后被删除。
- 非法外部地址被拒绝。
- 配置 schema 是有效 JSON。

## 当前状态

```text
状态：待开始大幅重构
基线：当前提交 874871e
策略：允许破坏性配置变更
重点：快速修改、低耦合、少分支、清晰所属关系
```
