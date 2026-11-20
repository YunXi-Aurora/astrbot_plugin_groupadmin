"""回归测试：LLM 工具路径下群公告/禁言的行为修复。

背景（两个叠加 bug）：
1. notice_handle.send_group_notice 的 _send_group_notice 调用在 if image_url 分支内，
   纯文字公告从不调用发布 API 却返回"群公告已发布"——LLM 被骗。
2. 底层 handler 内 event.stop_event() 会让框架 agent 中止
   （astr_agent_run_util._should_stop_agent 检查 is_stopped），导致工具执行成功
   但 LLM 没有最终回复。

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

# ---- source-only 模式：stub 掉 AstrBot / aiohttp 依赖 ----
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

# stub 消息组件与平台事件类型（notice_handle / normal_handle 的导入需要）
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
_core_msg = types.ModuleType("astrbot.core.message")
sys.modules["astrbot.core"] = sys.modules.get("astrbot.core") or types.ModuleType(
    "astrbot.core"
)
sys.modules["astrbot.core"].__path__ = []
sys.modules["astrbot.core.message"] = _core_msg
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

# notice_handle 的相对导入：..config / ..utils -> 直接提供最小替身
_pkg = types.ModuleType("astrbot_plugin_groupadmin")
_pkg.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault("astrbot_plugin_groupadmin", _pkg)


class _CfgStub:
    group_notice_dir = Path("/tmp/qqadmin_test_notice")


_config = types.ModuleType("astrbot_plugin_groupadmin.config")
_config.PluginConfig = type("PluginConfig", (), {})
sys.modules.setdefault("astrbot_plugin_groupadmin.config", _config)

# 直接加载真实的 utils.py（其依赖 aiohttp 已 stub），相对导入符号全部真实
_utils_spec = importlib.util.spec_from_file_location(
    "astrbot_plugin_groupadmin.utils", PLUGIN_ROOT / "utils.py"
)
_utils = importlib.util.module_from_spec(_utils_spec)
_utils.__package__ = "astrbot_plugin_groupadmin"
sys.modules["astrbot_plugin_groupadmin.utils"] = _utils
_utils_spec.loader.exec_module(_utils)

# normal_handle 的 ..data 依赖 aiosqlite，本测试不触库，给个占位
_data = types.ModuleType("astrbot_plugin_groupadmin.data")
_data.QQAdminDB = type("QQAdminDB", (), {})
sys.modules["astrbot_plugin_groupadmin.data"] = _data


_core_pkg = types.ModuleType("astrbot_plugin_groupadmin.core")
_core_pkg.__path__ = [str(PLUGIN_ROOT / "core")]
sys.modules["astrbot_plugin_groupadmin.core"] = _core_pkg


def _load(modname: str, path: Path):
    # 以包内全限定名加载，让 ..config / ..utils 相对导入解析到 stub
    fq = f"astrbot_plugin_groupadmin.core.{modname}"
    spec = importlib.util.spec_from_file_location(fq, path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "astrbot_plugin_groupadmin.core"
    sys.modules[fq] = mod
    spec.loader.exec_module(mod)
    return mod


notice_mod = _load(
    "notice_under_test", PLUGIN_ROOT / "core" / "notice_handle.py"
)
normal_mod = _load(
    "normal_under_test", PLUGIN_ROOT / "core" / "normal_handle.py"
)


# ---- 测试用事件/机器人替身 ----
def make_event(*, with_image: bool = False, fail_notice: bool = False):
    calls = []

    async def _send_group_notice(**kwargs):
        if fail_notice:
            raise RuntimeError("bot is not admin")
        calls.append(kwargs)

    chain = []
    if with_image:
        chain.append(_components.Image(url="http://example.com/a.png"))

    bot = NS(_send_group_notice=_send_group_notice)
    bot.calls = calls
    return NS(
        message_str="/发布群公告 测试公告内容",
        get_group_id=lambda: "12345",
        get_messages=lambda: chain,
        bot=bot,
        _stopped=False,
        stop_event=lambda: None,
    )


class NoticeHandleTests(unittest.TestCase):
    def setUp(self):
        self.plugin = NS()
        self.cfg = NS(group_notice_dir=Path("/tmp/qqadmin_test_notice"))
        self.handle = notice_mod.NoticeHandle(self.plugin, self.cfg)

    def test_text_only_notice_calls_publish_api(self):
        """Bug ①：纯文字公告必须真正调用 _send_group_notice"""
        ev = make_event()
        ret = asyncio.run(self.handle.send_group_notice(ev, content="测试公告内容"))
        self.assertEqual(len(ev.bot.calls), 1)
        self.assertEqual(ret, "群公告已发布")

    def test_notice_call_args_text_only(self):
        """纯文字路径参数正确：group_id + content，不带 image"""
        ev = make_event()
        asyncio.run(self.handle.send_group_notice(ev, content="纯文字公告"))
        # calls 记录在闭包里，通过 bot 对象属性读取
        self.assertEqual(ev.bot.calls, [{"group_id": 12345, "content": "纯文字公告"}])

    def test_notice_api_failure_reported(self):
        """API 失败时返回真实错误，而不是谎报已发布"""
        ev = make_event(fail_notice=True)
        ret = asyncio.run(self.handle.send_group_notice(ev, content="测试"))
        self.assertIn("群公告发布失败", ret)
        self.assertIn("bot is not admin", ret)

    def test_no_stop_event_after_success(self):
        """Bug ②：发布成功后不得 stop_event（会中止 agent 导致无回复）"""
        ev = make_event()
        stopped = []
        ev.stop_event = lambda: stopped.append(True)
        asyncio.run(self.handle.send_group_notice(ev, content="测试"))
        self.assertEqual(stopped, [])

    def test_empty_content_rejected_before_api(self):
        ev = make_event()
        ev.message_str = "/发布群公告 "
        ret = asyncio.run(self.handle.send_group_notice(ev))
        self.assertEqual(ret, "未指定群公告内容")


class NormalHandleStopEventTests(unittest.TestCase):
    def test_set_group_ban_does_not_stop_event(self):
        """Bug ②同款：set_group_ban 被多个 LLM 工具复用，不得 stop_event"""
        ev = make_event()
        stopped = []
        ev.stop_event = lambda: stopped.append(True)

        ban_calls = []

        async def _set_group_ban(**kwargs):
            ban_calls.append(kwargs)

        ev.bot.set_group_ban = _set_group_ban

        cfg = NS(get_ban_time_with_range=lambda v, d: 60)
        db = NS(get_group_snapshot=lambda gid: {})
        handle = normal_mod.NormalHandle(cfg, db)
        ret = asyncio.run(
            handle.set_group_ban(ev, ban_time=60, target_id="10001")
        )
        self.assertIn("已被禁言", ret)
        self.assertEqual(ban_calls[0]["user_id"], 10001)
        self.assertEqual(stopped, [])


if __name__ == "__main__":
    unittest.main()