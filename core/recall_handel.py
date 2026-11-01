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

            payloads = {
                "group_id": int(event.get_group_id()),
                "message_seq": 0,
                "count": count,
                "reverseOrder": True,
            }
            result: dict = await client.api.call_action(
                "get_group_msg_history", **payloads
            )

            messages = list(reversed(result.get("messages", [])))
            delete_count = 0
            sem = asyncio.Semaphore(10)

            # 撤回消息
            async def try_delete(message: dict):
                nonlocal delete_count
                if str(message["sender"]["user_id"]) not in target_ids:
                    return
                async with sem:
                    try:
                        await client.delete_msg(message_id=message["message_id"])
                        delete_count += 1
                    except Exception:
                        pass

            # 并发撤回
            tasks = [try_delete(msg) for msg in messages]
            await asyncio.gather(*tasks)

            await event.send(
                event.plain_result(f"已从{count}条消息中撤回{delete_count}条")
            )
            if delete_count > 0:
                await self._send_recall_notice(event)
            elif target_ids:
                # 一条都没撤成，给出失败原因（以第一个目标为参照）
                fail_msg = await self._recall_fail_reason(event, next(iter(target_ids)))
                await event.send(event.plain_result(fail_msg))
