# AstrBot 超级生图插件 5.1.0

群聊生图插件。发送 `/生图 一只猫坐在窗边看雨` 即可开始。消息、回复和合并聊天记录中的图片会作为参考图。Bot 也能调用 `super_draw` 工具生图。

支持 OpenAI Images、OpenAI Chat Completions 和 Gemini 三种协议。每个供应商只配置一个 `available_models` 列表，文生图和带参考图生图都使用当前选择的模型。

## 快速开始

1. 放进 AstrBot 插件目录并安装 `requirements.txt`
2. 在 WebUI 的 `api_providers` 填写 Key 和 `available_models`
3. 重启 AstrBot，发送 `/生图 一只猫` 验证

## 命令

| 命令 | 说明 |
| --- | --- |
| `/生图 描述` | 生成图片，带图片时自动作为参考图 |
| `/生图取消` | 取消自己最近的任务 |
| `/生图积分` | 查看积分 |
| `/生图预设` | 查看、添加、删除预设 |
| `/生图模型` | 管理员查看或切换模型 |
| `/生图开关` | 管理员切换总开关 |
| `/生图改分 @用户 数量` | 管理员加减积分 |

`rich_task_feedback` 开启后，任务完成、失败和取消会引用原始消息。平台不支持引用时自动发送普通消息。

## 积分和失败

开始任务时会预扣积分。API 普通 400、参数错误、网络错误、超时和取消都会自动退款。只有 API 明确返回内容安全或策略拒绝时，才按 `bad_request_penalty_points` 扣分。

生成图片只写入系统临时目录。发送结束后立即删除，不保存长期图片缓存。

## 文件结构

```
main.py                 机器人收到消息后，从这里进入插件

draw/flow.py            开始、执行和取消生图的完整顺序
draw/picture.py         消息、引用和合并转发中的参考图片
draw/model.py           OpenAI Images、OpenAI Chat、Gemini 请求
draw/task.py            正在运行的生图任务和并发数量
draw/send.py            发送成功、失败和取消消息
draw/temp.py            发送前保存、发送后删除的临时图片

user/point.py           用户积分、退款和发言加分
user/preset.py          提示词预设的查看、添加和删除
user/ban.py             生图黑名单的查看、添加和删除

setting/config.py       WebUI 配置和当前模型
```

积分按用户 ID 全局保存，文件在 AstrBot 插件数据目录的 `points.json`。

找文件时只需要按实际要改的功能判断：引用图片问题看 `draw/picture.py`，模型请求问题看 `draw/model.py`，积分问题看 `user/point.py`，配置问题看 `setting/config.py`。
