# AstrBot 超级生图插件 3.0.0

超级生图是给 AstrBot 使用的群聊生图插件。它现在只坚持一个交互原则：用户只需要说“生图”，剩下交给插件和生图模型判断。

## 第一原则

现在的生图模型已经能理解自然语言需求，所以插件不再要求用户学习参数、不再区分文生图和图生图、不再让用户复制任务 ID。

```text
/生图 一只猫坐在窗边看雨，画成手机壁纸
/生图 参考这张图，做成水彩头像
/生图 画一张 16:9 电影海报，高清，两张候选图
/取消生图
/生图积分
/积分排行
/生图预设
/生图模型 2
/生图开关
```

## 核心能力

- 统一入口：默认可用 `/生图`、`/画图`、`/生成`，也可以在 WebUI 改成任何顺口命令。
- 自动判断：消息里有图片、回复里有图片、转发里有图片、文本里有图片链接，就自动作为参考图。
- 自然语言：比例、质量、数量、用途、风格都直接写在提示词里。
- 积分制：群友正常发言赚积分，生图消耗积分，400 内容安全类错误按配置扣分。
- 强自定义：所有命令别名、积分规则、提示词优化模型、提示词优化模板、工具生图后评价都能在 WebUI 配置。
- Bot 操作能力：LLM 可调用工具生图、查状态、查积分、改积分、看排行、看预设、调用 AstrBot 模型优化提示词。
- 多接口：支持 OpenAI 兼容接口和 Gemini 官方接口。
- 多 Key：支持按顺序轮换或随机选择 Key。
- 队列控制：限制并发和最大队列长度，避免接口被刷爆。
- 预设提示词：常用效果可以用编号快速套用。

## 安装

- AstrBot `>= 4.20.1`
- Python `>= 3.10`

把本插件目录放进 AstrBot 插件目录，安装 [`requirements.txt`](requirements.txt)，然后在 AstrBot 插件配置面板填写 API Key 并重启。

## 用户命令

```text
/生图 一只白猫坐在窗边看雨，画成手机壁纸
/生图 参考这张图，做成水彩头像
/生图 画一张电影海报，16:9，高清，两张候选图
/取消生图
/生图积分
/积分排行
/生图预设
/生图模型 2
/生图开关
/生图队列
/生图帮助
/提示词优化 猫咪头像
```

`/画图` 和 `/生成` 是 `/生图` 的默认别名。管理员可以在 WebUI 的 `commands.draw` 里继续添加“画画”“出图”“生成图片”等词。

## 图生图

不需要 `/改图`。所有场景都用 `/生图`：

```text
/生图 参考这张图，改成水彩头像
/生图 保持人物特征，换成赛博朋克风格
/生图 把这张图做成聊天表情包
```

插件会自动从当前消息、回复消息、合并转发、图片 URL、`@用户` 头像中收集参考图。

## 取消任务

不需要复制任务 ID。直接发送：

```text
/取消生图
```

插件会自动取消你自己最近一个还没完成的生图任务，并退回已扣积分。

## 预设

```text
/生图预设
/生图 1号 橘猫
/生图 2号 我的头像
/生图 3号 表情包
```

管理预设仍使用：

```text
/预设 查看 手办化
/预设 添加 水彩:柔和透明水彩风格，纸张纹理，暖色调
/预设 删除 水彩
```

## 积分制

```text
/生图积分
/积分排行
```

默认规则：

- 新用户第一次出现会获得 `10` 初始积分。
- 每次有效群聊发言获得 `1` 积分。
- 同一用户默认 `30` 秒内只记一次发言积分，防止刷屏刷分。
- 每次生图请求默认消耗 `5` 积分。
- 生图取消、普通接口错误、网络错误时，已扣积分会自动退回。
- 接口返回 `400` 时，通常表示提示词或内容安全限制，会按 WebUI 配置扣除 `400错误扣除积分`，余额最低为 `0`。

## 提示词优化

```text
/提示词优化 猫咪头像
/优化提示词 电影海报，雨夜，霓虹灯
```

提示词优化不会直接生图。插件会先把短描述套进 WebUI 的 `prompt_optimize.optimize_template`，再调用 `prompt_optimize.optimize_provider_id` 指定的 AstrBot 聊天模型输出最终提示词；模型 ID 留空时使用当前会话模型。模板里用 `{prompt}` 表示用户原始描述。

## LLM 工具生图后评价

当 Bot 自己通过 `super_draw` 工具发起生图时，图片发送完成后会自动追加一句自然评价。这个评价会读取当前会话上下文、用户原始需求、生成图片数量和发送路径，再调用 `prompt_optimize.tool_commentary_provider_id` 指定的 AstrBot 聊天模型生成。

这个功能只对 LLM 工具生图生效，用户直接发送 `/生图` 不会触发，避免普通命令使用时 Bot 额外刷屏。

## LLM 工具

| 工具名                       | 作用                         |
| ---------------------------- | ---------------------------- |
| `super_draw`                 | 让 Bot 自动发起生图任务      |
| `super_draw_data`            | 让 Bot 查询/修改插件数据积分 |
| `super_draw_optimize_prompt` | 让 Bot 优化生图提示词        |

`super_draw` 参数：

