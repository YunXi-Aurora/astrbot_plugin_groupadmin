"""
权限体系（名单制）：
- 超管 = AstrBot admins_id，一切权限 + 名单管理 + 群管配置/重置
- 特殊权限名单成员（每群独立，DB 字段 special_admins）= 所有群管指令
- QQ 群主/管理员（群身份）= 基础管理指令 + 查询指令
- 普通成员 = 仅查询类
- 名单成员无保护，可被 bot 禁言/踢出（check_at 按真实群身份比较）
- bot 自身群管理员身份检查保留
"""
import inspect
from collections.abc import AsyncGenerator, Awaitable, Callable
from enum import IntEnum
from functools import wraps
from typing import Any, cast

from astrbot import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .config import PluginConfig
from .data import QQAdminDB
from .utils import get_ats
from .official import is_official


class PermLevel(IntEnum):
    """
    Bot 自身权限等级（数值越小越高）。
    仅用于检查 bot 在群内的身份，与用户权限分级无关。
    """

    BOT_OWNER = 1
    BOT_ADMIN = 2
    UNKNOWN = 5

    def __str__(self):
        return {
            PermLevel.BOT_OWNER: "群主权限",
            PermLevel.BOT_ADMIN: "管理员权限",
            PermLevel.UNKNOWN: "未知/无权限",
        }.get(self, "未知/无权限")


# ====================== 身份判定 ======================


def is_superuser(cfg: PluginConfig | None, uid: str | int) -> bool:
    """是否为 AstrBot 超管（admins_id）"""
    return cfg is not None and str(uid) in cfg.admins_id


async def is_special_admin(db: QQAdminDB | None, gid: str, uid: str | int) -> bool:
    """是否为本群特殊权限名单成员（QQ号与官方 OpenID 同列表）"""
    if db is None:
        return False
    try:
        admins = await db.get(gid, "special_admins", [])
    except Exception:
        return False
    return str(uid) in [str(a) for a in (admins or [])]


async def is_group_admin(event: AiocqhttpMessageEvent, uid: str | int) -> bool:
    """uid 在当前群是否为群主/管理员（OneBot 真实群身份）；失败返回 False"""
    try:
        group_id = int(event.get_group_id())
        user_id = int(uid)
    except (TypeError, ValueError):
        return False
    if group_id == 0 or user_id == 0:
        return False
    try:
        info = await event.bot.get_group_member_info(
            group_id=group_id, user_id=user_id, no_cache=True
        )
    except Exception:
        return False
    return info.get("role") in {"owner", "admin"}


