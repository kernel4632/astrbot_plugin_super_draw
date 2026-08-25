"""提示词预设的匹配、查看、添加和删除。"""

from typing import Any, Callable


class Preset:
    def resolve(self, values: dict[str, str], text: str) -> tuple[str, str | None]:
        text = text.strip()
        for index, (name, prompt) in enumerate(values.items(), 1):
            marker = f"{index}号"
            if text.startswith(marker):
                return f"{prompt} {text[len(marker):].strip()}".strip(), name
            if text.startswith(name):
                return f"{prompt} {text[len(name):].strip()}".strip(), name
        return text, None

    def change(self, values: dict[str, str], argument: str, save: Callable[[], Any]) -> str:
        argument = argument.strip()
        if not argument:
            if not values:
                return "暂无预设。用 /生图预设 添加 名称:内容 创建。"
            lines = ["预设列表："]
            for index, (name, prompt) in enumerate(values.items(), 1):
                suffix = "…" if len(prompt) > 30 else ""
                lines.append(f"  {index}号 {name}：{prompt[:30]}{suffix}")
            return "\n".join(lines)
        if argument.startswith("添加 "):
            value = argument[3:].strip()
            if ":" not in value:
                return "格式：名称:提示词内容"
            name, prompt = (part.strip() for part in value.split(":", 1))
            if not name or not prompt:
                return "名称和内容都不能为空。"
            values[name] = prompt
            save()
            return f"预设已添加：{name}"
        if argument.startswith("删除 "):
            name = argument[3:].strip()
            if name not in values:
                return f"预设不存在：{name}"
            del values[name]
            save()
            return f"预设已删除：{name}"
        if argument.startswith("查看 "):
            name = argument[3:].strip()
            return f"{name}：{values[name]}" if name in values else f"预设不存在：{name}"
        return "格式：/生图预设、/生图预设 添加 名称:内容、/生图预设 删除 名称、/生图预设 查看 名称"


preset = Preset()