| 参数     | 类型   | 说明                                                     |
| -------- | ------ | -------------------------------------------------------- |
| `prompt` | string | 必填，直接用自然语言描述图片、比例、质量、数量和修改要求 |
| `urls`   | string | 可选，参考图 URL，多个用英文逗号分隔                     |

`super_draw_data` 支持的 `action`：`summary`、`status`、`my_points`、`user_points`、`change_points`、`rank`、`presets`。其中 `change_points` 会使用 `user_key`、`delta`、`reason` 修改积分，扣分最低扣到 `0`。

## 配置说明

主要配置都在 [`_conf_schema.json`](_conf_schema.json)。常用项如下：

| 配置                                          | 说明                                                                       |
| --------------------------------------------- | -------------------------------------------------------------------------- |
| `enabled`                                     | 插件总开关                                                                 |
| `enable_llm_tool`                             | 是否允许 LLM 自动调用工具                                                  |
| `api_providers`                               | API 供应商、Key、模型列表                                                  |
| `generation.model`                            | 当前模型，格式为 `供应商/模型`                                             |
| `generation.key_mode`                         | 多 Key 使用方式：`round_robin` 或 `random`                                 |
| `generation.max_concurrent_tasks`             | 同时真正调用接口的任务数                                                   |
| `generation.max_queue_size`                   | 最大排队任务数                                                             |
| `generation.max_reference_images`             | 单次最多参考图数量                                                         |
| `generation.prompt_prefix`                    | 每次生图自动追加的提示词前缀                                               |
| `generation.negative_prompt`                  | 每次生图自动追加的反向提示词                                               |
| `points.enable_points`                        | 是否启用积分制                                                             |
| `points.points_per_message`                   | 每次有效发言获得多少积分                                                   |
| `points.message_point_cooldown_seconds`       | 同一用户发言加分冷却秒数                                                   |
| `points.draw_cost_per_image`                  | 每次生图请求消耗多少积分                                                   |
| `points.bad_request_penalty_points`           | 接口返回 400 时扣除多少积分，最低扣到 0                                    |
| `points.new_user_points`                      | 新用户初始积分                                                             |
| `commands.draw`                               | 生图命令别名，例如 生图、画图、生成；命令前缀使用 AstrBot 全局标准命令系统 |
| `commands.points`                             | 查询个人积分的命令别名                                                     |
| `commands.optimize`                           | 提示词优化命令别名                                                         |
| `data_tools.enable_data_tools`                | 是否允许 Bot 调用数据工具                                                  |
| `prompt_optimize.enable_prompt_optimize`      | 是否启用提示词优化                                                         |
| `prompt_optimize.optimize_provider_id`        | 提示词优化使用的 AstrBot 聊天模型 ID                                       |
| `prompt_optimize.optimize_template`           | 提示词优化模板，使用 `{prompt}` 放原文                                     |
| `prompt_optimize.enable_tool_commentary`      | 是否启用 LLM 工具生图后的自然评价                                          |
| `prompt_optimize.tool_commentary_provider_id` | 生图后评价使用的 AstrBot 聊天模型 ID                                       |
| `prompt_optimize.tool_commentary_template`    | 生图后评价模板，可读取上下文和图片信息                                     |
| `prompt_optimize.tool_commentary_max_length`  | 生图后评价最大字数                                                         |

## HOP 架构说明

```text
用户 /生图 或 WebUI 自定义命令
  → AstrBot 标准命令系统或 main.py 自定义别名路由直接触发，不交给 LLM 判断
  → main.py 停止事件继续传播，避免命令被后续 LLM 对话再次理解成工具调用
  → main.py 自动收集参考图
  → data.py 检查积分、预扣积分、拼接预设，必要时按 400 规则结算积分
  → generate.py 调用 OpenAI/Gemini 生图接口
  → main.py 把图片发回聊天

Bot 调用 super_draw 工具
  → main.py 标记这是 LLM 工具生图，并启动同一套生图任务
  → generate.py 返回图片后，main.py 先发图
  → main.py 读取会话上下文和图片发送信息
  → AstrBot 聊天模型生成自然评价并追发到聊天

Bot 调用数据工具
  → main.py 的 super_draw_data 接收 action
  → data.py 查询或修改积分、状态、预设摘要
  → main.py 把结果反馈给 Bot
```

## 项目结构

```text
main.py               AstrBot 入口，接收命令和 LLM 工具调用，编排完整生图任务
data.py               数据中心，管理配置、模型、预设、积分和用量记录
generate.py           生图指令，调用 OpenAI 兼容接口或 Gemini 官方接口
tool/file.py          文件工具，保存必要的临时发送图片
tool/picture.py       图片工具，识别图片格式并把动态图转成静态图
_conf_schema.json     AstrBot 配置面板结构
metadata.yaml         插件市场元信息和版本号
```

## 排查问题

1. 发送 `/生图开关`，确认插件可以开启或关闭。
2. 发送 `/生图模型`，确认至少有一个模型。
3. 发送 `/生图积分`，确认积分系统可用。
4. 检查配置里的 `api_keys` 是否为空。
5. 如果图片参考失败，开启 `debug_mode` 查看日志。
6. 如果 Gemini 报依赖缺失，确认已安装 `google-genai>=1.33.0`。