class PermissionManager:
    _initialized = False

    def __init__(self):
        self.cfg: PluginConfig | None = None
        self.db: QQAdminDB | None = None

    def lazy_init(self, config: PluginConfig, db: QQAdminDB):
        if self._initialized:
            raise RuntimeError("PermissionManager already initialized")
        self.cfg = config
        self.db = db
        self._initialized = True

    def refresh(self, config: PluginConfig, db: QQAdminDB | None = None):
        self.cfg = config
        if db is not None:
            self.db = db
        self._initialized = True

    # ====================== bot 自身检查 ======================

    async def get_bot_level(self, event: AiocqhttpMessageEvent) -> PermLevel:
        """查询 bot 在当前群的自身身份"""
        try:
            group_id = int(event.get_group_id())
            user_id = int(event.get_self_id())
        except (TypeError, ValueError):
            return PermLevel.UNKNOWN
        if group_id == 0 or user_id == 0:
            return PermLevel.UNKNOWN
        try:
            info = await event.bot.get_group_member_info(
                group_id=group_id, user_id=user_id, no_cache=True
            )
        except Exception:
            return PermLevel.UNKNOWN
        role = info.get("role", "unknown")
        match role:
            case "owner":
                return PermLevel.BOT_OWNER
            case "admin":
                return PermLevel.BOT_ADMIN
            case _:
                return PermLevel.UNKNOWN

    async def check_bot_perm(
        self, event: AiocqhttpMessageEvent, bot_perm: PermLevel
    ) -> str | None:
        """检查 bot 自身是否有权限执行操作"""
        bot_level = await self.get_bot_level(event)
        if bot_level > bot_perm:
            return f"我没{bot_perm}权限"
        return None

    # ====================== 用户身份 ======================

    async def is_user_superuser(
        self, event: AiocqhttpMessageEvent, uid: str | int
    ) -> bool:
        return is_superuser(self.cfg, uid)

    async def is_user_special_admin(
        self, event: AiocqhttpMessageEvent, uid: str | int
    ) -> bool:
        return await is_special_admin(self.db, event.get_group_id(), uid)

    async def is_user_group_admin(
        self, event: AiocqhttpMessageEvent, uid: str | int
    ) -> bool:
        return await is_group_admin(event, uid)

    async def is_user_authorized_admin(
        self, event: AiocqhttpMessageEvent, uid: str | int
    ) -> bool:
        """超管 ∪ 特殊权限名单 ∪ 群主/管理员"""
        return (
            await self.is_user_superuser(event, uid)
            or await self.is_user_special_admin(event, uid)
            or await self.is_user_group_admin(event, uid)
        )

    # ====================== check_at ======================

    async def check_at_protection(self, event: AiocqhttpMessageEvent) -> str | None:
        """
        检查 bot 是否有权对被@者实施操作，按真实群身份比较：
        - 被@者是群主 → 无法操作
        - 被@者是群管理员 → bot 至少同级（群主动管理员）
        - 被@者是普通成员/名单成员 → 无保护，bot 管理员即可
        """
        bot_level = await self.get_bot_level(event)
        if bot_level == PermLevel.UNKNOWN:
            return "我没管理员权限"
        for at_id in get_ats(event):
            try:
                group_id = int(event.get_group_id())
                user_id = int(at_id)
            except (TypeError, ValueError):
                continue
            if group_id == 0 or user_id == 0:
                continue
            try:
                info = await event.bot.get_group_member_info(
                    group_id=group_id, user_id=user_id, no_cache=True
                )
            except Exception:
                continue
            role = info.get("role", "unknown")
            if role == "owner":
                return "我动不了群主"
            if role == "admin":
                at_level = PermLevel.BOT_ADMIN
                if bot_level >= at_level:
                    return f"我动不了{at_level}"
        return None

    async def user_block(
        self,
        event: AiocqhttpMessageEvent,
        require_superuser_only: bool,
        bot_perm: PermLevel,
        check_at: bool,
    ) -> str | None:
        """用户身份检查 + bot 自身检查 + check_at，返回拒绝文案或 None"""
        sender_id = event.get_sender_id()
        if require_superuser_only:
            if not await self.is_user_superuser(event, sender_id):
                return "你没超管权限"
        else:
            if not await self.is_user_authorized_admin(event, sender_id):
                return "你没管理员权限"

        if error := await self.check_bot_perm(event, bot_perm):
            return error

        if check_at:
            if error := await self.check_at_protection(event):
                return error
        return None

    # ====================== LLM 工具鉴权 ======================

    async def llm_perm_block(
        self,
        event: AiocqhttpMessageEvent,
        perm_key: str = "",
        bot_perm: PermLevel = PermLevel.BOT_ADMIN,
    ) -> str | None:
        """
        LLM 工具鉴权：要求 超管 ∪ 名单 ∪ 群主/管理员。
        perm_key 仅为日志与调用方兼容保留，不再参与分级。
        """
        if event.platform_meta.name != "aiocqhttp":
            return "该群管理工具需要 NapCat / OneBot；QQ 官方机器人群聊不提供此管理接口。"

        if event.is_private_chat():
            return "该工具仅支持在 QQ 群聊中调用"

        if not self._initialized:
            logger.error(
                "PermissionManager is not initialized while checking LLM tool permission: %s",
                perm_key,
            )
            return "内部错误：权限系统尚未正确加载"

        sender_id = event.get_sender_id()
        if not await self.is_user_authorized_admin(event, sender_id):
            return "你没管理员权限"

        if error := await self.check_bot_perm(event, bot_perm):
            return error
        return None


