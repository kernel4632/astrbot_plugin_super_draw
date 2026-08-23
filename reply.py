"""任务状态消息，引用发送失败时退回普通消息。"""

from __future__ import annotations

import inspect
from typing import Any, Iterable

try:
    import astrbot.api.message_components as Comp
    from astrbot.api.event import MessageChain
except ImportError:  # 允许独立导入模块
    Comp = None
    MessageChain = None


class Reply:
    async def start(self, context: Any, umo: str, text: str, message_id: str = "") -> Any:
        return await self.send(context, umo, text, message_id)

    async def success(self, context: Any, umo: str, text: str, paths: Iterable[str] = (), message_id: str = "") -> Any:
        return await self.send(context, umo, text, message_id, paths)

    async def failure(self, context: Any, umo: str, text: str, message_id: str = "") -> Any:
        return await self.send(context, umo, text, message_id)

    async def cancel(self, context: Any, umo: str, text: str, message_id: str = "") -> Any:
        return await self.send(context, umo, text, message_id)

    async def send(
        self,
        context: Any,
        umo: str,
        text: str,
        message_id: str = "",
        paths: Iterable[str] = (),
    ) -> Any:
        paths = tuple(paths)
        if message_id:
            try:
                result = context.send_message(umo, self.chain(text, paths, message_id))
                return await result if inspect.isawaitable(result) else result
            except Exception:
                pass
        result = context.send_message(umo, self.chain(text, paths))
        return await result if inspect.isawaitable(result) else result

    def chain(self, text: str, paths: Iterable[str], message_id: str = "") -> Any:
        components = []
        if message_id:
            components.append(Comp.Reply(id=str(message_id)))
        components.append(Comp.Plain(text))
        components.extend(Comp.Image.fromFileSystem(path) for path in paths)
        try:
            return MessageChain(chain=components)
        except TypeError:
            chain = MessageChain()
            chain.chain.extend(components)
            return chain


reply = Reply()

__all__ = ["reply"]
