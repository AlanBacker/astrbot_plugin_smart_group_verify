import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

PACKAGE_NAME = "astrbot_plugin_smart_group_verify"
PACKAGE_DIR = Path(__file__).parents[1]
PACKAGE_ROOT = PACKAGE_DIR.parent
sys.path.insert(0, str(PACKAGE_ROOT))
if importlib.util.find_spec(PACKAGE_NAME) is None:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PACKAGE_DIR)]
    sys.modules[PACKAGE_NAME] = package


class DummyLogger:
    def __getattr__(self, _):
        return lambda *args, **kwargs: None


class DummyFilter:
    EventMessageType = types.SimpleNamespace(ALL="all")
    PermissionType = types.SimpleNamespace(ADMIN="admin")

    @staticmethod
    def _decorator(*args, **kwargs):
        return lambda func: func

    command = _decorator
    permission_type = _decorator
    event_message_type = _decorator
    on_astrbot_loaded = _decorator


astrbot = types.ModuleType("astrbot")
astrbot_api = types.ModuleType("astrbot.api")
astrbot_event = types.ModuleType("astrbot.api.event")
astrbot_star = types.ModuleType("astrbot.api.star")
astrbot_core = types.ModuleType("astrbot.core")
astrbot_utils = types.ModuleType("astrbot.core.utils")
astrbot_path = types.ModuleType("astrbot.core.utils.astrbot_path")
astrbot_api.AstrBotConfig = dict
astrbot_api.logger = DummyLogger()
astrbot_event.AstrMessageEvent = object
astrbot_event.filter = DummyFilter()
astrbot_star.Context = object
astrbot_star.Star = type("Star", (), {"__init__": lambda self, context: setattr(self, "context", context)})
astrbot_star.StarTools = type("StarTools", (), {"get_data_dir": lambda plugin_name=None: Path(".")})
astrbot_path.get_astrbot_data_path = lambda: "."
astrbot_path.get_astrbot_plugin_path = lambda: "./plugins"
sys.modules.update(
    {
        "astrbot": astrbot,
        "astrbot.api": astrbot_api,
        "astrbot.api.event": astrbot_event,
        "astrbot.api.star": astrbot_star,
        "astrbot.core": astrbot_core,
        "astrbot.core.utils": astrbot_utils,
        "astrbot.core.utils.astrbot_path": astrbot_path,
    }
)

web_server_stub = types.ModuleType("astrbot_plugin_smart_group_verify.web_server")
web_server_stub.WebAdminServer = object
sys.modules["astrbot_plugin_smart_group_verify.web_server"] = web_server_stub

from astrbot_plugin_smart_group_verify.main import (  # noqa: E402
    SmartGroupVerificationPlugin,
)


class FakeBot:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, **payload):
        self.calls.append((action, payload))
        return {"role": "admin"}


class PluginLogicTests(unittest.IsolatedAsyncioTestCase):
    def test_detects_only_group_add_requests(self):
        self.assertTrue(
            SmartGroupVerificationPlugin._is_group_join_request(
                {
                    "post_type": "request",
                    "request_type": "group",
                    "sub_type": "add",
                }
            )
        )
        self.assertFalse(
            SmartGroupVerificationPlugin._is_group_join_request(
                {
                    "post_type": "message",
                    "request_type": "group",
                    "sub_type": "add",
                }
            )
        )

    async def test_onebot_action_payload(self):
        bot = FakeBot()
        plugin = object.__new__(SmartGroupVerificationPlugin)
        await SmartGroupVerificationPlugin._set_group_add_request(
            plugin,
            bot,
            flag="request-flag",
            approve=False,
            reason="不符合规则",
        )
        self.assertEqual(bot.calls[0][0], "set_group_add_request")
        self.assertEqual(bot.calls[0][1]["sub_type"], "add")
        self.assertFalse(bot.calls[0][1]["approve"])

    async def test_request_flags_are_deduplicated_until_released(self):
        plugin = object.__new__(SmartGroupVerificationPlugin)
        plugin._request_lock = asyncio.Lock()
        plugin._inflight_flags = set()
        plugin._processed_flags = {}
        self.assertTrue(await plugin._reserve_flag("flag-1"))
        self.assertFalse(await plugin._reserve_flag("flag-1"))
        await plugin._release_flag("flag-1", processed=True)
        self.assertFalse(await plugin._reserve_flag("flag-1"))

    async def test_bot_role_uses_group_member_info(self):
        bot = FakeBot()
        plugin = object.__new__(SmartGroupVerificationPlugin)
        role = await SmartGroupVerificationPlugin._get_bot_role(
            plugin,
            bot,
            group_id="114514",
            bot_id="1919810",
        )
        self.assertEqual(role, "admin")
        self.assertEqual(bot.calls[0][0], "get_group_member_info")