perm_manager = PermissionManager()


# ====================== 装饰器三件套 ======================


def _make_guard(
    require_superuser_only: bool | None,
    bot_perm: PermLevel,
    check_at: bool,
):
    """
    通用守卫装饰器工厂，生成与原 perm_required 兼容的 generator 装饰器：
    - 官方平台：转调 plugin_instance.official.command(...)
    - 平台限制：仅 aiocqhttp；QQ 官方机器人给提示并拦截
    - 群聊限制：私聊静默返回
    - 权限未初始化：提示内部错误
    - require_superuser_only=None 表示 require_member：跳过身份与 bot 检查
    """

    def decorator(
        func: Callable[..., AsyncGenerator[Any, Any] | Awaitable[Any]],
    ) -> Callable[..., AsyncGenerator[Any, Any]]:
        @wraps(func)
        async def wrapper(
            plugin_instance: Any,
            event: AiocqhttpMessageEvent,
            *args: Any,
            **kwargs: Any,
        ) -> AsyncGenerator[Any, Any]:

            # 官方平台分支：转调官方接口实现
            if is_official(event):
                bound = inspect.signature(func).bind(
                    plugin_instance, event, *args, **kwargs
                )
                bound.apply_defaults()
                yield event.plain_result(
                    await plugin_instance.official.command(
                        func.__name__, event, dict(bound.arguments)
                    )
                )
                event.stop_event()
                return

            # 仅限 aiocqhttp
            if event.platform_meta.name != "aiocqhttp":
                if event.platform_meta.name in {"qq_official", "qq_official_webhook"}:
                    yield event.plain_result(
                        "该群管理功能需要 NapCat / OneBot；QQ 官方机器人群聊不提供此管理接口。"
                    )
                    event.stop_event()
                return

            # 仅限群聊
            if event.is_private_chat():
                return

            # 权限管理未初始化
            if not perm_manager._initialized:
                logger.error(f"PermissionManager 未初始化（尝试访问：{func.__name__}）")
                yield event.plain_result("内部错误：权限系统未正确加载")
                event.stop_event()
                return

            # 身份判定（require_member 级跳过身份与 bot 检查）
            if require_superuser_only is not None:
                result = await perm_manager.user_block(
                    event,
                    require_superuser_only=require_superuser_only,
                    bot_perm=bot_perm,
                    check_at=check_at,
                )
                if result:
                    yield event.plain_result(result)
                    event.stop_event()
                    return

            # 执行原始方法
            if inspect.isasyncgenfunction(func):
                async for item in func(plugin_instance, event, *args, **kwargs):
                    yield item
            else:
                await cast(
                    Awaitable[Any], func(plugin_instance, event, *args, **kwargs)
                )

        return wrapper

    return decorator


def require_superuser(
    bot_perm: PermLevel = PermLevel.BOT_ADMIN,
    check_at: bool = True,
):
    """仅超管可用（名单管理、群管配置/重置等）"""
    return _make_guard(
        require_superuser_only=True, bot_perm=bot_perm, check_at=check_at
    )


def require_admin(
    bot_perm: PermLevel = PermLevel.BOT_ADMIN,
    check_at: bool = True,
):
    """
    超管 ∪ 特殊权限名单 ∪ 群主/管理员 可用。
    check_at 对被@者按真实群身份比较（名单成员不豁免）。
    """
    return _make_guard(
        require_superuser_only=False, bot_perm=bot_perm, check_at=check_at
    )


def require_member():
    """全员可用（登录用户即可），仅保留平台限制与群聊限制"""
    return _make_guard(
        require_superuser_only=None, bot_perm=PermLevel.UNKNOWN, check_at=False
    )