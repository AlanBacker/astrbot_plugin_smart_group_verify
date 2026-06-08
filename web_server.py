from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aiohttp import web
from astrbot.api import logger

from .reviewer import RuleStore, ValidationError

ProviderLoader = Callable[[], list[dict[str, Any]]]
ReviewTester = Callable[[str, str], Awaitable[dict[str, Any]]]


class WebAdminServer:
    """Token-protected standalone WebUI for group rules and audit entries."""

    def __init__(
        self,
        *,
        store: RuleStore,
        access_token: str,
        host: str,
        port: int,
        static_dirs: list[Path],
        provider_loader: ProviderLoader,
        review_tester: ReviewTester,
    ) -> None:
        self.store = store
        self.access_token = access_token
        self.host = host
        self.port = port
        self.static_dirs = static_dirs
        self.provider_loader = provider_loader
        self.review_tester = review_tester
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        if self._runner is not None:
            return
        app = web.Application(
            client_max_size=256 * 1024,
            middlewares=[self._error_middleware, self._auth_middleware],
        )
        app.add_routes(
            [
                web.get("/", self._index),
                web.get("/app.js", self._app_js),
                web.get("/style.css", self._style_css),
                web.get("/api/health", self._health),
                web.get("/api/bootstrap", self._bootstrap),
                web.put("/api/settings", self._update_settings),
                web.post("/api/groups", self._upsert_group),
                web.put("/api/groups/{group_id}", self._upsert_group),
                web.delete("/api/groups/{group_id}", self._delete_group),
                web.delete("/api/audits", self._clear_audits),
                web.post("/api/test-review", self._test_review),
            ]
        )
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        try:
            await self._site.start()
        except Exception:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            raise
        status = self._static_status()
        if status["ready"]:
            logger.info("WebUI assets loaded from %s", status["static_dir"])
        else:
            logger.error(
                "WebUI assets are incomplete. missing=%s searched=%s",
                status["missing"],
                status["searched_dirs"],
            )

    async def stop(self) -> None:
        if self._runner is None:
            return
        await self._runner.cleanup()
        self._runner = None
        self._site = None

    @web.middleware
    async def _error_middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        try:
            return await handler(request)
        except ValidationError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except web.HTTPException:
            raise
        except Exception as exc:
            logger.exception("WebUI request failed: %s", exc)
            return web.json_response(
                {
                    "error": (
                        "服务异常，请稍后重试。若问题持续存在，请检查 AstrBot 日志"
                        "并访问 /api/health 查看 WebUI 状态。"
                    )
                },
                status=500,
            )

    @web.middleware
    async def _auth_middleware(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        if not request.path.startswith("/api/") or request.path == "/api/health":
            return await handler(request)
        supplied = request.headers.get("Authorization", "")
        if supplied.startswith("Bearer "):
            supplied = supplied[7:]
        else:
            supplied = request.headers.get("X-Access-Token", "")
        if not secrets.compare_digest(supplied, self.access_token):
            return web.json_response({"error": "访问令牌无效"}, status=401)
        return await handler(request)

    def _resolve_static_file(self, name: str) -> Path | None:
        for static_dir in self.static_dirs:
            candidate = static_dir / name
            if candidate.is_file():
                return candidate
        return None

    def _static_status(self) -> dict[str, Any]:
        required_files = ("index.html", "app.js", "style.css")
        resolved_files = {
            name: self._resolve_static_file(name)
            for name in required_files
        }
        missing = [
            name for name, file_path in resolved_files.items() if file_path is None
        ]
        resolved_paths = {
            name: str(file_path)
            for name, file_path in resolved_files.items()
            if file_path is not None
        }
        resolved_dirs = list(
            dict.fromkeys(
                str(file_path.parent)
                for file_path in resolved_files.values()
                if file_path
            )
        )
        return {
            "ready": not missing,
            "missing": missing,
            "static_dir": resolved_dirs[0] if len(resolved_dirs) == 1 else "",
            "resolved_dirs": resolved_dirs,
            "resolved_files": resolved_paths,
            "searched_dirs": [str(static_dir) for static_dir in self.static_dirs],
        }

    async def _text_file(
        self,
        name: str,
        content_type: str,
        *,
        cache_control: str,
    ) -> web.Response:
        file_path = self._resolve_static_file(name)
        if file_path is None:
            status = self._static_status()
            logger.error(
                "WebUI asset %s is missing. searched=%s",
                name,
                status["searched_dirs"],
            )
            raise web.HTTPInternalServerError(
                text=(
                    "WebUI 静态资源缺失，请重新安装插件并查看 AstrBot 日志。"
                    f" missing={','.join(status['missing'])}"
                ),
                content_type="text/plain",
            )
        return web.Response(
            text=file_path.read_text(encoding="utf-8"),
            content_type=content_type,
            headers={"Cache-Control": cache_control},
        )

    async def _index(self, _: web.Request) -> web.Response:
        return await self._text_file(
            "index.html",
            "text/html",
            cache_control="no-cache",
        )

    async def _app_js(self, _: web.Request) -> web.Response:
        return await self._text_file(
            "app.js",
            "application/javascript",
            cache_control="public, max-age=300, must-revalidate",
        )

    async def _style_css(self, _: web.Request) -> web.Response:
        return await self._text_file(
            "style.css",
            "text/css",
            cache_control="public, max-age=300, must-revalidate",
        )

    async def _health(self, _: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "service": "smart-group-verify",
                "webui_assets": self._static_status(),
            }
        )

    async def _bootstrap(self, _: web.Request) -> web.Response:
        return web.json_response(
            {
                "settings": await self.store.get_settings(),
                "providers": self.provider_loader(),
                "audits": await self.store.get_audits(),
            }
        )

    async def _update_settings(self, request: web.Request) -> web.Response:
        body = await request.json()
        settings = await self.store.get_settings()
        settings["global_provider_id"] = body.get("global_provider_id", "")
        self._validate_provider_id(settings["global_provider_id"])
        return web.json_response({"settings": await self.store.update_settings(settings)})

    async def _upsert_group(self, request: web.Request) -> web.Response:
        body = await request.json()
        route_group_id = request.match_info.get("group_id")
        if route_group_id and not str(body.get("group_id", "")).strip():
            body["group_id"] = route_group_id
        self._validate_provider_id(body.get("provider_id", ""))
        group = await self.store.upsert_group(
            body,
            original_group_id=route_group_id,
        )
        return web.json_response({"group": group})

    async def _delete_group(self, request: web.Request) -> web.Response:
        deleted = await self.store.delete_group(request.match_info["group_id"])
        return web.json_response({"deleted": deleted})

    async def _clear_audits(self, _: web.Request) -> web.Response:
        await self.store.clear_audits()
        return web.json_response({"ok": True})

    async def _test_review(self, request: web.Request) -> web.Response:
        body = await request.json()
        group_id = str(body.get("group_id", "")).strip()
        answer = str(body.get("answer", "")).strip()
        if not group_id:
            raise ValidationError("请先选择群")
        if not answer:
            raise ValidationError("请输入模拟入群答案")
        if len(answer) > 1000:
            raise ValidationError("模拟入群答案最多允许 1000 个字符")
        return web.json_response(await self.review_tester(group_id, answer))

    def _validate_provider_id(self, provider_id: Any) -> None:
        provider_id = str(provider_id or "").strip()
        if not provider_id:
            return
        if provider_id not in {provider["id"] for provider in self.provider_loader()}:
            raise ValidationError("选择的模型不存在或尚未启用")
