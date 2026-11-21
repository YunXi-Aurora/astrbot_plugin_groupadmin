"""回归测试：引用踢出（/踢了 引用消息）的行为。

覆盖：
- 引用消息踢出：被引用者作为目标、被引用消息作为踢出原因、留存记录（含聊天记录补充）
- 引用但拿不到被引用者 id（sender_id=0）时按"未指定要踢出的用户"处理
- 聊天记录补充读取失败时异常如实穿透，且不会踢出（先读后踢的顺序保证）
- 踢出 API 失败时异常如实穿透，不留记录
- @用户 与引用并存时 @优先（沿用既有语义），不留引用踢出记录
- 踢出原因查询文本生成（按目标过滤 / 无记录）

本测试在无 AstrBot/aiohttp 的工作站上也可运行（自动 stub 导入）。
"""

import asyncio
import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

PLUGIN_ROOT = Path(__file__).parents[1]

# ---- source-only 模式：stub 掉 AstrBot / aiohttp 依赖（与 test_llm_notice_ban 相同） ----
if importlib.util.find_spec("astrbot") is None:
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    astrbot.logger = logging.getLogger("test")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("test")
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
if importlib.util.find_spec("aiohttp") is None:
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = type("ClientError", (Exception,), {})
    aiohttp.ClientTimeout = lambda **kw: kw

    class _FakeClientSession:  # utils.py 顶层 import 需要，不会被真正调用
        pass

    aiohttp.ClientSession = _FakeClientSession
    sys.modules["aiohttp"] = aiohttp

_components = types.ModuleType("astrbot.core.message.components")


class _Comp:
    def __init__(self, **kw):
        self.__dict__.update(kw)


_components.Reply = type("Reply", (_Comp,), {})
_components.Image = type("Image", (_Comp,), {})
_components.At = type("At", (_Comp,), {})
_components.BaseMessageComponent = type("BaseMessageComponent", (), {})
_aiocq = types.ModuleType(
    "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
)
_aiocq.AiocqhttpMessageEvent = type("AiocqhttpMessageEvent", (), {})
sys.modules["astrbot.core"] = sys.modules.get("astrbot.core") or types.ModuleType(
    "astrbot.core"
)
sys.modules["astrbot.core"].__path__ = []
sys.modules["astrbot.core.message"] = types.ModuleType("astrbot.core.message")
sys.modules["astrbot.core.message.components"] = _components
sys.modules[
    "astrbot.core.platform"
] = sys.modules.get("astrbot.core.platform") or types.ModuleType(
    "astrbot.core.platform"
)
sys.modules["astrbot.core.platform"].__path__ = []
sys.modules[
    "astrbot.core.platform.sources"
] = types.ModuleType("astrbot.core.platform.sources")
sys.modules[
    "astrbot.core.platform.sources.aiocqhttp"
] = types.ModuleType("astrbot.core.platform.sources.aiocqhttp")
sys.modules[
    "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event"
] = _aiocq

_pkg = types.ModuleType("astrbot_plugin_groupadmin")
_pkg.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault("astrbot_plugin_groupadmin", _pkg)

_config = types.ModuleType("astrbot_plugin_groupadmin.config")
_config.PluginConfig = type("PluginConfig", (), {})
sys.modules.setdefault("astrbot_plugin_groupadmin.config", _config)

_utils_spec = importlib.util.spec_from_file_location(
    "astrbot_plugin_groupadmin.utils", PLUGIN_ROOT / "utils.py"
)
_utils = importlib.util.module_from_spec(_utils_spec)
_utils.__package__ = "astrbot_plugin_groupadmin"
sys.modules["astrbot_plugin_groupadmin.utils"] = _utils
_utils_spec.loader.exec_module(_utils)

_data = types.ModuleType("astrbot_plugin_groupadmin.data")
_data.QQAdminDB = type("QQAdminDB", (), {})
sys.modules["astrbot_plugin_groupadmin.data"] = _data

_core_pkg = types.ModuleType("astrbot_plugin_groupadmin.core")
_core_pkg.__path__ = [str(PLUGIN_ROOT / "core")]
sys.modules["astrbot_plugin_groupadmin.core"] = _core_pkg


