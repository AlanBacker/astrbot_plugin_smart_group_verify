from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_plugin_path,
)

from .reviewer import (
    DEFAULT_REJECT_REASON,
    PLUGIN_NAME,
    RuleStore,
    ValidationError,
    build_review_prompt,
    parse_llm_decision,
)
from .web_server import WebAdminServer

PROCESSED_FLAG_TTL_SECONDS = 10 * 60


class SmartGroupVerificationPlugin(Star):
    """Review OneBot v11 QQ group join requests with an AstrBot LLM provider."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.access_token = self._resolve_access_token()
        self.store = RuleStore(
            self.data_dir,
            max_audit_logs=int(self.config.get("max_audit_logs", 500)),
            warning_logger=lambda message: logger.warning(f"[{PLUGIN_NAME}] {message}"),
        )
        self._request_lock = asyncio.Lock()
        self._inflight_flags: set[str] = set()
        self._processed_flags: OrderedDict[str, float] = OrderedDict()
        self._webui_start_task: asyncio.Task[None] | None = None
        webui_dirs = self._webui_static_dirs()
        self.web_server = WebAdminServer(
            store=self.store,
            access_token=self.access_token,
            host=str(self.config.get("webui_host", "0.0.0.0")).strip() or "0.0.0.0",
            port=int(self.config.get("webui_port", 10001)),
            static_dirs=webui_dirs,
            provider_loader=self._list_providers,
            review_tester=self._test_review,
        )
        self._schedule_webui_start()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        """Ensure the WebUI also starts during AstrBot cold boot."""
        await self._ensure_webui_started()

    def _schedule_webui_start(self) -> None:
        """Start immediately when the plugin is installed or hot-reloaded."""
        if self._webui_start_task and not self._webui_start_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                f"[{PLUGIN_NAME}] 当前没有运行中的事件循环，将在 AstrBot 初始化完成后启动 WebUI。"
            )
            return
        self._webui_start_task = loop.create_task(self._ensure_webui_started())

    async def _ensure_webui_started(self) -> None:
        """Start the independent management WebUI once."""
        try:
            await self.web_server.start()
        except Exception as exc:
            logger.exception(f"[{PLUGIN_NAME}] WebUI 启动失败: {exc}")
            return
        logger.info(f"[{PLUGIN_NAME}] WebUI 已启动: {self._webui_url(include_token=True)}")
        logger.info(
            f"[{PLUGIN_NAME}] 建议为入群审查选择响应快、成本低的小模型。"
        )
        if str(self.config.get("webui_host", "0.0.0.0")).strip() in {"0.0.0.0", "::"}:
            logger.info(
                f"[{PLUGIN_NAME}] 若 AstrBot 运行在 Docker 中，请将容器端口 "
                f"{int(self.config.get('webui_port', 10001))} 映射到宿主机；"
                "从其他设备访问时请使用 AstrBot 所在服务器 IP。"
            )

    @filter.command("group_verify_webui")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def group_verify_webui(self, event: AstrMessageEvent):
        """获取智能入群验证审查 WebUI 地址。"""
        yield event.plain_result(
            "智能入群验证审查 WebUI：\n"
            f"{self._webui_url(include_token=True)}\n"
            "访问令牌等同于管理密码，请勿转发。"
        )

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def on_group_join_request(self, event: AstrMessageEvent) -> None:
        """Handle OneBot v11 group join request events before the normal chat flow."""
        if event.get_platform_name() != "aiocqhttp":
            return
        raw = getattr(event.message_obj, "raw_message", None)
        if not self._is_group_join_request(raw):
            return
        event.stop_event()
        await self._handle_group_join_request(event, raw)

    async def terminate(self) -> None:
        if self._webui_start_task and not self._webui_start_task.done():
            self._webui_start_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._webui_start_task
        await self.web_server.stop()

    async def _handle_group_join_request(
        self,
        event: AstrMessageEvent,
        raw: Any,
    ) -> None:
        group_id = str(raw.get("group_id", "")).strip()
        user_id = str(raw.get("user_id", "")).strip()
        flag = str(raw.get("flag", "")).strip()
        answer = str(raw.get("comment", "")).strip()
        base_audit = {
            "group_id": group_id,
            "user_id": user_id,
            "answer": answer[:1000],
            "flag": flag[:200],
        }
        group = await self.store.get_group(group_id)
        if not group or not group.get("enabled", True):
            await self.store.add_audit(
                {**base_audit, "status": "ignored", "reason": "该群未启用自动审查"}
            )
            return
        if not flag:
            await self.store.add_audit(
                {**base_audit, "status": "manual", "reason": "入群申请缺少 flag"}
            )
            return
        if not any(rule.get("enabled", True) for rule in group.get("rules", [])):
            await self.store.add_audit(
                {**base_audit, "status": "manual", "reason": "该群没有启用中的规则"}
            )
            return
        if not await self._reserve_flag(flag):
            logger.debug(f"[{PLUGIN_NAME}] 忽略重复入群申请 flag={flag}")
            return

        processed = False
        try:
            role = await self._get_bot_role(
                event.bot,
                group_id=group_id,
                bot_id=str(event.message_obj.self_id),
            )
            if role not in {"owner", "admin"}:
                await self.store.add_audit(
                    {
                        **base_audit,
                        "status": "manual",
                        "reason": "Bot 不是群主或管理员，无法自动审批",
                    }
                )
                return
            provider_id = await self._resolve_provider_id(group)
            if not provider_id:
                await self.store.add_audit(
                    {
                        **base_audit,
                        "status": "manual",
                        "reason": "没有可用的 AstrBot 聊天模型",
                    }
                )
                return
            decision = await self._review(group, user_id, answer, provider_id)
            reason = "符合入群规则" if decision.approve else (
                str(group.get("reject_reason", "")).strip()
                or decision.reason
                or self._default_reject_reason()
            )
            await self._set_group_add_request(
                event.bot,
                flag=flag,
                approve=decision.approve,
                reason="" if decision.approve else reason,
            )
            processed = True
            await self.store.add_audit(
                {
                    **base_audit,
                    "status": "approved" if decision.approve else "rejected",
                    "reason": reason,
                    "matched_rules": decision.matched_rules,
                    "confidence": decision.confidence,
                    "provider_id": provider_id,
                    "model_response": decision.raw_response,
                }
            )
        except Exception as exc:
            logger.exception(f"[{PLUGIN_NAME}] 入群申请处理失败: {exc}")
            processed = await self._handle_failure(event.bot, flag, base_audit, exc)
        finally:
            await self._release_flag(flag, processed=processed)

    async def _handle_failure(
        self,
        bot: Any,
        flag: str,
        base_audit: dict[str, Any],
        exc: Exception,
    ) -> bool:
        reason = self._default_reject_reason()
        if self.config.get("failure_policy", "manual") == "reject":
            try:
                await self._set_group_add_request(
                    bot,
                    flag=flag,
                    approve=False,
                    reason=reason,
                )
            except Exception as reject_exc:
                logger.exception(f"[{PLUGIN_NAME}] 故障策略拒绝申请失败: {reject_exc}")
                await self.store.add_audit(
                    {
                        **base_audit,
                        "status": "manual",
                        "reason": f"审查失败且自动拒绝失败：{exc}",
                    }
                )
                return False
            await self.store.add_audit(
                {
                    **base_audit,
                    "status": "rejected",
                    "reason": f"{reason}（模型调用失败）",
                }
            )
            return True
        await self.store.add_audit(
            {
                **base_audit,
                "status": "manual",
                "reason": f"审查失败，已保留人工处理：{exc}",
            }
        )
        return False

    async def _review(
        self,
        group: dict[str, Any],
        user_id: str,
        answer: str,
        provider_id: str,
    ):
        prompt = build_review_prompt(group, user_id, answer)
        timeout = max(5, min(int(self.config.get("review_timeout_seconds", 30)), 180))
        response = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            ),
            timeout=timeout,
        )
        return parse_llm_decision(response.completion_text)

    async def _test_review(self, group_id: str, answer: str) -> dict[str, Any]:
        group = await self.store.get_group(group_id)
        if not group:
            raise ValidationError("没有找到该群配置")
        if not any(rule.get("enabled", True) for rule in group.get("rules", [])):
            raise ValidationError("请至少启用一条规则")
        provider_id = await self._resolve_provider_id(group)
        if not provider_id:
            raise ValidationError("没有可用的 AstrBot 聊天模型")
        decision = await self._review(group, "WebUI-模拟申请", answer, provider_id)
        return {"provider_id": provider_id, "decision": decision.to_dict()}

    async def _resolve_provider_id(self, group: dict[str, Any]) -> str:
        settings = await self.store.get_settings()
        configured = (
            str(group.get("provider_id", "")).strip()
            or str(settings.get("global_provider_id", "")).strip()
            or str(self.config.get("default_provider_id", "")).strip()
        )
        if configured and self.context.get_provider_by_id(configured):
            return configured
        using_provider = self.context.get_using_provider()
        if using_provider:
            return str(using_provider.meta().id)
        return ""

    def _list_providers(self) -> list[dict[str, Any]]:
        providers: list[dict[str, Any]] = []
        for provider in self.context.get_all_providers():
            try:
                meta = provider.meta()
                model = str(meta.model or "")
                providers.append(
                    {
                        "id": str(meta.id),
                        "model": model,
                        "type": str(meta.type),
                        "recommended_small_model": self._looks_like_small_model(model),
                    }
                )
            except Exception as exc:
                logger.warning(f"[{PLUGIN_NAME}] 读取模型信息失败: {exc}")
        return providers

    @staticmethod
    def _looks_like_small_model(model: str) -> bool:
        model = model.lower()
        hints = ("mini", "flash", "lite", "small", "turbo", "7b", "8b", "14b")
        return any(hint in model for hint in hints)

    @staticmethod
    def _is_group_join_request(raw: Any) -> bool:
        return bool(
            hasattr(raw, "get")
            and raw.get("post_type") == "request"
            and raw.get("request_type") == "group"
            and raw.get("sub_type") == "add"
        )

    async def _get_bot_role(self, bot: Any, group_id: str, bot_id: str) -> str:
        result = await self._call_action(
            bot,
            "get_group_member_info",
            group_id=int(group_id),
            user_id=int(bot_id),
            no_cache=False,
        )
        return str(result.get("role", "member"))

    async def _set_group_add_request(
        self,
        bot: Any,
        *,
        flag: str,
        approve: bool,
        reason: str,
    ) -> None:
        await self._call_action(
            bot,
            "set_group_add_request",
            flag=flag,
            sub_type="add",
            approve=approve,
            reason=reason[:100],
        )

    @staticmethod
    async def _call_action(bot: Any, action: str, **payload: Any) -> Any:
        direct = getattr(bot, "call_action", None)
        if callable(direct):
            return await direct(action=action, **payload)
        api = getattr(bot, "api", None)
        if api and callable(getattr(api, "call_action", None)):
            return await api.call_action(action, **payload)
        raise RuntimeError("当前 OneBot 客户端不支持 call_action")

    async def _reserve_flag(self, flag: str) -> bool:
        async with self._request_lock:
            now = time.monotonic()
            while self._processed_flags:
                _, created_at = next(iter(self._processed_flags.items()))
                if now - created_at < PROCESSED_FLAG_TTL_SECONDS:
                    break
                self._processed_flags.popitem(last=False)
            if flag in self._inflight_flags or flag in self._processed_flags:
                return False
            self._inflight_flags.add(flag)
            return True

    async def _release_flag(self, flag: str, *, processed: bool) -> None:
        async with self._request_lock:
            self._inflight_flags.discard(flag)
            if processed:
                self._processed_flags[flag] = time.monotonic()

    def _resolve_access_token(self) -> str:
        configured = str(self.config.get("webui_access_token", "")).strip()
        if configured:
            return configured
        token_path = self.data_dir / "webui_access_token.txt"
        if token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip()
            if token:
                return token
        token = secrets.token_urlsafe(32)
        token_path.write_text(token, encoding="utf-8")
        return token

    def _default_reject_reason(self) -> str:
        return (
            str(self.config.get("default_reject_reason", "")).strip()[:100]
            or DEFAULT_REJECT_REASON
        )

    @staticmethod
    def _webui_static_dirs() -> list[Path]:
        """Cover normal imports and AstrBot's uploaded-plugin installation layout."""
        module_dir = Path(__file__).resolve().parent
        plugin_dir = Path(get_astrbot_plugin_path()).resolve() / PLUGIN_NAME
        legacy_plugin_dir = (
            Path(get_astrbot_data_path()).resolve() / "plugins" / PLUGIN_NAME
        )
        candidates = [
            module_dir,
            module_dir / "webui",
            plugin_dir,
            plugin_dir / "webui",
            legacy_plugin_dir,
            legacy_plugin_dir / "webui",
        ]
        return list(dict.fromkeys(candidates))

    def _webui_url(self, *, include_token: bool) -> str:
        public_url = str(self.config.get("webui_public_url", "")).strip().rstrip("/")
        if public_url:
            base_url = public_url
        else:
            host = str(self.config.get("webui_host", "0.0.0.0")).strip()
            browser_host = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host
            base_url = f"http://{browser_host}:{int(self.config.get('webui_port', 10001))}"
        if include_token:
            return f"{base_url}/?token={quote(self.access_token)}"
        return base_url
