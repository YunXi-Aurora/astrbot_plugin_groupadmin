import os
import re
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import File
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .config import PluginConfig
from .core import (
    FileHandle,
    JoinHandle,
    MemberHandle,
    NormalHandle,
    NoticeHandle,
    RecallHandle,
)
from .data import QQAdminDB
from .group_info_cache import QQGroupInfoCache
from .permission import (
    PermLevel,
    perm_manager,
    require_superuser,
    require_admin,
    require_member,
)
from .utils import get_ats, parse_ban_args, parse_bool, split_list_arg
from .official import OfficialHandle, HELP, is_official, official_tool
from .web import QQAdminWebController



# OneBot / NapCat 平台的群管帮助。官方机器人走 official.HELP，两套命令集不同。
ONEBOT_ONLY_HINT = (
    "{cmd} 是 QQ 官方机器人专用指令，当前 NapCat / OneBot 平台不适用。\n"
    "本平台请发送 /群管帮助 查看可用指令。"
)

HELP_ONE = """QQ群管（重构版） · OneBot
【查询】/群友信息  /群公告  /群精华  /查看群文件
【禁言】/禁言 @某人 1h30m   /禁言 QQ号1,QQ号2 1h,2h   /娱乐式禁言 @某人 30m 玩
        /解禁 @某人   /全禁 开|关
【管理】/改名 新昵称 @某人   /改头衔 头衔 @某人   /申请头衔 头衔
        /踢了 @某人   /群拉黑 @某人   /上管 @某人   /下管 @某人
        /设精（引用） /移精（引用）   /撤回（引用） /撤回 @某人 数量
        /发布群公告 内容   /设置群名 新群名   /设置群头像（引用图片）
        /上传群文件   /删除群文件
【进群】/进群审核 开|关   /进群白词 词   /进群黑词 词   /未命中驳回 开
        /进群次数 n   /进群黑名单 +QQ|-QQ   /进群禁言 秒   /进群欢迎 开
        /退群通知 开   /退群拉黑 开   /批准   /驳回 理由
【超管】/特殊权限 查看|添加 @成员|删除 @成员|清空
        /群管状态  /群管配置 群号 配置串  /群管重置 群号|all
时间单位：周/天/小时/分钟/秒，或 w/d/h/min/sec；支持 1h30m、1.5小时。
官方机器人专用命令（禁言状态/入群申请/群黑名单/解除群拉黑）不适用于本平台。
"""

class QQAdminPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.official = OfficialHandle(self)
        self.cfg = PluginConfig(config, context)
        self.db = QQAdminDB(self.cfg)
        self.db.default_cfg = self.cfg.build_group_default_config()
        self.group_cache = QQGroupInfoCache(context, self.db)
        self.normal = NormalHandle(self.cfg, self.db)
        self.recall = RecallHandle(self.cfg, self.db, perm_manager)
        self.notice = NoticeHandle(self, self.cfg)
        self.join = JoinHandle(self.cfg, self.db)
        self.member = MemberHandle(self)
        self.file = FileHandle(self.cfg)
        self.web = QQAdminWebController(context, self.cfg, self.db, self.group_cache)
        self.web.register_routes()

    async def initialize(self):
        await self.db.init()
        perm_manager.lazy_init(self.cfg, self.db)

    async def terminate(self):
        await self.db.close()

    @filter.command("群管帮助", alias={"群管菜单"})
    async def official_help(self, event: AiocqhttpMessageEvent):
        if is_official(event):
            yield event.plain_result(HELP)
        else:
            yield event.plain_result(HELP_ONE)
        event.stop_event()

    @filter.command("群管状态")
    async def official_status(self, event: AiocqhttpMessageEvent):
        if is_official(event):
            yield event.plain_result('正在检查本群的官方接口权限，请稍等～')
            yield event.plain_result(await self.official.run('status', event))
        else:
            yield event.plain_result(ONEBOT_ONLY_HINT.format(cmd="群管状态"))
        event.stop_event()

    @filter.command("禁言状态")
    async def official_mutes(self, event: AiocqhttpMessageEvent):
        if is_official(event):
            yield event.plain_result(await self.official.run('mutes', event))
        else:
            yield event.plain_result(ONEBOT_ONLY_HINT.format(cmd="禁言状态"))
        event.stop_event()

    @filter.command("入群申请")
    async def official_requests(self, event: AiocqhttpMessageEvent):
        if is_official(event):
            yield event.plain_result(await self.official.run('requests', event))
        else:
            yield event.plain_result(ONEBOT_ONLY_HINT.format(cmd="入群申请"))
        event.stop_event()

    @filter.command("群黑名单")
    async def official_blacklist(self, event: AiocqhttpMessageEvent):
        if is_official(event):
            yield event.plain_result(await self.official.run('blacklist', event))
        else:
            yield event.plain_result(ONEBOT_ONLY_HINT.format(cmd="群黑名单"))
        event.stop_event()

    @filter.command("解除群拉黑")
    async def official_unblock(self, event: AiocqhttpMessageEvent, member_id: str = ''):
        if is_official(event):
            yield event.plain_result(await self.official.run('unblock', event, {'member_id': member_id}))
        else:
            yield event.plain_result(ONEBOT_ONLY_HINT.format(cmd="解除群拉黑"))
        event.stop_event()

    @filter.llm_tool()
    async def qqadmin_official_query(self, event: AiocqhttpMessageEvent, kind: str = 'status'):
        """查询当前 QQ 官方群聊的群管接口状态或成员，不执行管理操作。
        Args:
            kind(string): status=权限诊断，members=群友信息，mutes=禁言状态，requests=入群申请，blacklist=群黑名单。
        """
        if not is_official(event):
            yield '此查询工具用于 QQ 官方群聊。'
            return
        if kind not in {'status', 'members', 'mutes', 'requests', 'blacklist'}:
            yield '查询类型错误，请用 status、members、mutes、requests 或 blacklist。'
            return
        yield await self.official.run(kind, event)

    @filter.llm_tool()
    async def qqadmin_official_approval(self, event: AiocqhttpMessageEvent, operation: str,
                                        member_id: str, request_id: str = ''):
        """由授权管理员处理当前 QQ 官方群的指定入群申请或解除黑名单。
        Args:
            operation(string): approve批准申请，decline拒绝申请，unblock解除群黑名单。
            member_id(string): 当前群的成员OpenID，不能用QQ号或私聊UID。
            request_id(string): approve和decline必填，为查询入群申请得到的申请ID。
        """
        if not is_official(event):
            yield '此工具仅用于 QQ 官方群聊。'
            return
        if operation not in {'approve','decline','unblock'}:
            yield '操作类型错误，未执行。'
            return
        yield await self.official.run(operation, event, {'member_id':member_id,'request_id':request_id})

    @filter.command("特殊权限", alias={"特殊权限名单"})
    @require_superuser(bot_perm=PermLevel.UNKNOWN, check_at=False)
    async def special_admins(self, event: AiocqhttpMessageEvent):
        """特殊权限 查看 | 添加 @成员/QQ号 | 删除 @成员/QQ号 | 清空"""
        gid = event.get_group_id()
        raw = event.message_str.partition(" ")[2].strip()

        if not raw:
            admins = await self.db.get(gid, "special_admins", [])
            if admins:
                yield event.plain_result(f"本群特殊权限名单：\n{'、'.join(str(a) for a in admins)}")
            else:
                yield event.plain_result("本群特殊权限名单为空")
            return

        op, args_str = (raw.split(" ", 1) + [""])[:2]
        op = op.strip()

        if op == "清空":
            await self.db.set(gid, "special_admins", [])
            yield event.plain_result("已清空本群特殊权限名单")
            return

        if op not in ("添加", "删除"):
            yield event.plain_result("用法：特殊权限 [添加/删除 @成员或QQ号 | 清空]")
            return

        # @成员 + 纯数字QQ号 混合提取
        ids = [str(a) for a in get_ats(event)]
        ids += re.findall(r"\d{5,11}", args_str)
        # 去重且保持顺序
        ids = list(dict.fromkeys(ids))
        if not ids:
            yield event.plain_result(f"请提供要{op}的成员（@成员或QQ号）")
            return

        curr = [str(a) for a in await self.db.get(gid, "special_admins", [])]
        if op == "添加":
            added = [i for i in ids if i not in curr]
            curr += added
            await self.db.set(gid, "special_admins", curr)
            if added:
                yield event.plain_result(f"已添加特殊权限：{'、'.join(added)}")
            else:
                yield event.plain_result("这些成员已在特殊权限名单中")
        else:  # 删除
            removed = [i for i in ids if i in curr]
            curr = [i for i in curr if i not in ids]
            await self.db.set(gid, "special_admins", curr)
            if removed:
                yield event.plain_result(f"已移除特殊权限：{'、'.join(removed)}")
            else:
                yield event.plain_result("这些成员不在特殊权限名单中")

    @filter.command("群管配置", alias={"群管设置"})
    @require_superuser(bot_perm=PermLevel.UNKNOWN, check_at=False)
    async def set_config(self, event: AiocqhttpMessageEvent):
        """群管配置 <群号 | 留空> <配置串>"""
        raw: str = event.message_str.partition(" ")[2].strip()
        if not raw:
            gid = event.get_group_id()
            config_str = await self.db.export_cn_lines(gid)
            yield event.plain_result(f"【群管配置】\n{config_str}")
            return
        m = re.match(r"(\d+)\s+(.+)", raw)
        if m:
            gid = str(m.group(1))
            arg = m.group(2)
        else:
            gid = event.get_group_id()
            arg = raw
        await self.db.import_cn_lines(gid, arg)
        config_str = await self.db.export_cn_lines(gid)
        yield event.plain_result(f"【群管配置】更新:\n{config_str}")

    @filter.command("群管重置")
    @require_superuser(bot_perm=PermLevel.UNKNOWN, check_at=False)
    async def reset_config(
        self, event: AiocqhttpMessageEvent, group_id: str | int | None = None
    ):
        """群管重置 <群号 | all>"""
        gid = group_id or event.get_group_id()
        if gid == "all" and event.is_admin():
            await self.db.reset_to_default()
            yield event.plain_result("已重置所有群的群管配置")
        else:
            await self.db.reset_to_default(str(gid))
            yield event.plain_result("已重置本群的群管配置")

    @filter.command("禁言")
    @require_admin()
    async def set_group_ban(self, event: AiocqhttpMessageEvent):
        """禁言 @群友 时间；支持 QQ号批量与逐人时长/逐人真假：禁言 123,456 1h,2h 玩,真"""
        raw = event.message_str.partition(" ")[2].strip()
        ids, times, flags = parse_ban_args(raw)
        if ids:
            # 号码批量模式：ids / times / flags 均按位置一一对应
            result = await self.normal.set_group_ban(
                event,
                ban_time=times[0] if len(times) == 1 else None,
                target_id=ids,
                ban_times=times if len(times) > 1 else None,
                instant_unban=False,
                instant_unbans=flags if flags else None,
            )
        else:
            # @ 模式：时间与真假都取解析出的第一个
            result = await self.normal.set_group_ban(
                event,
                ban_time=times[0] if times else None,
                instant_unban=flags[0] if flags else False,
            )
        if result:
            yield event.plain_result(result)

    @filter.command("娱乐式禁言", alias={"玩笑禁言", "吓唬禁言"})
    @require_admin()
    async def fun_group_ban(self, event: AiocqhttpMessageEvent):
        """娱乐式禁言 @群友 时间；支持批量并逐人指定真假：娱乐式禁言 123,456 1h,2h 玩,真"""
        raw = event.message_str.partition(" ")[2].strip()
        ids, times, flags = parse_ban_args(raw)
        if ids:
            result = await self.normal.set_group_ban(
                event,
                ban_time=times[0] if len(times) == 1 else None,
                target_id=ids,
                ban_times=times if len(times) > 1 else None,
                instant_unban=True,
                instant_unbans=flags if flags else None,
            )
        else:
            result = await self.normal.set_group_ban(
                event,
                ban_time=times[0] if times else None,
                instant_unban=flags[0] if flags else True,
            )
        if result:
            yield event.plain_result(result)

    @filter.command("解禁")
    @require_admin()
    async def cancel_group_ban(self, event: AiocqhttpMessageEvent):
        """解禁 @群友"""
        if result := await self.normal.set_group_ban(event, ban_time=0):
            yield event.plain_result(result)

    @filter.command("全禁", alias={"全员禁言", "群全员禁言"})
    @require_admin()
    async def set_group_whole_ban(
        self, event: AiocqhttpMessageEvent, enable: bool | str = True
    ):
        """全禁 开/关, 开启或关闭群全员禁言"""
        enable = parse_bool(enable, default=True)
        if result := await self.normal.set_group_whole_ban(event, enable):
            yield event.plain_result(result)

    @filter.command("改名")
    @require_admin()
    async def set_group_card(
        self, event: AiocqhttpMessageEvent, target_card: str | int = ""
    ):
        """改名 <新昵称> @user"""
        if result := await self.normal.set_group_card(event, target_id=target_card):
            yield event.plain_result(result)

    @filter.command("改头衔", alias={"头衔"})
    @require_admin(bot_perm=PermLevel.BOT_OWNER)
    async def set_group_special_title(
        self, event: AiocqhttpMessageEvent, special_title: str | int = ""
    ):
        """改头衔 <新头衔> @群友"""
        if result := await self.normal.set_group_special_title(
            event, special_title=special_title
        ):
            yield event.plain_result(result)

    @filter.command("申请头衔", alias={"我要头衔"})
    @require_admin(bot_perm=PermLevel.BOT_OWNER)
    async def set_group_special_title_me(
        self, event: AiocqhttpMessageEvent, special_title: str | int = ""
    ):
        """申请头衔 <新头衔>"""
        if result := await self.normal.set_group_special_title(
            event, special_title=special_title
        ):
            yield event.plain_result(result)

    @filter.command("踢了")
    @require_admin()
    async def set_group_kick(self, event: AiocqhttpMessageEvent):
        """踢了@群友"""
        if result := await self.normal.set_group_kick(event):
            yield event.plain_result(result)

    @filter.command("群拉黑")
    @require_admin()
    async def set_group_block(self, event: AiocqhttpMessageEvent):
        """群拉黑@群友"""
        if result := await self.normal.set_group_block(event):
            yield event.plain_result(result)

    @filter.command("上管", alias={"设置管理员"})
    @require_admin(bot_perm=PermLevel.BOT_OWNER, check_at=False)
    async def set_group_admin(self, event: AiocqhttpMessageEvent):
        """上管@群友，将群友设为管理员"""
        if result := await self.normal.set_group_admin(event, enable=True):
            yield event.plain_result(result)

    @filter.command("下管", alias={"取消管理员"})
    @require_admin(bot_perm=PermLevel.BOT_OWNER, check_at=False)
    async def cancel_group_admin(self, event: AiocqhttpMessageEvent):
        """下管@群友，取消群友的管理员身份"""
        if result := await self.normal.set_group_admin(event, enable=False):
            yield event.plain_result(result)

    @filter.command("设精", alias={"设为精华"})
    @require_admin()
    async def set_essence_msg(self, event: AiocqhttpMessageEvent):
        """(引用消息)设精, 将消息设为群精华"""
        if result := await self.normal.set_essence_msg(event, enable=True):
            yield event.plain_result(result)

    @filter.command("移精", alias={"移除精华"})
    @require_admin()
    async def delete_essence_msg(self, event: AiocqhttpMessageEvent):
        """(引用消息)移精，将消息移除群精华"""
        if result := await self.normal.set_essence_msg(event, enable=False):
            yield event.plain_result(result)

    @filter.command("群精华", alias={"查看群精华"})
    @require_admin()
    async def get_essence_msg_list(self, event: AiocqhttpMessageEvent):
        """查看群精华"""
        if result := await self.normal.get_essence_msg_list(event):
            yield event.plain_result(result)

    @filter.command("设置群头像")
    @require_admin()
    async def set_group_portrait(self, event: AiocqhttpMessageEvent):
        """(引用图片)设置群头像"""
        if result := await self.normal.set_group_portrait(event):
            yield event.plain_result(result)

    @filter.command("设置群名")
    @require_admin()
    async def set_group_name(
        self, event: AiocqhttpMessageEvent, group_name: str | int | None = None
    ):
        """设置群名 <新群名>"""
        if result := await self.normal.set_group_name(event, group_name):
            yield event.plain_result(result)

    @filter.command("撤回")
    @require_admin()
    async def delete_msg(self, event: AiocqhttpMessageEvent):
        """(引用消息)撤回 | 撤回 <@群友> <消息数量>"""
        await self.recall.delete_msg(event)

    @filter.command("发布群公告")
    @require_admin()
    async def send_group_notice(self, event: AiocqhttpMessageEvent):
        """(引用图片)发布群公告 <文字内容>"""
        if result := await self.notice.send_group_notice(event):
            yield event.plain_result(result)

    @filter.command("群公告", alias={"查看群公告"})
    @require_member()
    async def get_group_notice(self, event: AiocqhttpMessageEvent):
        """查看群公告"""
        if result := await self.notice.get_group_notice(event):
            yield event.plain_result(result)
    @filter.command("进群审核")
    @require_admin()
    async def handle_join_review(
        self, event: AiocqhttpMessageEvent, mode: str | bool | None = None
    ):
        "进群审核 开/关，所有进群审核功能的总开关"
        await self.join.handle_join_review(event, mode)

    @filter.command("进群白词")
    @require_admin()
    async def handle_accept_words(self, event: AiocqhttpMessageEvent):
        "设置/查看自动批准进群的关键词（空格隔开，无参数表示查看）"
        await self.join.handle_accept_words(event)

    @filter.command("进群黑词")
    @require_admin()
    async def handle_reject_words(self, event: AiocqhttpMessageEvent):
        "设置/查看进群黑名单关键词（空格隔开，无参数表示查看）"
        await self.join.handle_reject_words(event)

    @filter.command("未命中驳回")
    @require_admin()
    async def handle_no_match_reject(
        self, event: AiocqhttpMessageEvent, mode: str | bool | None = None
    ):
        "设置/查看是否拒绝无关键词的进群申请（无参数表示查看）"
        await self.join.handle_no_match_reject(event, mode)

    @filter.command("进群次数")
    @require_admin()
    async def handle_join_max_time(
        self, event: AiocqhttpMessageEvent, time: int | None = None
    ):
        "设置/查看未命中进群关键词多少次后拉黑（0表示不限制，无参数表示查看）"
        await self.join.handle_join_max_time(event, time)

    @filter.command("进群黑名单")
    @require_admin()
    async def handle_reject_ids(self, event: AiocqhttpMessageEvent):
        "进群黑名单 +QQ -QQ, 带+-则增删, 不带则覆写"
        await self.join.handle_block_ids(event)

    @filter.command("批准", alias={"同意进群"})
    @require_admin()
    async def agree_add_group(self, event: AiocqhttpMessageEvent, extra: str = "", request_id: str = ""):
        "批准进群申请"
        await self.join.agree_add_group(event, extra)

    @filter.command("驳回", alias={"拒绝进群", "不批准"})
    @require_admin()
    async def refuse_add_group(self, event: AiocqhttpMessageEvent, extra: str = "", request_id: str = ""):
        "驳回进群申请"
        await self.join.refuse_add_group(event, extra)

    @filter.command("进群禁言")
    @require_admin()
    async def handle_join_ban(
        self, event: AiocqhttpMessageEvent, time: int | None = None
    ):
        "进群禁言 <秒数>，设为 0 表示本群不启用该功能"
        await self.join.handle_join_ban(event, time)

    @filter.command("进群欢迎")
    @require_admin()
    async def handle_join_welcome(self, event: AiocqhttpMessageEvent):
        "进群欢迎 <欢迎语>"
        await self.join.handle_join_welcome(event)

    @filter.command("退群通知")
    @require_admin()
    async def handle_leave_notify(
        self, event: AiocqhttpMessageEvent, mode: str | bool | None = None
    ):
        """退群通知 开/关"""
        await self.join.handle_leave_notify(event, mode)

    @filter.command("退群拉黑")
    @require_admin()
    async def handle_leave_block(
        self, event: AiocqhttpMessageEvent, mode: str | bool | None = None
    ):
        "退群拉黑 开/关, 拉黑后下次进群直接自动拒绝"
        await self.join.handle_leave_block(event, mode)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def event_monitoring(self, event: AiocqhttpMessageEvent):
        """监听进群/退群事件"""
        await self.join.event_monitoring(event)

    @filter.command("群友信息")
    @require_member()
    async def get_group_member_list(self, event: AiocqhttpMessageEvent):
        "查看群友信息"
        await self.member.get_group_member_list(event)

    @filter.command("上传群文件")
    @require_admin()
    async def upload_group_file(
        self,
        event: AiocqhttpMessageEvent,
        path: str | int | None = None,
    ):
        "上传群文件 <文件夹名/文件名 | 文件名>"
        if result := await self.file.upload_group_file(event, str(path)):
            yield event.plain_result(result)

    @filter.command("删除群文件")
    @require_admin()
    async def delete_group_file(
        self,
        event: AiocqhttpMessageEvent,
        path: str | int | None = None,
    ):
        "删除群文件 <文件夹名/序号> <文件名/序号>"
        if result := await self.file.delete_group_file(event, str(path)):
            yield event.plain_result(result)

    @filter.command("查看群文件")
    @require_member()
    async def view_group_file(
        self,
        event: AiocqhttpMessageEvent,
        path: str | int | None = None,
    ):
        "查看群文件 <文件夹名/序号> <文件名/序号>"
        if result := await self.file.view_group_file(event, path):
            yield event.plain_result(result)

    @filter.llm_tool()
    @official_tool
    async def llm_set_group_ban(
        self,
        event: AiocqhttpMessageEvent,
        user_id: str,
        duration: str = "",
        durations: str = "",
        need_auth: bool = True,
        instant_unban: bool = False,
        instant_unbans: str = "",
    ):
        """
        在群聊中禁言一个或多个用户，被禁言的用户在禁言期间将无法发送消息。
        支持批量：user_id 填多个（逗号分隔），durations 填对应时长（逗号分隔），按位置一一对应。
        例：user_id="123,456,789" + durations="1h,2h,3h" → 123 禁 1 小时、456 禁 2 小时、789 禁 3 小时。
        若只给一个时长，则所有目标共用该时长。
        instant_unban 为 True 时为娱乐式禁言：下发禁言后立刻解禁，只吓唬一下不禁真。
        Args:
            user_id(string): OneBot用户QQ号或官方当前群的成员OpenID，不得使用私聊UID。多个用英文逗号分隔，如 "123,456,789"
            duration(string): 单个目标或所有目标共用的禁言时长，支持单位换算，如 "1h" / "30分钟" / "2天" / "3600"（纯数字按秒）；0 表示取消禁言
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True
            instant_unban(boolean): 娱乐式禁言，默认False；为True时下发禁言后立刻解禁（用户会看到禁言提示但随即恢复发言），适用于玩笑场景，用户明确要求"吓唬一下/禁言玩玩"或"禁言后立刻解禁"时使用
            durations(string): 每个目标各自的禁言时长，逗号分隔且与 user_id 顺序一一对应，如 "1h,2h,3h"。仅当需要给不同人设不同时长时填写；填写后优先于 duration；数量必须不少于目标数量
            instant_unbans(string): 逐人娱乐式开关，逗号分隔且与 user_id 顺序一一对应，如 "true,false,true"。用于同一批里有的人只吓唬、有的人真禁言；填写后优先于 instant_unban，数量必须不少于目标数量
        """
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event,
                bot_perm=PermLevel.BOT_ADMIN,
            ):
                yield error
                return

        targets = split_list_arg(user_id)
        if not targets:
            yield "请提供要禁言的 user_id"
            return

        per_flags = split_list_arg(instant_unbans)
        if per_flags and len(per_flags) > 1 and len(per_flags) < len(targets):
            yield (
                f"instant_unbans 数量（{len(per_flags)}）少于目标数量（{len(targets)}），"
                "无法一一对应。请补齐，或只填一个 instant_unban 让所有目标共用。"
            )
            return
        flags_arg = per_flags if len(per_flags) > 1 else None

        per_times = split_list_arg(durations)
        if per_times and len(per_times) > 1:
            if len(per_times) < len(targets):
                yield (
                    f"禁言时长数量（{len(per_times)}）少于目标数量（{len(targets)}），"
                    "无法一一对应。请补齐 durations，或只填一个 duration 让所有目标共用。"
                )
                return
            if result := await self.normal.set_group_ban(
                event,
                ban_time=None,
                target_id=targets,
                instant_unban=instant_unban,
                instant_unbans=flags_arg,
                ban_times=per_times[: len(targets)],
            ):
                yield result
            return

        shared = per_times[0] if per_times else duration
        if result := await self.normal.set_group_ban(
            event,
            ban_time=shared if shared != "" else None,
            target_id=targets,
            instant_unban=instant_unban,
            instant_unbans=flags_arg,
        ):
            yield result

    @filter.llm_tool()
    @official_tool
    async def llm_set_group_card(
        self,
        event: AiocqhttpMessageEvent,
        target_id: int,
        target_card: str,
        need_auth: bool = True,
    ):
        """
        给群聊中某用户设置群昵称。
        Args:
            target_id(number): 要设置群昵称的用户的QQ
            target_card(string): 要设置的群昵称
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True
        """
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event,
                bot_perm=PermLevel.BOT_ADMIN,
            ):
                yield error
                return
        if result := await self.normal.set_group_card(
            event, target_id=target_id, target_card=target_card
        ):
            yield result

    @filter.llm_tool()
    @official_tool
    async def llm_set_group_special_title(
        self,
        event: AiocqhttpMessageEvent,
        target_id: int,
        special_title: str,
        need_auth: bool = True,
    ):
        """
        给群聊中某用户设置专属头衔。
        Args:
            target_id(number): 要设置头衔的用户的QQ
            special_title(string): 要设置的新头衔
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True
        """
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event,
                bot_perm=PermLevel.BOT_OWNER,
            ):
                yield error
                return
        if result := await self.normal.set_group_special_title(
            event, target_id=target_id, special_title=special_title
        ):
            yield result

    @filter.llm_tool()
    @official_tool
    async def llm_set_group_whole_ban(
        self,
        event: AiocqhttpMessageEvent,
        enable: bool = True,
        need_auth: bool = True,
    ):
        """
        开启或关闭群全员禁言。
        Args:
            enable(boolean): 是否开启全员禁言。
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True。
        """
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event, bot_perm=PermLevel.BOT_ADMIN
            ):
                yield error
                return
        if result := await self.normal.set_group_whole_ban(event, enable):
            yield result

    @filter.llm_tool()
    @official_tool
    async def llm_set_group_kick(
        self,
        event: AiocqhttpMessageEvent,
        target_id: str,
    ):
        """
        将指定用户踢出当前群聊(危险操作，本工具已强制鉴权)。
        Args:
            target_id(string): OneBot用户QQ号或官方当前群的成员OpenID。
        """
        if error := await perm_manager.llm_perm_block(
            event, bot_perm=PermLevel.BOT_ADMIN
        ):
            yield error
            return
        if result := await self.normal.set_group_kick(event, target_id=target_id):
            yield result

    @filter.llm_tool()
    @official_tool
    async def llm_set_group_block(
        self,
        event: AiocqhttpMessageEvent,
        target_id: str,
    ):
        """
        将指定用户踢出当前群聊并加入群黑名单(危险操作，本工具已强制鉴权)。
        Args:
            target_id(string): OneBot用户QQ号或官方当前群的成员OpenID。
        """
        if error := await perm_manager.llm_perm_block(
            event, bot_perm=PermLevel.BOT_ADMIN
        ):
            yield error
            return
        if result := await self.normal.set_group_block(event, target_id=target_id):
            yield result

    @filter.llm_tool()
    @official_tool
    async def llm_set_essence_msg(
        self,
        event: AiocqhttpMessageEvent,
        message_id: int,
        enable: bool = True,
        need_auth: bool = True,
    ):
        """
        设置或取消指定消息的群精华。
        Args:
            message_id(number): 要操作的消息ID。
            enable(boolean): 是否设置为群精华，False表示取消群精华。
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True。
        """
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event, bot_perm=PermLevel.BOT_ADMIN
            ):
                yield error
                return
        if result := await self.normal.set_essence_msg(
            event, enable=enable, message_id=message_id
        ):
            yield result

    @filter.llm_tool()
    @official_tool
    async def llm_get_essence_msg_list(
        self,
        event: AiocqhttpMessageEvent,
        need_auth: bool = True,
    ):
        """
        查看当前群的群精华消息列表。
        Args:
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True。
        """
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event, bot_perm=PermLevel.BOT_ADMIN
            ):
                yield error
                return
        if result := await self.normal.get_essence_msg_list(event):
            yield result

    @filter.llm_tool()
    @official_tool
    async def llm_set_group_name(
        self,
        event: AiocqhttpMessageEvent,
        group_name: str,
        need_auth: bool = True,
    ):
        """
        设置当前群的群名称。
        Args:
            group_name(string): 要设置的新群名称。
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True。
        """
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event, bot_perm=PermLevel.BOT_ADMIN
            ):
                yield error
                return
        if result := await self.normal.set_group_name(event, group_name):
            yield result

    @filter.llm_tool()
    @official_tool
    async def llm_set_group_portrait(
        self,
        event: AiocqhttpMessageEvent,
        image_url: str,
        need_auth: bool = True,
    ):
        """
        设置当前群的群头像。
        Args:
            image_url(string): 群头像图片URL或本地图片路径。
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True。
        """
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event, bot_perm=PermLevel.BOT_ADMIN
            ):
                yield error
                return
        if result := await self.normal.set_group_portrait(event, image_url=image_url):
            yield result

    @filter.llm_tool()
    @official_tool
    async def llm_send_group_notice(
        self,
        event: AiocqhttpMessageEvent,
        content: str,
        image_url: str = "",
        need_auth: bool = True,
    ):
        """
        发布一条群公告。
        Args:
            content(string): 群公告正文。
            image_url(string): 可选的公告图片URL或本地图片路径。
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True。
        """
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event, bot_perm=PermLevel.BOT_ADMIN
            ):
                yield error
                return
        if result := await self.notice.send_group_notice(
            event, content=content, image_url=image_url
        ):
            yield result

    @filter.llm_tool()
    @official_tool
    async def llm_get_group_notice(
        self,
        event: AiocqhttpMessageEvent,
        need_auth: bool = True,
    ):
        """
        查看当前群的群公告。
        Args:
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True。
        """
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event, bot_perm=PermLevel.BOT_ADMIN
            ):
                yield error
                return
        if result := await self.notice.get_group_notice(event):
            yield result

    @filter.llm_tool()
    @official_tool
    async def llm_upload_group_file(
        self,
        event: AiocqhttpMessageEvent,
        path: str,
        need_auth: bool = True,
    ):
        """
        上传本地文件到当前群的群文件。
        Args:
            path(string): 本地文件路径，可包含群文件夹路径。
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True。
        """
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event, bot_perm=PermLevel.BOT_ADMIN
            ):
                yield error
                return
        if result := await self.file.upload_group_file(event, path):
            yield result

    @filter.llm_tool()
    @official_tool
    async def llm_delete_group_file(
        self,
        event: AiocqhttpMessageEvent,
        path: str,
        need_auth: bool = True,
    ):
        """
        删除当前群的群文件或群文件夹。
        Args:
            path(string): 群文件名、文件夹名或文件夹/文件名。
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True。
        """
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event, bot_perm=PermLevel.BOT_ADMIN
            ):
                yield error
                return
        if result := await self.file.delete_group_file(event, path):
            yield result

    @filter.llm_tool()
    @official_tool
    async def llm_view_group_file(
        self,
        event: AiocqhttpMessageEvent,
        path: str = "",
        need_auth: bool = True,
    ):
        """
        查看当前群的群文件或群文件夹。
        Args:
            path(string): 可选的文件名、文件夹名或文件夹/文件名，留空查看根目录。
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True。
        """
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event, bot_perm=PermLevel.BOT_ADMIN
            ):
                yield error
                return
        if result := await self.file.view_group_file(event, path):
            yield result

    @filter.llm_tool()
    async def llm_download_group_file(
        self,
        event: AiocqhttpMessageEvent,
        path: str,
        need_auth: bool = True,
    ):
        """
        下载群文件到本地并以文件形式发回群聊，供用户获取群文件内容。
        用法：先用 llm_view_group_file 看清文件名（可取序号），再把文件名或「文件夹名/文件名」传给本工具。
        文件会直接作为群文件消息发出，不会暴露下载直链。
        权限：仅超级管理员与特殊权限名单成员可调用；群主/管理员请改用 llm_view_group_file 查看。
        Args:
            path(string): 群文件的文件名、文件夹名/文件名，或 llm_view_group_file 返回的序号（如 "3" 或 "2/1"）。
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True。
        """
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event, bot_perm=PermLevel.BOT_ADMIN
            ):
                yield error
                return
            # 二次收紧：群文件下载会占用 bot 宿主磁盘与带宽，只放行超管与名单成员，
            # 群主/管理员不在这条通道上（可用 llm_view_group_file 查看，但不下载）
            if not await perm_manager.is_user_strict_admin(
                event, event.get_sender_id()
            ):
                yield "群文件下载仅限超级管理员与特殊权限名单成员"
                return

        if not str(path or "").strip():
            yield "请提供要下载的群文件名，或用 llm_view_group_file 先查看序号"
            return

        local_path, file_info, err = await self.file.download_group_file(
            event, str(path).strip(), max_mb=self.cfg.download_max_mb
        )
        if not local_path or not file_info:
            yield f"下载失败：{err}"
            return

        size_desc = self.file._human_size(os.path.getsize(local_path))
        try:
            await event.send(
                MessageChain(
                    chain=[
                        File(name=local_path.name, file=str(local_path)),
                    ]
                )
            )
        except Exception as e:
            logger.error(f"群文件发送失败：{e!r}")
            yield f"已下载到本地（{size_desc}），但发送失败：{e}"
            return

        yield f"已发送群文件：{local_path.name}（{size_desc}）"

    @filter.llm_tool()
    async def llm_delete_msg(
        self,
        event: AiocqhttpMessageEvent,
        message_id: str | int = "",
        need_auth: bool = True,
    ):
        """
        撤回群聊中指定的一条消息。用户引用消息要求撤回时无需提供message_id。
        Args:
            message_id(string): 可选，要撤回的消息ID（OneBot message_id，数字）；用户引用消息发起撤回时留空
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True
        """
        if event.platform_meta.name != "aiocqhttp":
            yield "该工具需要 NapCat / OneBot 平台；QQ 官方机器人不提供撤回接口。"
            return
        if event.is_private_chat():
            return
        # 未显式给出 message_id 时，从引用消息中提取
        if message_id in ("", None):
            from astrbot.core.message.components import Reply

            reply_seg = next(
                (seg for seg in event.get_messages() if isinstance(seg, Reply)), None
            )
            if reply_seg is None:
                yield "请提供要撤回的消息ID，或直接引用那条消息再让我撤回。"
                return
            message_id = reply_seg.id
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            yield "参数错误：消息ID必须是数字（OneBot message_id）。"
            return
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event, bot_perm=PermLevel.BOT_ADMIN
            ):
                yield error
                return
        try:
            await event.bot.delete_msg(message_id=mid)
            yield f"已撤回消息 {message_id}"
        except Exception:
            yield "撤回失败：我无权撤回这条消息或消息ID无效"

    @filter.llm_tool()
    async def llm_get_recent_messages(
        self,
        event: AiocqhttpMessageEvent,
        count: int = 10,
        user_id: str = "",
        keyword: str = "",
        need_auth: bool = True,
    ):
        """
        获取当前群最近的聊天消息列表，包含每条消息的ID、发送者和内容摘要，一次可查询多条。支持按内容关键词模糊匹配（部分字符即可命中，大小写不敏感）。当需要撤回消息但不知道消息ID时（如用户要求"撤回刚才那几张图""把他说的话都撤了""撤回有xxx字样的消息"），先用此工具查到消息ID，再用 llm_delete_msg_batch 的 message_ids 参数撤回。
        Args:
            count(number): 最多返回多少条消息，默认10；查询多条时按需调大
            user_id(string): 可选，只看某用户的消息（QQ号）；留空看所有人
            keyword(string): 可选，内容关键词，模糊匹配消息文本的部分字符，如"测试"；命中多条时全部返回
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True
        """
        if event.platform_meta.name != "aiocqhttp":
            yield "该工具需要 NapCat / OneBot 平台。"
            return
        if event.is_private_chat():
            return
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event, bot_perm=PermLevel.BOT_ADMIN
            ):
                yield error
                return
        count = min(max(int(count), 1), 100)
        try:
            messages = await self.recall.get_recent_messages(
                event, count, user_id=str(user_id).strip(), keyword=keyword.strip()
            )
        except Exception as e:
            logger.error(f"获取最近消息失败 群{event.get_group_id()}: {e!r}")
            yield f"获取消息失败：{e}"
            return
        if not messages:
            yield "未获取到消息"
            return
        current_mid = getattr(event.message_obj, "message_id", None)
        lines = []
        for m in messages:
            mid = m["message_id"]
            sender = m.get("sender", {})
            uid = sender.get("user_id", "?")
            nick = sender.get("card") or sender.get("nickname") or str(uid)
            time_str = m.get("time", "?")
            # 拼接消息内容摘要（文本/图片/At等类型标记）
            parts = []
            for seg in m.get("message", []):
                seg_type = seg.get("type", "")
                if seg_type == "text":
                    parts.append(seg.get("data", {}).get("text", "").strip())
                elif seg_type == "image":
                    parts.append("[图片]")
                elif seg_type == "at":
                    parts.append(f"@{seg.get('data', {}).get('qq', '?')}")
                elif seg_type == "reply":
                    parts.append("[引用]")
                else:
                    parts.append(f"[{seg_type}]")
            content = "".join(p for p in parts if p)[:60]
            mark = "（本次请求）" if mid == current_mid else ""
            lines.append(f"ID:{mid} | {nick}[{uid}] | {content}{mark}")
        yield "\n".join(lines)

    @filter.llm_tool()
    async def llm_delete_msg_batch(
        self,
        event: AiocqhttpMessageEvent,
        message_ids: str = "",
        user_id: str = "",
        count: int = 1,
        need_auth: bool = True,
    ):
        """
        批量撤回消息，两种模式：1) 传 message_ids（逗号分隔的消息ID列表）撤回任意指定的多条不连续消息，如"123,456,789"，先用 llm_get_recent_messages 查询ID；2) 传 user_id + count 撤回某用户最近 count 条连续消息。二选一，优先 message_ids。
        Args:
            message_ids(string): 可选，要撤回的消息ID列表，英文逗号分隔，如"123,456,789"；用于撤回多条不连续的指定消息
            user_id(string): 可选，要撤回消息的用户QQ号；配合count使用，撤回该用户最近count条消息；留空默认为当前发消息的用户自己
            count(number): 要撤回的消息条数，默认1；仅在未传message_ids时生效
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True
        """
        if event.platform_meta.name != "aiocqhttp":
            yield "该工具需要 NapCat / OneBot 平台；QQ 官方机器人不提供撤回接口。"
            return
        if event.is_private_chat():
            return
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event, bot_perm=PermLevel.BOT_ADMIN
            ):
                yield error
                return
        # 模式1：按 ID 列表撤回（不连续多条）
        ids_str = str(message_ids).strip()
        if ids_str:
            try:
                mids = [int(x) for x in ids_str.split(",") if x.strip()]
            except ValueError:
                yield "参数错误：message_ids 必须是英文逗号分隔的数字列表，如 \"123,456\"。"
                return
            if not mids:
                yield "message_ids 为空且未指定用户，无法撤回。"
                return
            # 排除当前请求消息本身
            current_mid = getattr(event.message_obj, "message_id", None)
            mids = [m for m in mids if m != current_mid]
            if not mids:
                yield "没有可撤回的消息（已排除本次请求消息本身）。"
                return
            try:
                delete_count = await self.recall.delete_msgs_by_ids(event, mids)
            except Exception as e:
                logger.error(f"批量撤回失败 群{event.get_group_id()}: {e!r}")
                yield f"批量撤回失败：{e}"
                return
            if delete_count > 0:
                yield f"已撤回 {delete_count} 条消息"
            else:
                reason = await self.recall._recall_fail_reason(event, None)
                yield reason
            return
        # 模式2：按用户+条数撤回（最近连续N条）
        target = str(user_id).strip() or event.get_sender_id()
        count = max(int(count), 1)
        try:
            delete_count = await self.recall.recall_user_messages(
                event, {target}, count
            )
        except Exception as e:
            logger.error(f"批量撤回失败 群{event.get_group_id()}: {e!r}")
            yield f"批量撤回失败：{e}"
            return
        if delete_count > 0:
            yield f"已撤回 {delete_count} 条消息"
        else:
            reason = await self.recall._recall_fail_reason(event, target)
            yield reason

    @filter.llm_tool()
    async def llm_get_special_admins(self, event: AiocqhttpMessageEvent):
        """查看当前群的特殊权限名单，名单内成员可执行所有群管指令。
        Args:
        """
        if event.platform_meta.name != "aiocqhttp":
            yield "该查询工具需要 NapCat / OneBot 平台。"
            return
        if event.is_private_chat():
            yield "该查询工具仅支持在 QQ 群聊中调用。"
            return
        gid = event.get_group_id()
        admins = await self.db.get(gid, "special_admins", [])
        admins = [str(a) for a in (admins or [])]
        if admins:
            yield (
                f"当前群特殊权限名单（{len(admins)} 人）：\n"
                + "、".join(admins)
                + "\n名单内成员可执行所有群管指令。"
            )
        else:
            yield "当前群特殊权限名单为空。名单内成员可执行所有群管指令；可通过「特殊权限 添加 @成员」管理（仅超管）。"
