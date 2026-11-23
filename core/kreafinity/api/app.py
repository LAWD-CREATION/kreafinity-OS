"""The daemon's HTTP API: REST for actions, a WebSocket for live events, and the web UI.

Every ``/api`` route except ``GET /api/health`` requires one of:

- the **local token**: ``Authorization: Bearer <token>`` header (REST), or
  ``{"type": "auth", "token": "<token>"}`` as first WebSocket message;
- a request from **the user's own browser**: the connection comes from a
  process of the same user (checked with the kernel, see `peer`), sends
  the ``X-Kreafinity-Client: web`` header and comes from the daemon's own
  origin. Other websites cannot meet these conditions: the custom header
  forces a CORS preflight that the daemon never grants, and their
  ``Origin`` is refused. This lets the web interface work without any
  secret, while other users of the machine still need the token.

No cookie is used (no CSRF), requests whose ``Host`` is not the local
address are refused (DNS rebinding), and every response carries a strict
Content Security Policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, Query, Request, WebSocket, status
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from kreafinity import __version__
from kreafinity.api.auth import token_matches
from kreafinity.api.peer import Address, peer_uid
from kreafinity.service import AssistantService, ErrorCode, ServiceError
from kreafinity.voice import VoiceError
from kreafinity.voice.audio import MAX_BYTES

#: Seconds a WebSocket client has to authenticate.
AUTH_TIMEOUT = 10.0
WS_POLICY_VIOLATION = 1008
#: Header the web interface adds to every API request.
CLIENT_HEADER = "x-kreafinity-client"

STATUS_BY_CODE: dict[ErrorCode, int] = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "busy": status.HTTP_409_CONFLICT,
    "invalid": status.HTTP_400_BAD_REQUEST,
    "invalid_config": status.HTTP_409_CONFLICT,
    "provider_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "provider_auth": status.HTTP_502_BAD_GATEWAY,
    "provider_error": status.HTTP_502_BAD_GATEWAY,
    "keyring_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "update_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "voice_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
}

PeerLookup = Callable[[Address | None, Address | None], int | None]


def get_service(request: Request) -> AssistantService:
    """The service of the running application."""
    service: AssistantService = request.app.state.service
    return service


# Module level: with postponed annotations, FastAPI resolves names in module globals.
Service = Annotated[AssistantService, Depends(get_service)]


class NewConversation(BaseModel):
    title: str = Field("", max_length=200)


class ConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class NewMessage(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    provider: str | None = Field(None, min_length=1, max_length=200)
    attachments: list[str] = Field(default_factory=list, max_length=10)


class MemoryText(BaseModel):
    content: str = Field(max_length=20_000)


class SpeechText(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


class ShowRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=100)


class ApprovalDecision(BaseModel):
    approved: StrictBool


class DefaultProvider(BaseModel):
    provider: str = Field(min_length=1, max_length=200)


class McpServerIn(BaseModel):
    name: str = Field(min_length=1, max_length=24)
    command: str = Field(default="", max_length=500)
    args: list[str] = Field(default_factory=list, max_length=50)
    env: dict[str, str] = Field(default_factory=dict)
    secret_env: list[str] = Field(default_factory=list, max_length=20)
    url: str = Field(default="", max_length=500)
    token: bool = False
    enabled: bool = True
    auto_approve: list[str] = Field(default_factory=list, max_length=200)


class McpServerChange(BaseModel):
    enabled: bool | None = None
    auto_approve: list[str] | None = Field(default=None, max_length=200)


class SecretValue(BaseModel):
    value: str = Field(min_length=1, max_length=4000)


class ModelName(BaseModel):
    model: str = Field(min_length=1, max_length=120)


class ApiKey(BaseModel):
    api_key: str = Field(min_length=1, max_length=1000)


def create_app(
    service_factory: Callable[[], AssistantService],
    token: str,
    *,
    port: int,
    allowed_hosts: Sequence[str] = ("127.0.0.1", "localhost"),
    web_dir: Path | None = None,
    uid: int | None = None,
    peer_lookup: PeerLookup = peer_uid,
) -> FastAPI:
    """Build the application.

    Args:
        service_factory: Creates the service when the application starts
            (inside its event loop).
        token: Secret accepted from any local client.
        port: Port the daemon listens on (for the allowed origins).
        allowed_hosts: Accepted values of the ``Host`` header.
        web_dir: Folder of the web interface, served at ``/`` (none: API only).
        uid: User whose browser is let in without the token (default: ours).
        peer_lookup: Finds the user behind a connection (replaced in tests).
    """
    owner = os.getuid() if uid is None else uid
    allowed_origins = frozenset(f"http://{host}:{port}" for host in allowed_hosts)

    def from_own_user(scope: Scope) -> bool:
        return peer_lookup(scope.get("client"), scope.get("server")) == owner

    def is_own_browser(scope: Scope) -> bool:
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        return (
            headers.get(CLIENT_HEADER) == "web"
            and (origin is None or origin in allowed_origins)
            and headers.get("sec-fetch-site") in (None, "same-origin", "none")
            and from_own_user(scope)
        )

    def is_authorized(scope: Scope) -> bool:
        scheme, _, credentials = Headers(scope=scope).get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and token_matches(credentials.strip(), token):
            return True
        return is_own_browser(scope)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        service = service_factory()
        app.state.service = service
        service.start_background_tasks()
        try:
            yield
        finally:
            await service.aclose()

    app = FastAPI(
        title="Kreafinity",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Last added runs first: Host check, security headers, then authentication.
    app.add_middleware(_AuthGate, is_authorized=is_authorized, open_paths={"/api/health"})
    app.add_middleware(_SecurityHeaders, port=port, allowed_hosts=tuple(allowed_hosts))
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=STATUS_BY_CODE.get(exc.code, status.HTTP_500_INTERNAL_SERVER_ERROR),
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    api = APIRouter(prefix="/api")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "name": "kreafinity", "version": __version__}

    @api.get("/info")
    async def info(service: Service) -> dict[str, Any]:
        return service.info()

    # --- Conversations -----------------------------------------------------------

    @api.get("/conversations")
    async def list_conversations(
        service: Service,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        return service.list_conversations(limit=limit, offset=offset)

    @api.post("/conversations", status_code=status.HTTP_201_CREATED)
    async def create_conversation(
        service: Service, body: NewConversation | None = None
    ) -> dict[str, Any]:
        return service.create_conversation(title=body.title if body else "")

    @api.get("/conversations/{conversation_id}")
    async def get_conversation(service: Service, conversation_id: str) -> dict[str, Any]:
        return service.get_conversation(conversation_id)

    @api.patch("/conversations/{conversation_id}")
    async def rename_conversation(
        service: Service, conversation_id: str, body: ConversationUpdate
    ) -> dict[str, Any]:
        return service.rename_conversation(conversation_id, body.title)

    @api.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_conversation(service: Service, conversation_id: str) -> Response:
        await service.delete_conversation(conversation_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @api.post("/conversations/{conversation_id}/messages", status_code=status.HTTP_202_ACCEPTED)
    async def send_message(
        service: Service, conversation_id: str, body: NewMessage
    ) -> dict[str, Any]:
        return await service.send_message(
            conversation_id, body.content, provider=body.provider, attachments=body.attachments
        )

    @api.post("/uploads", status_code=status.HTTP_201_CREATED)
    async def upload(service: Service, request: Request) -> dict[str, Any]:
        """A file sent as the request body (its name in the X-Filename header, URL-encoded)."""
        from urllib.parse import unquote

        from kreafinity import attachments

        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > attachments.MAX_SIZE:
                raise ServiceError("File too large (25 MB at most)", code="invalid")
        name = unquote(request.headers.get("x-filename", "fichier"))
        try:
            saved = await asyncio.to_thread(
                attachments.save, name, request.headers.get("content-type", ""), bytes(data)
            )
        except attachments.AttachmentError as exc:
            raise ServiceError(str(exc), code="invalid") from exc
        return saved.to_dict()

    @api.post("/conversations/{conversation_id}/cancel")
    async def cancel(service: Service, conversation_id: str) -> dict[str, str]:
        await service.cancel_run(conversation_id)
        return {"status": "cancelled"}

    # --- Memory ------------------------------------------------------------------------

    @api.get("/memory")
    async def memory(service: Service) -> dict[str, Any]:
        return service.memory_info()

    @api.put("/memory/{section}")
    async def write_memory(service: Service, section: str, body: MemoryText) -> dict[str, Any]:
        if section not in ("user", "notes", "soul"):
            raise ServiceError("Unknown memory section", code="not_found")
        return service.write_memory(section, body.content)

    @api.get("/voice")
    async def voice(service: Service) -> dict[str, Any]:
        return service.voice.info()

    @api.post("/voice/prepare", status_code=status.HTTP_202_ACCEPTED)
    async def prepare_voice(service: Service) -> dict[str, Any]:
        return service.voice.prepare()

    @api.post("/voice/transcribe")
    async def transcribe(service: Service, request: Request) -> dict[str, Any]:
        """The text of a recording sent as the request body (WebM, Ogg, WAV…)."""
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > MAX_BYTES:
                raise ServiceError("Recording too large", code="invalid")
        try:
            text = await service.voice.transcribe(
                bytes(data), request.headers.get("content-type", "")
            )
        except VoiceError as exc:
            raise ServiceError(str(exc), code="voice_unavailable") from exc
        return {"text": text}

    @api.post("/voice/speak")
    async def speak(service: Service, body: SpeechText) -> Response:
        """`text` read aloud, as a WAV file."""
        try:
            audio = await service.voice.speak(body.text)
        except VoiceError as exc:
            raise ServiceError(str(exc), code="voice_unavailable") from exc
        return Response(audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @api.delete("/memory/soul")
    async def reset_soul(service: Service) -> dict[str, Any]:
        return service.reset_soul()

    @api.delete("/memory/skills/{name}")
    async def delete_skill(service: Service, name: str) -> dict[str, Any]:
        return service.delete_skill(name)

    @api.delete("/memory")
    async def forget_memory(service: Service) -> dict[str, Any]:
        return service.forget_memory()

    # --- Updates -----------------------------------------------------------------------

    @api.get("/update")
    async def update_info(service: Service) -> dict[str, Any]:
        return service.update_info()

    @api.post("/update/check")
    async def check_updates(service: Service) -> dict[str, Any]:
        return await service.check_updates()

    @api.post("/update", status_code=status.HTTP_202_ACCEPTED)
    async def start_update(service: Service) -> dict[str, Any]:
        return service.start_update()

    # --- Assistant panels (bubble, Plasma widget) ---------------------------------------

    @api.post("/ui/show")
    async def show_conversation(service: Service, body: ShowRequest) -> dict[str, Any]:
        """Ask the panels to show a conversation; ``delivered`` says if one listens."""
        return await service.show_conversation(body.conversation_id)

    @api.get("/ui/requests")
    async def ui_requests(
        service: Service,
        after: Annotated[int | None, Query(ge=0)] = None,
        wait: Annotated[float, Query(ge=0, le=60)] = 25,
    ) -> dict[str, Any]:
        """Long polling for panels without a WebSocket: the requests after `after`."""
        return await service.wait_ui_requests(after, wait)

    # --- Approvals ---------------------------------------------------------------------

    @api.get("/approvals")
    async def list_approvals(service: Service) -> list[dict[str, Any]]:
        return service.pending_approvals()

    @api.post("/approvals/{approval_id}")
    async def resolve_approval(
        service: Service, approval_id: str, body: ApprovalDecision
    ) -> dict[str, Any]:
        return service.resolve_approval(approval_id, body.approved)

    # --- Providers, keys, tools and settings ----------------------------------------------

    @api.get("/providers")
    async def list_providers(service: Service) -> dict[str, Any]:
        return service.list_providers()

    @api.get("/providers/{name}/models")
    async def list_models(service: Service, name: str) -> list[dict[str, Any]]:
        return await service.list_models(name)

    @api.put("/providers/default")
    async def set_default_provider(service: Service, body: DefaultProvider) -> dict[str, Any]:
        return service.set_default_provider(body.provider)

    @api.put("/providers/{name}/key")
    async def set_api_key(service: Service, name: str, body: ApiKey) -> dict[str, Any]:
        return service.set_api_key(name, body.api_key)

    @api.delete("/providers/{name}/key")
    async def delete_api_key(service: Service, name: str) -> dict[str, Any]:
        return service.delete_api_key(name)

    @api.get("/setup")
    async def setup_status(service: Service) -> dict[str, Any]:
        return await service.setup_status()

    @api.post("/setup/complete")
    async def complete_setup(service: Service) -> dict[str, Any]:
        return service.complete_setup()

    @api.post("/setup/ollama/pull", status_code=status.HTTP_202_ACCEPTED)
    async def pull_ollama_model(service: Service, body: ModelName) -> dict[str, Any]:
        return service.pull_ollama_model(body.model)

    @api.get("/mcp")
    async def mcp_status(service: Service) -> dict[str, Any]:
        return await service.mcp_status()

    @api.post("/mcp/servers", status_code=status.HTTP_201_CREATED)
    async def add_mcp_server(service: Service, body: McpServerIn) -> dict[str, Any]:
        entry = body.model_dump(exclude={"name"})
        return service.add_mcp_server(
            body.name, {k: v for k, v in entry.items() if v not in ("", [], {})}
        )

    @api.patch("/mcp/servers/{name}")
    async def update_mcp_server(
        service: Service, name: str, body: McpServerChange
    ) -> dict[str, Any]:
        return service.update_mcp_server(name, body.model_dump(exclude_none=True))

    @api.delete("/mcp/servers/{name}")
    async def remove_mcp_server(service: Service, name: str) -> dict[str, Any]:
        return service.remove_mcp_server(name)

    @api.put("/mcp/servers/{name}/secrets/{secret}")
    async def set_mcp_secret(
        service: Service, name: str, secret: str, body: SecretValue
    ) -> dict[str, Any]:
        return service.set_mcp_secret(name, secret, body.value)

    @api.post("/mcp/servers/{name}/restart")
    async def restart_mcp_server(service: Service, name: str) -> dict[str, Any]:
        return service.restart_mcp_server(name)

    @api.get("/tools")
    async def list_tools(service: Service) -> list[dict[str, Any]]:
        return service.list_tools()

    @api.get("/config")
    async def get_config(service: Service) -> dict[str, Any]:
        return service.config()

    @api.patch("/config")
    async def update_config(
        service: Service, changes: Annotated[dict[str, Any], Body()]
    ) -> dict[str, Any]:
        return service.update_config(changes)

    app.include_router(api)

    # --- Live events --------------------------------------------------------------------

    @app.websocket("/api/events")
    async def events(websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin")
        if origin is not None and origin not in allowed_origins:
            await websocket.close(code=WS_POLICY_VIOLATION)
            return
        own_browser = origin is not None and from_own_user(websocket.scope)
        await websocket.accept()
        try:
            message = json.loads(
                await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT)
            )
        except (TimeoutError, WebSocketDisconnect, ValueError):
            await _close_quietly(websocket)
            return
        if not (isinstance(message, dict) and message.get("type") == "auth"):
            await _close_quietly(websocket)
            return
        candidate = message.get("token")
        has_token = isinstance(candidate, str) and token_matches(candidate, token)
        if not (has_token or own_browser):
            await _close_quietly(websocket)
            return

        service: AssistantService = websocket.app.state.service
        # ?panel=1: a compact assistant panel, which can show a conversation when asked.
        subscription = service.events.subscribe(panel=websocket.query_params.get("panel") == "1")
        try:
            await websocket.send_json(
                {
                    "type": "ready",
                    "version": __version__,
                    "running": service.running(),
                    "pending_approvals": service.pending_approvals(),
                }
            )

            async def forward() -> None:
                while True:
                    event = await subscription.get()
                    await websocket.send_json(event)
                    if event["type"] == "resync_required":
                        return

            async def drain() -> None:
                while True:  # ignore client messages; ends when the client leaves
                    await websocket.receive_text()

            tasks = [asyncio.create_task(forward()), asyncio.create_task(drain())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for task in done:
                with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                    task.result()
        except WebSocketDisconnect:
            pass
        finally:
            service.events.unsubscribe(subscription)
            await _close_quietly(websocket)

    # --- Web interface (static files, last so that /api always wins) ----------------------

    if web_dir is not None:
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

    return app


def _unauthorized_response() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"error": {"code": "unauthorized", "message": "Missing or invalid token"}},
        headers={"WWW-Authenticate": "Bearer"},
    )


class _AuthGate:
    """Refuses unauthenticated API requests before their body is even read."""

    def __init__(
        self, app: ASGIApp, is_authorized: Callable[[Scope], bool], open_paths: set[str]
    ) -> None:
        self.app = app
        self.is_authorized = is_authorized
        self.open_paths = frozenset(open_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path: str = scope.get("path", "")
        if (
            scope["type"] == "http"
            and path.startswith("/api/")
            and path not in self.open_paths
            and not self.is_authorized(scope)
        ):
            await _unauthorized_response()(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _SecurityHeaders:
    """Adds a strict Content Security Policy and related headers to every response."""

    def __init__(self, app: ASGIApp, port: int, allowed_hosts: tuple[str, ...]) -> None:
        self.app = app
        websockets = " ".join(f"ws://{host}:{port}" for host in allowed_hosts)
        self.headers = {
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                # Thumbnails of the files about to be sent: blob: addresses of the page.
                "img-src 'self' data: blob:; "
                f"font-src 'self'; connect-src 'self' {websockets}; manifest-src 'self'; "
                # Answers read aloud: audio the page itself received from the daemon.
                "media-src blob:; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Permissions-Policy": "camera=(), geolocation=(), payment=(), usb=()",
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cache = "no-store" if scope.get("path", "").startswith("/api/") else "no-cache"

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    headers.setdefault(name, value)
                headers["Cache-Control"] = cache
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def _close_quietly(websocket: WebSocket) -> None:
    with contextlib.suppress(Exception):
        await websocket.close(code=WS_POLICY_VIOLATION)
