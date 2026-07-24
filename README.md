# AstrBot 超级生图插件 4.0.0

群聊生图插件。用户发 `/生图`，插件自动判断文生图还是图生图，并可从消息、回复及合并聊天记录中提取参考图。Bot 也能通过 LLM 工具自动生图。

## 快速开始

1. 放进 AstrBot 插件目录，安装 `requirements.txt`
2. 在 WebUI 填写至少一个 API 供应商的 Key 和模型
3. 重启 AstrBot，群里发 `/生图 一只猫` 验证

## 命令

| 命令                   | 说明                 | 示例                                 |
| ---------------------- | -------------------- | ------------------------------------ |
| `/生图 描述`           | 生成图片             | `/生图 一只猫坐在窗边看雨`           |
| `/生图取消`            | 取消自己最近一个任务 | `/生图取消`                          |
| `/生图积分`            | 查看积分             | `/生图积分`                          |
| `/生图预设`            | 查看/添加/删除预设   | `/生图预设 添加 手办化:变成手办风格` |
| `/生图模型`            | 查看或切换模型       | `/生图模型 2`                        |
| `/生图开关`            | 切换总开关           | `/生图开关`                          |
| `/生图改分 @用户 数量` | 管理员加分           | `/生图改分 @张三 50`                 |

所有命令处理后调用 `event.stop_event()`，不会再交给 LLM。

## LLM 工具

| 工具              | 作用                                                                                    |
| ----------------- | --------------------------------------------------------------------------------------- |
| `super_draw`      | Bot 生图（prompt 必填，urls 可选）                                                      |
| `super_draw_data` | Bot 查改积分（action: summary/my\_points/user\_points/change\_points/set\_points/rank） |
| `super_draw_ban`  | Bot 管黑名单（action: list/add/remove）                                                 |

工具生图成功后可自动追加评价（WebUI 可配置）。

## 架构

```
用户 /生图 猫  ──→  main.py cmd_draw()  ──→  _draw()  ──→  _run()  ──→  generate.py  ──→  发图
Bot 调 super_draw  ──→  main.py tool_draw()  ──→  _draw()  ──→  _run()  ──→  generate.py  ──→  发图 + 评价
```

**命令和工具共享同一个** **`_draw()`** **函数**，不存在两套生图逻辑。

### 文件职责

```
main.py       入口。命令 + 工具 + 生图任务 + 发送。~220 行。
              关键：_draw() 通用生图、_run() 后台执行、_images() 收集参考图。

data.py       数据。配置 + 积分 + 预设 + 黑名单 + 模型。~230 行。
              关键：check/spend/refund/give 积分四件套，preset/model/ban 各一个入口方法。

generate.py   API 调用。给提示词和图片，返回 bytes。不认识 AstrBot。~320 行。
              兼容 b64_json、HTTP URL、data URI 三种图片返回格式。

tool/file.py      保存图片到临时文件。
tool/picture.py   检测图片格式，动态图转静态。
```

### 数据流

```
WebUI 配置  →  data.py 读取  →  各字段
用户消息    →  main.py 命令/工具  →  data.py 检查积分  →  generate.py 调 API  →  图片 bytes  →  发送
```

积分按用户 QQ 号全局存储（不区分群），文件在 `{dataDir}/points.json`。
