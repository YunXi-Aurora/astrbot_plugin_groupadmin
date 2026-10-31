from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ..utils import format_time

if TYPE_CHECKING:
    from ..main import QQAdminPlugin


class MemberHandle:
    def __init__(self, plugin: QQAdminPlugin):
        self.plugin = plugin

    async def get_group_member_list(self, event: AiocqhttpMessageEvent):
        """查看群友信息，人数太多时可能会处理失败"""
        await event.send(event.plain_result("获取中..."))
        group_id = event.get_group_id()
        members_data = await event.bot.get_group_member_list(group_id=int(group_id))
        info_list = [
            (
                f"{format_time(member['join_time'])}："
                f"【{member['level']}】"
                f"{member['user_id']}-"
                f"{member['nickname']}"
            )
            for member in members_data
        ]
        info_list.sort(key=lambda x: datetime.strptime(x.split("：")[0], "%Y-%m-%d"))
        info_str = "进群时间：【等级】QQ-昵称\n\n"
        info_str += "\n\n".join(info_list)
        # TODO 做张好看的图片来展示
        url = await self.plugin.text_to_image(info_str)
        await event.send(event.image_result(url))
