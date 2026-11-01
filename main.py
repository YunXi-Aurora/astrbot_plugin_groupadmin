import asyncio
import re

from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.filter.event_message_type import EventMessageType

from .config import PluginConfig
from .core import (
    BanproHandle,
    CurfewHandle,
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
from .utils import get_ats, parse_bool
from .official import OfficialHandle, HELP, is_official, official_tool
from .web import QQAdminWebController


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
        self.banpro = BanproHandle(self.cfg, self.db)
        self.join = JoinHandle(self.cfg, self.db)
        self.member = MemberHandle(self)
        self.file = FileHandle(self.cfg)
        self.curfew = CurfewHandle(self.context, self.cfg)
        self.web = QQAdminWebController(context, self.cfg, self.db, self.group_cache)
        self.web.register_routes()

    async def initialize(self):
        await self.db.init()
        asyncio.create_task(self.curfew.initialize())
        perm_manager.lazy_init(self.cfg, self.db)

    async def terminate(self):
        await self.curfew.stop_all_tasks()
        await self.db.close()

    @filter.on_platform_loaded()
    async def on_platform_loaded(self):
        """平台加载完成时"""
        if not self.curfew.curfew_managers:
            asyncio.create_task(self.curfew.initialize())

    @filter.command("群管帮助", alias={"群管菜单"})
    async def official_help(self, event: AiocqhttpMessageEvent):
        if is_official(event):
            yield event.plain_result(HELP)
            event.stop_event()

    @filter.command("群管状态")
    async def official_status(self, event: AiocqhttpMessageEvent):
        if is_official(event):
            yield event.plain_result('正在检查本群的官方接口权限，请稍等～')
            yield event.plain_result(await self.official.run('status', event))
            event.stop_event()

    @filter.command("禁言状态")
    async def official_mutes(self, event: AiocqhttpMessageEvent):
        if is_official(event):
            yield event.plain_result(await self.official.run('mutes', event))
            event.stop_event()

    @filter.command("入群申请")
    async def official_requests(self, event: AiocqhttpMessageEvent):
        if is_official(event):
            yield event.plain_result(await self.official.run('requests', event))
            event.stop_event()

    @filter.command("群黑名单")
    async def official_blacklist(self, event: AiocqhttpMessageEvent):
        if is_official(event):
            yield event.plain_result(await self.official.run('blacklist', event))
            event.stop_event()

    @filter.command("解除群拉黑")
    async def official_unblock(self, event: AiocqhttpMessageEvent, member_id: str = ''):
        if is_official(event):
            yield event.plain_result(await self.official.run('unblock', event, {'member_id': member_id}))
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
    async def set_group_ban(self, event: AiocqhttpMessageEvent, ban_time=None):
        """禁言 @群友 时间，如：禁言 @某人 1分钟 / 禁言 @某人 60s / 禁言 @某人 90"""
        if result := await self.normal.set_group_ban(event, ban_time):
            yield event.plain_result(result)

    @filter.command("解禁")
    @require_admin()
    async def cancel_group_ban(self, event: AiocqhttpMessageEvent):
        """解禁 @群友"""
        if result := await self.normal.set_group_ban(event, ban_time=0):
            yield event.plain_result(result)

    @filter.command("全禁", alias={"全员禁言", "全员禁言"})
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

    @filter.command("禁词禁言")
    @require_admin()
    async def handle_word_ban_time(
        self, event: AiocqhttpMessageEvent, time: int | None = None
    ):
        """禁词禁言 <秒数>, 设为 0 表示关闭禁词检测"""
        await self.banpro.handle_word_ban_time(event, time)

    @filter.command("设置禁词", alias={"禁词", "违禁词"})
    @require_admin()
    async def handle_builtin_ban_words(self, event: AiocqhttpMessageEvent):
        """禁词 +词1 -词2, 带+-则增删, 不带则覆写"""
        await self.banpro.handle_ban_words(event)

    @filter.command("内置禁词")
    @require_admin()
    async def handle_ban_words(
        self, event: AiocqhttpMessageEvent, mode: str | bool | None = None
    ):
        """内置禁词 开/关"""
        await self.banpro.handle_builtin_ban_words(event, mode)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_ban_words(self, event: AiocqhttpMessageEvent):
        """自动检测违禁词，撤回并禁言"""
        if not event.is_admin():
            await self.banpro.on_ban_words(event)

    @filter.command("刷屏禁言")
    @require_admin()
    async def handle_spamming_ban_time(
        self, event: AiocqhttpMessageEvent, time: int | None = None
    ):
        """刷屏禁言 <秒数>, 设为 0 表示关闭禁词检测"""
        await self.banpro.handle_spamming_ban_time(event, time)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def spamming_ban(self, event: AiocqhttpMessageEvent):
        """刷屏检测与禁言"""
        await self.banpro.spamming_ban(event)

    @filter.command("开启宵禁")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @require_admin()
    async def start_curfew(
        self,
        event: AiocqhttpMessageEvent,
        start_time: str | None = None,
        end_time: str | None = None,
    ):
        """开启宵禁 HH:MM HH:MM"""
        if result := await self.curfew.start_curfew(event, start_time, end_time):
            yield event.plain_result(result)

    @filter.command("关闭宵禁")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @require_admin()
    async def stop_curfew(self, event: AiocqhttpMessageEvent):
        """关闭本群的宵禁任务"""
        if result := await self.curfew.stop_curfew(event):
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
        duration: int,
        need_auth: bool = True,
        instant_unban: bool = False,
    ):
        """
        在群聊中禁言某用户，被禁言的用户在禁言期间将无法发送消息。instant_unban为True时为娱乐式禁言：下发禁言后立刻解禁，只吓唬一下不禁真。
        Args:
            user_id(string): OneBot用户QQ号或官方当前群的成员OpenID，不得使用私聊UID
            duration(number): 禁言持续时间（秒），支持30天内的任意时长（2592000秒），0表示取消禁言
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True
            instant_unban(boolean): 娱乐式禁言，默认False；为True时下发禁言后立刻解禁（用户会看到禁言提示但随即恢复发言），适用于玩笑场景，用户明确要求"吓唬一下/禁言玩玩"或"禁言后立刻解禁"时使用
        """
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event,
                bot_perm=PermLevel.BOT_ADMIN,
            ):
                yield error
                return
        if result := await self.normal.set_group_ban(
            event, ban_time=duration, target_id=user_id, instant_unban=instant_unban
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
    async def llm_start_curfew(
        self,
        event: AiocqhttpMessageEvent,
        start_time: str,
        end_time: str,
        need_auth: bool = True,
    ):
        """
        设置当前群的宵禁时间范围。
        Args:
            start_time(string): 宵禁开始时间，格式为HH:MM。
            end_time(string): 宵禁结束时间，格式为HH:MM。
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True。
        """
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event, bot_perm=PermLevel.BOT_ADMIN
            ):
                yield error
                return
        if result := await self.curfew.start_curfew(event, start_time, end_time):
            yield result

    @filter.llm_tool()
    @official_tool
    async def llm_stop_curfew(
        self,
        event: AiocqhttpMessageEvent,
        need_auth: bool = True,
    ):
        """
        关闭当前群的宵禁。
        Args:
            need_auth(boolean): 是否要进行鉴权，机器人自行发起操作则填False, 当前用户要发起操作则填True。
        """
        if need_auth:
            if error := await perm_manager.llm_perm_block(
                event, bot_perm=PermLevel.BOT_ADMIN
            ):
                yield error
                return
        if result := await self.curfew.stop_curfew(event):
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
