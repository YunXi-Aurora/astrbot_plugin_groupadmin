import os
from datetime import datetime
from pathlib import Path

from aiohttp import ClientSession

from astrbot import logger
from astrbot.core.message.components import At, BaseMessageComponent, Image, Reply
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


async def get_nickname(event: AiocqhttpMessageEvent, user_id: int | str) -> str:
    """获取指定群友的群昵称或 Q 名，群接口失败/空结果自动降级到陌生人资料"""
    user_id = int(user_id)
    client = event.bot
    group_id = event.get_group_id()
    info = {}

    # 在群里就先试群资料，任何异常或空结果都跳过
    if group_id.isdigit():
        try:
            info = (
                await client.get_group_member_info(
                    group_id=int(group_id), user_id=user_id
                )
                or {}
            )
        except Exception:
            pass

    # 群资料没拿到就降级到陌生人资料
    if not info:
        try:
            info = await client.get_stranger_info(user_id=user_id) or {}
        except Exception:
            pass

    # 依次取群名片、QQ 昵称、通用 nick，兜底数字 UID
    return info.get("card") or info.get("nickname") or info.get("nick") or str(user_id)


def get_ats(event: AiocqhttpMessageEvent) -> list[str]:
    """获取被at者们的id列表"""
    return [
        str(seg.qq)
        for seg in event.get_messages()
        if (isinstance(seg, At) and str(seg.qq) != event.get_self_id())
    ]


def get_replyer_id(event: AiocqhttpMessageEvent) -> str | None:
    """获取被引用消息者的id"""
    for seg in event.get_messages():
        if isinstance(seg, Reply):
            return str(seg.sender_id)


def get_reply_message_str(event: AiocqhttpMessageEvent) -> str | None:
    """
    获取被引用的消息解析后的纯文本消息字符串。
    """
    return next(
        (
            seg.message_str
            for seg in event.message_obj.message
            if isinstance(seg, Reply)
        ),
        "",
    )


def format_time(timestamp):
    """格式化时间戳"""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")


async def download_file(url: str, save_path: Path) -> Path | None:
    """下载文件并保存到本地"""
    url = url.replace("https://", "http://")
    try:
        async with ClientSession() as client:
            response = await client.get(url)
            file = await response.read()

            os.makedirs(os.path.dirname(save_path), exist_ok=True)

            with open(save_path, "wb") as img_file:
                img_file.write(file)

            logger.info(f"文件已保存: {save_path}")
            return save_path
    except Exception as e:
        logger.error(f"文件下载并保存失败: {e}")
        return None


def extract_image_url(chain: list[BaseMessageComponent]) -> str | None:
    """从消息链中提取图片URL"""
    for seg in chain:
        if isinstance(seg, Image):
            return seg.url
        elif isinstance(seg, Reply) and seg.chain:
            for reply_seg in seg.chain:
                if isinstance(reply_seg, Image):
                    return reply_seg.url
    return None


def parse_bool(mode: str | bool | None, default: bool = False):
    """解析布尔值"""
    mode = str(mode).strip().lower()
    match mode:
        case "开" | "开启" | "启用" | "on" | "true" | "1" | "是" | "真":
            return True
        case "关" | "关闭" | "禁用" | "off" | "false" | "0" | "否" | "假":
            return False
        case _:
            return default


# 时间单位 -> 秒（按长度降序匹配，避免"分钟"被"分"截断）
_TIME_UNITS: tuple[tuple[str, int], ...] = (
    ("周", 604800),
    ("星期", 604800),
    ("week", 604800),
    ("w", 604800),
    ("天", 86400),
    ("日", 86400),
    ("day", 86400),
    ("d", 86400),
    ("小时", 3600),
    ("時", 3600),
    ("hour", 3600),
    ("h", 3600),
    ("分钟", 60),
    ("分鐘", 60),
    ("min", 60),
    ("minute", 60),
    ("分", 60),
    ("秒", 1),
    ("sec", 1),
    ("second", 1),
    ("s", 1),
)


def parse_duration(text: str | int | float | None) -> int | None:
    """
    解析时长字符串为秒数，支持单位换算：
    纯数字按秒；"1分钟"/"1min"/"60s"/"60秒"/"1h30m"/"1时30分" 均换算为秒。
    无法解析返回 None。
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    s = str(text).strip().lower().replace(" ", "")
    if not s:
        return None
    # 纯数字直接按秒
    if s.isdigit():
        return int(s)
    total = 0
    i = 0
    matched_any = False
    while i < len(s):
        # 取数字部分
        num_start = i
        while i < len(s) and (s[i].isdigit() or s[i] == "."):
            i += 1
        if num_start == i:
            return None  # 期望数字但遇到其它字符
        num_str = s[num_start:i]
        try:
            num = float(num_str)
        except ValueError:
            return None
        # 取单位部分
        unit_matched = False
        for unit, secs in _TIME_UNITS:
            if s.startswith(unit, i):
                total += num * secs
                i += len(unit)
                unit_matched = True
                matched_any = True
                break
        if not unit_matched:
            # 无单位则默认秒（仅当已是末尾，如 "60"）
            if i == len(s):
                total += num
                matched_any = True
                break
            return None
    return int(total) if matched_any else None


def format_duration(seconds: int) -> str:
    """把秒数格式化为人类可读时长，如 3660 -> 1小时1分钟"""
    if seconds <= 0:
        return f"{seconds}秒"
    parts: list[str] = []
    units = (("周", 604800), ("天", 86400), ("小时", 3600), ("分钟", 60), ("秒", 1))
    for name, secs in units:
        n, seconds = divmod(seconds, secs)
        if n:
            parts.append(f"{int(n)}{name}")
    return "".join(parts)


def split_list_arg(value: str | list | tuple | None) -> list[str]:
    """把 "a,b,c" / "a、b、c" / "a b" / list 统一拆成去空白的字符串列表"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw = [str(v) for v in value]
    else:
        raw = str(value).replace("，", ",").replace("、", ",").split(",")
    return [p.strip() for p in raw if p.strip()]


