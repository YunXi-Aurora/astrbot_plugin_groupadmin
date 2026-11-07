from astrbot.api import logger
import asyncio
from astrbot.core.message.components import Reply
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from ..config import PluginConfig
from ..data import QQAdminDB
from ..utils import (
    extract_image_url,
    format_duration,
    get_ats,
    get_nickname,
    pair_targets_and_times,
    parse_bool,
    parse_duration,
    split_list_arg,
)


class NormalHandle:
    def __init__(self, config: PluginConfig, db: QQAdminDB):
        self.cfg = config
        self.db = db

    async def set_group_ban(
        self,
        event: AiocqhttpMessageEvent,
        ban_time: int | None = None,
        target_id: str | int | list | None = "",
        instant_unban: bool | list | tuple | str = False,
        ban_times: str | list | tuple | None = None,
        instant_unbans: str | list | tuple | None = None,
    ):
        """
        禁言一个或多个目标。

        支持三种入参形态：
        1. target_id 单个 + ban_time 单个（原行为）；
        2. target_id 为 "123,456" / ["123","456"]，ban_time 单个 -> 所有人同一时长；
        3. target_id 与 ban_times 都是等长列表 -> 按位置一一配对，
           如 target_id="1,2,3"、ban_times="1h,2h,3h" -> 1 禁 1 小时、2 禁 2 小时、3 禁 3 小时。

        instant_unban 支持分人：
        - 传 True/False -> 整批统一；
        - 传 "true,false,true" 或 [True, False, True] -> 与目标按位置一一对应；
        - instant_unbans 与 instant_unban 同义，二者取其一即可（instant_unbans 优先）。
        """
        group_config = self.db.get_group_snapshot(event.get_group_id())
        if ban_time is None:
            ban_time = self.cfg.get_ban_time_with_range(
                group_config.get("random_ban_time"), 60
            )
        else:
            # 时间参数支持单位换算："1分钟"/"1min"/"60s"/"60秒" 均等价
            parsed = parse_duration(ban_time)
            if parsed is None:
                return (
                    f"禁言时长参数无效：{ban_time!r}"
                    "（支持如 60、60秒、60s、1分钟、1min、1小时、1h、1天 等写法）"
                )
            ban_time = max(parsed, 0)

        # 算出 [(uid, 该 uid 的时长或 None 表示沿用 ban_time)]
        if target_id:
            pairs, error = pair_targets_and_times(target_id, ban_times)
            if error:
                return error
        else:
            ats = get_ats(event)
            times = split_list_arg(ban_times)
            if len(times) == len(ats):
                pairs = list(zip(ats, times))
            else:
                pairs = [(tid, None) for tid in ats]

        if not pairs:
            return "未指定要禁言的用户"

        # 分人解析 instant_unban：布尔 -> 整批同值；列表/字符串 -> 按位置对应
        flags_src = instant_unbans if instant_unbans not in (None, "", []) else instant_unban
        if isinstance(flags_src, (list, tuple)):
            flag_list = [parse_bool(f, default=False) for f in flags_src]
        elif isinstance(flags_src, str):
            raw_flags = split_list_arg(flags_src)
            if len(raw_flags) > 1:
                flag_list = [parse_bool(f, default=False) for f in raw_flags]
            else:
                flag_list = [parse_bool(flags_src, default=False)]
        else:
            flag_list = [bool(flags_src)]

        # 数量不足直接拒绝，避免「该真禁言的被当成吓唬」这种静默误判
        if len(flag_list) > 1 and len(flag_list) < len(pairs):
            return (
                f"娱乐式开关数量（{len(flag_list)}）少于目标数量（{len(pairs)}），"
                "无法一一对应。请补齐，或只填一个让所有目标共用。"
            )
        flag_list = flag_list[: len(pairs)]

        def _flag_at(idx: int) -> bool:
            """单个值 -> 全员共用；等长列表 -> 按位置"""
            if len(flag_list) == 1:
                return flag_list[0]
            return flag_list[idx]

        results = []
        for idx, (tid, per_time) in enumerate(pairs):
            do_instant_unban = _flag_at(idx)
            duration = ban_time
            if per_time is not None:
                parsed = parse_duration(per_time)
                if parsed is None:
                    results.append(f"用户[{tid}]禁言失败：时长 {per_time!r} 无法解析")
                    continue
                duration = max(parsed, 0)
            try:
                await event.bot.set_group_ban(
                    group_id=int(event.get_group_id()),
                    user_id=int(tid),
                    duration=duration,
                )
                time_desc = format_duration(duration) if duration else "0秒"
                msg = f"用户[{tid}]已被禁言{time_desc}"
                if do_instant_unban and duration > 0:
                    # 娱乐式禁言：下发后立刻解禁，但提示文案与普通禁言完全一致（不剧透）
                    try:
                        await asyncio.sleep(1)
                        await event.bot.set_group_ban(
                            group_id=int(event.get_group_id()),
                            user_id=int(tid),
                            duration=0,
                        )
                    except Exception as e:
                        logger.error(
                            f"娱乐式禁言解禁失败 群{event.get_group_id()} "
                            f"用户{tid}: {e!r}"
                        )
                        msg = f"用户[{tid}]已被禁言{time_desc}（解禁失败，联系管理员手动解禁）"
                results.append(msg)
            except Exception as e:
                logger.error(f"禁言失败 群{event.get_group_id()} 用户{tid}: {e!r}")
                results.append(f"用户[{tid}]禁言失败")
        event.stop_event()
        return "\n".join(results)

    async def set_group_whole_ban(self, event: AiocqhttpMessageEvent, enable: bool):
        await event.bot.set_group_whole_ban(
            group_id=int(event.get_group_id()), enable=enable
        )
        return "已开启全体禁言" if enable else "已关闭全体禁言"

    async def set_group_card(
        self,
        event: AiocqhttpMessageEvent,
        target_id: str | int = "",
        target_card: str | int = "",
    ):
        tids = ([target_id] if target_id else get_ats(event)) or [event.get_sender_id()]
        results = []
        for tid in tids:
            target_name = await get_nickname(event, user_id=tid)
            results.append(
                f"已修改{target_name}的群昵称为【{target_card}】"
                if target_card
                else f"已清除{target_name}的群昵称"
            )
            await event.bot.set_group_card(
                group_id=int(event.get_group_id()),
                user_id=int(tid),
                card=str(target_card),
            )
        return "\n".join(results) if results else "未指定要设置群昵称的用户"

    async def set_group_special_title(
        self,
        event: AiocqhttpMessageEvent,
        target_id: str | int = "",
        special_title: str | int = "",
    ):
        tids = ([target_id] if target_id else get_ats(event)) or [event.get_sender_id()]
        results = []
        for tid in tids:
            target_name = await get_nickname(event, user_id=tid)
            results.append(
                f"已修改{target_name}的头衔为【{special_title}】"
                if special_title
                else f"已清除{target_name}的头衔"
            )
            await event.bot.set_group_special_title(
                group_id=int(event.get_group_id()),
                user_id=int(tid),
                special_title=str(special_title),
                duration=-1,
            )
        return "\n".join(results) if results else "未指定要设置头衔的用户"

    async def set_group_kick(
        self, event: AiocqhttpMessageEvent, target_id: str | int = ""
    ):
        tids = [target_id] if target_id else get_ats(event)
        results = []
        for tid in tids:
            target_name = await get_nickname(event, user_id=tid)
            await event.bot.set_group_kick(
                group_id=int(event.get_group_id()),
                user_id=int(tid),
                reject_add_request=False,
            )
            results.append(f"已将【{tid}-{target_name}】踢出本群")
        return "\n".join(results) if results else "未指定要踢出的用户"

    async def set_group_block(
        self, event: AiocqhttpMessageEvent, target_id: str | int = ""
    ):
        tids = [target_id] if target_id else get_ats(event)
        results = []
        for tid in tids:
            target_name = await get_nickname(event, user_id=tid)
            await event.bot.set_group_kick(
                group_id=int(event.get_group_id()),
                user_id=int(tid),
                reject_add_request=True,
            )
            results.append(f"已将【{tid}-{target_name}】踢出本群并拉黑!")
        return "\n".join(results) if results else "未指定要拉黑的用户"

    async def set_group_admin(self, event: AiocqhttpMessageEvent, enable: bool):
        results = []
        for tid in get_ats(event):
            target_name = await get_nickname(event, user_id=tid)
            await event.bot.set_group_admin(
                group_id=int(event.get_group_id()), user_id=int(tid), enable=enable
            )
            msg = (
                f"{target_name}已被设为管理员"
                if enable
                else f"{target_name}的管理员身份已被取消"
            )
            results.append(msg)
        return "\n".join(results) if results else "未指定要操作的用户"

    async def set_essence_msg(
        self,
        event: AiocqhttpMessageEvent,
        enable: bool,
        message_id: str | int = "",
    ):
        if not message_id:
            chain = event.get_messages()
            first_seg = chain[0] if chain else None
            if not isinstance(first_seg, Reply):
                return "未指定要设置精华的消息"
            message_id = first_seg.id
        if enable:
            await event.bot.set_essence_msg(message_id=int(message_id))
            return "已设为精华消息"
        else:
            await event.bot.delete_essence_msg(message_id=int(message_id))
            return "已取消精华消息"

    async def get_essence_msg_list(self, event: AiocqhttpMessageEvent):
        """查看群精华"""
        essence_data = await event.bot.get_essence_msg_list(
            group_id=int(event.get_group_id())
        )
        if not essence_data:
            return "没有群精华消息"
        return f"{essence_data}"

    async def set_group_portrait(
        self, event: AiocqhttpMessageEvent, image_url: str | None = None
    ):
        image_url = image_url or extract_image_url(chain=event.get_messages())
        if not image_url:
            return "未获取到新头像"
        await event.bot.set_group_portrait(
            group_id=int(event.get_group_id()),
            file=image_url,
        )
        return "群头像已更新"

    async def set_group_name(
        self, event: AiocqhttpMessageEvent, group_name: str | int | None = None
    ):
        if not group_name:
            return "未输入新群名"
        await event.bot.set_group_name(
            group_id=int(event.get_group_id()), group_name=str(group_name)
        )
        return f"本群群名更新为：{group_name}"