def _load(modname: str, path: Path):
    spec = importlib.util.spec_from_file_location(
        f"astrbot_plugin_groupadmin.core.{modname}", path
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "astrbot_plugin_groupadmin.core"
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


from astrbot.core.message.components import (  # noqa: E402
    At,
    Reply,
)
normal_handle = _load("normal_handle", PLUGIN_ROOT / "core" / "normal_handle.py")


class FakeDB:
    def __init__(self):
        self.store = {}

    async def get(self, gid, field, default=None):
        return self.store.get((gid, field), default)

    async def set(self, gid, field, value):
        self.store[(gid, field)] = value


class FakeBot:
    def __init__(self, history=None, kick_error=None, history_error=None):
        self.kick_calls = []
        self.history_calls = []
        self.kick_error = kick_error
        self.history_error = history_error
        self._history = history or []
        self.api = NS(call_action=self._call_action)

    async def _call_action(self, action, **payload):
        self.history_calls.append((action, payload))
        if self.history_error:
            raise self.history_error
        return {"messages": self._history}

    async def set_group_kick(self, **payload):
        self.kick_calls.append(payload)
        if self.kick_error:
            raise self.kick_error

    async def get_group_member_info(self, group_id, user_id, no_cache=False):
        return {"nickname": f"成员{user_id}", "user_id": user_id}

    async def get_stranger_info(self, user_id):
        return {"nickname": f"陌生人{user_id}", "user_id": user_id}


def make_event(segs, sender_id="10001", group_id="67890"):
    message = list(segs)
    return NS(
        bot=None,  # 由用例注入
        message_obj=NS(message=message),
        get_messages=lambda: list(message),
        get_group_id=lambda: group_id,
        get_sender_id=lambda: sender_id,
        get_self_id=lambda: "10000",
    )


def history_entry(user_id, nickname, text):
    return {
        "message_id": 1,
        "sender": {"user_id": user_id, "nickname": nickname},
        "message": [{"type": "text", "data": {"text": text}}],
    }


class TestQuoteKick(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()

    def _handle(self):
        return normal_handle.NormalHandle(None, self.db)

    def test_quote_kick_kicks_replyer_with_reason_and_record(self):
        """引用踢出：踢被引用者，原因=被引用消息，记录含聊天记录补充"""
        handle = self._handle()
        event = make_event([Reply(sender_id=12345, message_str="发广告刷屏")])
        bot = FakeBot(
            history=[
                # reverseOrder=True：最新在前（与 NapCat 返回顺序一致）
                history_entry("12345", "捣蛋鬼", "发广告刷屏"),
                history_entry("12345", "捣蛋鬼", "买号加我"),
                history_entry("999", "路人", "无关消息"),
            ]
        )
        event.bot = bot

        result = asyncio.run(handle.set_group_kick(event))

        self.assertEqual(len(bot.kick_calls), 1)
        self.assertEqual(bot.kick_calls[0]["user_id"], 12345)
        self.assertFalse(bot.kick_calls[0]["reject_add_request"])
        self.assertIn("已将【12345-成员12345】踢出本群", result)
        self.assertIn("踢出原因：发广告刷屏", result)

        records = self.db.store[("67890", "kick_records")]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["target_id"], "12345")
        self.assertEqual(record["reason"], "发广告刷屏")
        self.assertEqual(record["operator_id"], "10001")
        # 聊天记录补充只含被踢者的文本消息（时间正序，不含路人）
        self.assertEqual(
            record["context"], ["捣蛋鬼: 买号加我", "捣蛋鬼: 发广告刷屏"]
        )
        # get_group_msg_history payload 与 recall 既有调用一致
        action, payload = bot.history_calls[0]
        self.assertEqual(action, "get_group_msg_history")
        self.assertEqual(
            payload,
            {"group_id": 67890, "message_seq": 0, "count": 50, "reverseOrder": True},
        )

    def test_quote_without_replyer_id_falls_back_to_no_target(self):
        """引用消息拿不到被引用者 id（sender_id=0）时按未指定处理，不踢人"""
        handle = self._handle()
        event = make_event([Reply(sender_id=0, message_str="内容")])
        bot = FakeBot()
        event.bot = bot

        result = asyncio.run(handle.set_group_kick(event))

        self.assertEqual(result, "未指定要踢出的用户")
        self.assertEqual(bot.kick_calls, [])
        self.assertNotIn(("67890", "kick_records"), self.db.store)

    def test_at_takes_priority_over_quote(self):
        """@用户 与引用并存时 @优先（既有语义），不留引用踢出记录"""
        handle = self._handle()
        event = make_event(
            [At(qq=777), Reply(sender_id=12345, message_str="被引用的话")]
        )
        bot = FakeBot()
        event.bot = bot

        result = asyncio.run(handle.set_group_kick(event))

        self.assertEqual(len(bot.kick_calls), 1)
        self.assertEqual(bot.kick_calls[0]["user_id"], 777)
        self.assertEqual(result, "已将【777-成员777】踢出本群")
        self.assertNotIn(("67890", "kick_records"), self.db.store)

    def test_history_failure_propagates_and_no_kick(self):
        """聊天记录补充读取失败：异常如实穿透，且先读后踢保证没有人被踢出"""
        handle = self._handle()
        event = make_event([Reply(sender_id=12345, message_str="刷屏")])
        bot = FakeBot(history_error=RuntimeError("历史消息接口返回 500"))
        event.bot = bot

        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(handle.set_group_kick(event))

        self.assertIn("历史消息接口返回 500", str(ctx.exception))
        self.assertEqual(bot.kick_calls, [])
        self.assertNotIn(("67890", "kick_records"), self.db.store)

    def test_kick_failure_propagates_without_record(self):
        """踢出 API 失败：异常如实穿透，不留记录"""
        handle = self._handle()
        event = make_event([Reply(sender_id=12345, message_str="刷屏")])
        bot = FakeBot(kick_error=RuntimeError("机器人不是管理员"))
        event.bot = bot

        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(handle.set_group_kick(event))

        self.assertIn("机器人不是管理员", str(ctx.exception))
        self.assertNotIn(("67890", "kick_records"), self.db.store)

    def test_get_kick_reason_text(self):
        """踢出原因查询：按目标过滤；无记录时如实说明"""
        handle = self._handle()
        self.db.store[("67890", "kick_records")] = [
            {
                "time": "2026-11-21 20:00:00",
                "operator_id": "10001",
                "operator_name": "管理员A",
                "target_id": "12345",
                "target_name": "捣蛋鬼",
                "reason": "发广告刷屏",
                "context": ["捣蛋鬼: 发广告刷屏"],
            }
        ]

        event = make_event([])
        text = asyncio.run(handle.get_kick_reason_text(event, target_id="12345"))
        self.assertIn("管理员A(10001) 踢出 捣蛋鬼(12345)", text)
        self.assertIn("踢出原因：发广告刷屏", text)
        self.assertIn("聊天记录补充", text)

        miss = asyncio.run(handle.get_kick_reason_text(event, target_id="99999"))
        self.assertEqual(miss, "本群暂无【99999】的引用踢出记录")

        self.db.store.clear()
        empty = asyncio.run(handle.get_kick_reason_text(event))
        self.assertEqual(empty, "本群暂无引用踢出记录")


if __name__ == "__main__":
    unittest.main()
