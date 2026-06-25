# AstrBot 超级生图插件 4.0.0

群聊生图插件。用户只需要说 `/生图`，剩下交给插件和模型。

## 命令

```text
/生图 一只猫坐在窗边看雨
/生图 参考这张图，做成水彩头像
/取消生图
/生图积分
/生图预设
/生图模型 2
/生图开关
```

## 生图

有图片就是图生图，没图片就是文生图。插件会自动从当前消息、回复消息、@用户头像、文本 URL 中收集参考图。

```text
/生图 把这张图做成表情包
/生图 保持人物特征，换成赛博朋克风格
```

## 取消任务

不需要任务 ID，直接发：

```text
/取消生图
```

## 预设

```text
/生图预设
/生图预设 添加 手办化:把参考主体变成手办风格
/生图预设 删除 手办化
/生图预设 查看 手办化
/生图 1号 一只猫
```

## 积分制

- 新用户获得初始积分
- 群聊发言赚积分
- 生图消耗积分
- 400 错误按配置扣积分
- 取消任务和普通失败退回积分

## LLM 工具

| 工具名            | 作用             |
| ----------------- | ---------------- |
| `super_draw`      | Bot 自动生图     |
| `super_draw_data` | Bot 查改积分数据 |
| `super_draw_ban`  | Bot 管理黑名单   |

### super_draw

| 参数     | 类型   | 说明                       |
| -------- | ------ | -------------------------- |
| `prompt` | string | 必填，图片描述             |
| `urls`   | string | 可选，参考图 URL，逗号分隔 |

### super_draw_data

| action          | 说明                           |
| --------------- | ------------------------------ |
| `summary`       | 插件概要                       |
| `my_points`     | 当前用户积分                   |
| `user_points`   | 指定用户积分                   |
| `change_points` | 修改积分（需 delta 和 reason） |
| `rank`          | 积分排行                       |

### super_draw_ban

| action   | 说明                           |
| -------- | ------------------------------ |
| `list`   | 查看黑名单                     |
| `add`    | 添加用户到黑名单（需 user_id） |
| `remove` | 移除用户出黑名单（需 user_id） |

## 生图后评价

仅在 Bot 通过 `super_draw` 工具生图成功后触发。Bot 会结合当前会话上下文和图片信息，自然追加一句评价。用户直接 `/生图` 不触发评价。

## 配置

| 配置                                | 说明                  |
| ----------------------------------- | --------------------- |
| `enabled`                           | 总开关                |
| `enable_llm_tool`                   | 是否允许 LLM 调用工具 |
| `ban_list`                          | 黑名单用户 ID 列表    |
| `api_providers`                     | API 供应商、Key、模型 |
| `generation.model`                  | 当前模型              |
| `generation.max_retry_attempts`     | 最大重试次数          |
| `generation.timeout`                | 超时时间              |
| `generation.max_queue_size`         | 最大队列长度          |
| `points.enable_points`              | 启用积分制            |
| `points.draw_cost_per_image`        | 每次生图消耗积分      |
| `points.bad_request_penalty_points` | 400 错误扣除积分      |
| `points.new_user_points`            | 新用户初始积分        |
| `points.enable_data_tools`          | 允许 Bot 数据工具     |
| `commentary.enable_commentary`      | 启用工具生图后评价    |
| `commentary.commentary_provider_id` | 评价模型 ID           |
| `commentary.commentary_template`    | 评价模板              |
| `presets`                           | 预设提示词            |

## 项目结构

```text
main.py               入口，命令和 LLM 工具
data.py               数据中心，配置、积分、预设、黑名单
generate.py           生图接口，OpenAI/Gemini
tool/file.py          图片保存
tool/picture.py       图片格式处理
_conf_schema.json     WebUI 配置面板
metadata.yaml         插件元信息
```

## 安装

- AstrBot `>= 4.20.1`
- Python `>= 3.10`

把插件放进 AstrBot 插件目录，安装 `requirements.txt`，在 WebUI 填写 API Key 并重启。
