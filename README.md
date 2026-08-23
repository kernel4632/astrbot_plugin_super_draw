# AstrBot 超级生图插件 5.0.0

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

## 模块

```
main.py      AstrBot 命令、工具和事件转换
app.py       生图流程、积分结算、任务和评价
settings.py  WebUI 配置与当前模型
points.py    积分 JSON 数据
images.py    参考图提取和下载
providers.py OpenAI Images、OpenAI Chat、Gemini 请求
jobs.py      同时运行任务限制
files.py     一次性临时图片
reply.py     引用回复与普通消息回退
```

积分按用户 ID 全局保存，文件在 AstrBot 插件数据目录的 `points.json`。
