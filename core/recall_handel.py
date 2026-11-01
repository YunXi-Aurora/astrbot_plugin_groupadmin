import asyncio

from astrbot.core.message.components import At, Reply
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ..config import PluginConfig
from ..data import QQAdminDB
from ..permission import PermLevel
from ..utils import get_ats, get_nickname, get_replyer_id


class RecallHandle:
    def __init__(self, config: PluginConfig, db: QQAdminDB, perm_manager=None):
        self.cfg = config
        self.db = db
        self.perm = perm_manager

    async def _send_recall_notice(self, event: AiocqhttpMessageEvent):
        """撤回后向群聊通报发起者"""
        sender_id = event.get_sender_id()
        nickname = await get_nickname(event, sender_id)
        await event.send(
            event.plain_result(f"本次撤回由[{nickname}][{sender_id}]发起")
        )

    async def _recall_fail_reason(
        self, event: AiocqhttpMessageEvent, target_id: str | int | None = None
    ) -> str:
        """撤回失败时给出具体原因文案"""
        # 1. bot 自身不是群管/群主
        if self.perm is not None:
            bot_level = await self.perm.get_bot_level(event)
            if bot_level != PermLevel.BOT_OWNER and bot_level != PermLevel.BOT_ADMIN:
                return "我没有管理员权限"
            # 2. 被撤回消息的发送者是群管/群主
            if target_id is not None:
                try:
                    info = await event.bot.get_group_member_info(
                        group_id=int(event.get_group_id()),
                        user_id=int(target_id),
                        no_cache=True,
                    )
                    role = info.get("role", "unknown")
                    if role == "owner":
                        return "我动不了群主"
                    if role == "admin" and bot_level == PermLevel.BOT_ADMIN:
                        return "我动不了管理员"
                except (TypeError, ValueError, Exception):
                    pass
            # 3. 请求发起者无权限（走到这里说明 bot 有权但操作仍失败）
            if not await self.perm.is_user_authorized_admin(event, event.get_sender_id()):
                return "你无权让我这么做"
        return "撤回失败：未知原因"

    async def delete_msgs_by_ids(
        self, event: AiocqhttpMessageEvent, message_ids: list[int]
    ) -> int:
        """按 ID 列表撤回消息，返回实际撤回条数"""
        client = event.bot
        delete_count = 0
        sem = asyncio.Semaphore(10)

        async def try_delete(mid: int):
            nonlocal delete_count
            async with sem:
                try:
                    await client.delete_msg(message_id=mid)
                    delete_count += 1
                except Exception:
                    pass

        await asyncio.gather(*(try_delete(mid) for mid in message_ids))
        return delete_count

    async def get_recent_messages(
        self,
        event: AiocqhttpMessageEvent,
        count: int,
        user_id: str = "",
    ) -> list[dict]:
        """获取最近的群消息（时间正序），可按发送者过滤。返回原始 message dict 列表。"""
        fetch = max(count * 5, 50)
        payloads = {
            "group_id": int(event.get_group_id()),
            "message_seq": 0,
            "count": fetch,
            "reverseOrder": True,
        }
        result: dict = await event.bot.api.call_action(
            "get_group_msg_history", **payloads
        )
        messages = list(reversed(result.get("messages", [])))
        if user_id:
            messages = [
                m for m in messages if str(m["sender"]["user_id"]) == str(user_id)
            ]
        return messages[-count:] if count > 0 else []

    async def recall_user_messages(
        self,
        event: AiocqhttpMessageEvent,
        target_ids: set[str],
        count: int,
    ) -> int:
        """撤回指定用户们最近的 count 条消息，返回实际撤回条数。
        拉取足够大的历史窗口以保证覆盖目标消息；自动跳过当前请求消息本身。"""
        client = event.bot
        # 取较大的窗口，避免目标用户的消息被其他人的消息稀释
        fetch = max(count * 5, 50)
        payloads = {
            "group_id": int(event.get_group_id()),
            "message_seq": 0,
            "count": fetch,
            "reverseOrder": True,
        }
        result: dict = await client.api.call_action(
            "get_group_msg_history", **payloads
        )
        # reverseOrder=True 返回最新在前，反转为时间正序
        messages = list(reversed(result.get("messages", [])))
        # 排除当前请求消息本身
        current_mid = getattr(event.message_obj, "message_id", None)
        target_msgs = [
            m
            for m in messages
            if str(m["sender"]["user_id"]) in target_ids
            and m["message_id"] != current_mid
        ]
        # 只撤最近 count 条
        to_delete = target_msgs[-count:] if count > 0 else []
        delete_count = 0
        sem = asyncio.Semaphore(10)

        async def try_delete(message: dict):
            nonlocal delete_count
            async with sem:
                try:
                    await client.delete_msg(message_id=message["message_id"])
                    delete_count += 1
                except Exception:
                    pass

        await asyncio.gather(*(try_delete(m) for m in to_delete))
        return delete_count

    async def delete_msg(self, event: AiocqhttpMessageEvent):
        """(引用消息)撤回 | 撤回 @某人(默认bot) 数量(默认10)"""
        client = event.bot
        chain = event.get_messages()
        first_seg = chain[0]
        if isinstance(first_seg, Reply):
            try:
                await client.delete_msg(message_id=int(first_seg.id))
                await self._send_recall_notice(event)
            except Exception:
                fail_msg = await self._recall_fail_reason(
                    event, get_replyer_id(event)
                )
                await event.send(event.plain_result(fail_msg))
            finally:
                event.stop_event()
        elif any(isinstance(seg, At) for seg in chain):
            target_ids = get_ats(event) or [event.get_self_id()]
            target_ids = {str(uid) for uid in target_ids}

            end_arg = event.message_str.split()[-1]
            count = int(end_arg) if end_arg.isdigit() else 10

            delete_count = await self.recall_user_messages(event, target_ids, count)

            await event.send(
                event.plain_result(f"已撤回{delete_count}条消息")
            )
            if delete_count > 0:
                await self._send_recall_notice(event)
            else:
                # 一条都没撤成，给出失败原因（以第一个目标为参照）
                fail_msg = await self._recall_fail_reason(event, next(iter(target_ids)))
                await event.send(event.plain_result(fail_msg))