def pair_targets_and_times(
    targets: str | list | tuple | None,
    durations: str | list | tuple | None,
    default_duration=None,
) -> tuple[list[tuple[str, object]], str | None]:
    """
    把「目标列表」和「时长列表」按位置配对。

    规则：
    - 两个列表长度相同 -> 一一对应。
    - 时长只有一个 -> 所有目标共用该时长（常见场景：一批人同禁言 1 小时）。
    - 时长多于目标 -> 忽略多余时长。
    - 目标多于时长 -> 返回错误，避免静默按错时长禁言。

    返回 (pairs, error)：error 非 None 时调用方应直接返回该错误文案。
    """
    tids = split_list_arg(targets)
    if not tids:
        return [], None
    times = split_list_arg(durations)
    if not times:
        # 没给时长：全部使用默认值（随机或调用方指定）
        return [(tid, default_duration) for tid in tids], None
    if len(times) == 1:
        return [(tid, times[0]) for tid in tids], None
    if len(times) < len(tids):
        return [], (
            f"禁言时长数量（{len(times)}）少于目标数量（{len(tids)}），"
            "无法一一对应。请补齐时长，或只填一个时长让所有目标共用。"
        )
    return list(zip(tids, times)), None

_UNIT_CHARS = "秒时分天日周星期s mhdw".replace(" ", "")

# 娱乐式禁言的逐人开关词：玩 = 下发后立刻解禁，真 = 正常禁言
_INSTANT_TRUE_WORDS = frozenset({"玩", "假", "吓", "吓唬", "玩笑", "娱乐"})
_INSTANT_FALSE_WORDS = frozenset({"真", "正", "实", "正常", "实禁"})


def parse_ban_args(raw: str) -> tuple[list[str], list[str], list[bool]]:
    """
    从命令尾部原文里拆出「目标 QQ 号列表」「时长列表」「逐人娱乐式开关」。

    规则（QQ 号一律用纯数字，时长最好带单位）：
    - 带单位的 token（如 1h / 30分 / 2天）一律当作时长；
    - 纯数字 token：若已存在带单位时长，则当作 QQ 号；
    - 全是纯数字时，最后一个当时间，其余当 QQ 号（保留 `禁言 60` 的原义）；
    - 玩 / 假 / 吓 视为「下发后立刻解禁」，真 / 正 / 实 视为「正常禁言」，
      按出现顺序与目标一一对应；出现真假词时纯数字一律视为 QQ 号。

    例：
    - "123456,789012 1h"                -> ids=[123456,789012], times=[1h], flags=[]
    - "123456,789012 1h,2h"             -> ids=[123456,789012], times=[1h,2h], flags=[]
    - "123456,789012 1h,2h 玩,真"       -> flags=[True, False]
    - "123456 3600"                     -> ids=[123456], times=[3600]
    - "3600"                            -> ids=[], times=[3600]
    """
    parts: list[str] = []
    for tok in str(raw or "").split():
        parts.extend(split_list_arg(tok))
    if not parts:
        return [], [], []

    flagged: list[tuple[str, object]] = []
    for p in parts:
        low = p.lower()
        if low in _INSTANT_TRUE_WORDS:
            flagged.append(("flag", True))
            continue
        if low in _INSTANT_FALSE_WORDS:
            flagged.append(("flag", False))
            continue
        has_unit = any(c in low for c in _UNIT_CHARS)
        if has_unit and parse_duration(p) is not None:
            flagged.append(("time", p))
        elif p.isdigit():
            flagged.append(("digit", p))
        elif parse_duration(p) is not None:
            flagged.append(("time", p))
        else:
            flagged.append(("other", p))

    flags = [v for k, v in flagged if k == "flag"]
    has_unit_time = any(k == "time" for k, _ in flagged)
    times = [v for k, v in flagged if k == "time"]
    if has_unit_time:
        ids = [v for k, v in flagged if k == "digit"]
        return ids, times, flags

    digits = [v for k, v in flagged if k == "digit"]
    # 出现真假词时，纯数字一律视为 QQ 号（如 "123 玩"），时长走默认
    if flags:
        return digits, [], flags
    if len(digits) <= 1:
        return [], digits, flags
    return digits[:-1], digits[-1:], flags
