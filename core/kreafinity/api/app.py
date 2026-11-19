"""The daemon's HTTP API: REST for actions, a WebSocket for live events.

Every route except ``GET /api/health`` needs the local token:

- REST: ``Authorization: Bearer <token>`` header;
- WebSocket ``/api/events``: the first message must be
  ``{"type": "auth", "token": "<token>"}``.

No cookie is used, so other websites cannot make the browser act on the
daemon (no CSRF), and requests whose ``Host`` is not the local address are
refused (DNS rebinding).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, Query, Request, WebSocket, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, StrictBool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.websockets import WebSocketDisconnect

from kreafinity import __version__
from kreafinity.api.auth import token_matches
from kreafinity.service import AssistantService, ErrorCode, ServiceError

#: Seconds a WebSocket client has to authenticate.
AUTH_TIMEOUT = 10.0
WS_POLICY_VIOLATION = 1008

STATUS_BY_CODE: dict[ErrorCode, int] = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "busy": status.HTTP_409_CONFLICT,
    "invalid": status.HTTP_400_BAD_REQUEST,
    "invalid_config": status.HTTP_409_CONFLICT,
    "provider_unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "provider_auth": status.HTTP_502_BAD_GATEWAY,
    "provider_error": status.HTTP_502_BAD_GATEWAY,
}


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


class ApprovalDecision(BaseModel):
    approved: StrictBool


class DefaultProvider(BaseModel):
    provider: str = Field(min_length=1, max_length=200)


def create_app(
    service_factory: Callable[[], AssistantService],
    token: str,
    *,
    port: int,
    allowed_hosts: Sequence[str] = ("127.0.0.1", "localhost"),
) -> FastAPI:
    """Build the API application.

    Args:
        service_factory: Creates the service when the application starts
            (inside its event loop).
        token: Secret every client must present.
        port: Port the daemon listens on (for the allowed WebSocket origins).
        allowed_hosts: Accepted values of the ``Host`` header.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        service = service_factory()
        app.state.service = service
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
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    allowed_origins = {f"http://{host}:{port}" for host in allowed_hosts}

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=STATUS_BY_CODE.get(exc.code, status.HTTP_500_INTERNAL_SERVER_ERROR),
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    def require_token(request: Request) -> None:
        scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token_matches(credentials.strip(), token):
            raise _Unauthorized

    @app.exception_handler(_Unauthorized)
    async def unauthorized(request: Request, exc: _Unauthorized) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": {"code": "unauthorized", "message": "Missing or invalid token"}},
            headers={"WWW-Authenticate": "Bearer"},
        )

    api = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "name": "kreafinity", "version": __version__}

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
        return await service.send_message(conversation_id, body.content, provider=body.provider)

    @api.post("/conversations/{conversation_id}/cancel")
    async def cancel(service: Service, conversation_id: str) -> dict[str, str]:
        await service.cancel_run(conversation_id)
        return {"status": "cancelled"}

    # --- Approvals ---------------------------------------------------------------------

    @api.get("/approvals")
    async def list_approvals(service: Service) -> list[dict[str, Any]]:
        return service.pending_approvals()

    @api.post("/approvals/{approval_id}")
    async def resolve_approval(
        service: Service, approval_id: str, body: ApprovalDecision
    ) -> dict[str, Any]:
        return service.resolve_approval(approval_id, body.approved)

    # --- Providers, tools and settings ---------------------------------------------------

    @api.get("/providers")
    async def list_providers(service: Service) -> dict[str, Any]:
        return service.list_providers()

    @api.get("/providers/{name}/models")
    async def list_models(service: Service, name: str) -> list[dict[str, Any]]:
        return await service.list_models(name)

    @api.put("/providers/default")
    async def set_default_provider(service: Service, body: DefaultProvider) -> dict[str, Any]:
        return service.set_default_provider(body.provider)

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
        await websocket.accept()
        try:
            message = json.loads(
                await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT)
            )
        except (TimeoutError, WebSocketDisconnect, ValueError):
            await _close_quietly(websocket)
            return
        if not (
            isinstance(message, dict)
            and message.get("type") == "auth"
            and isinstance(message.get("token"), str)
            and token_matches(message["token"], token)
        ):
            await _close_quietly(websocket)
            return

        service: AssistantService = websocket.app.state.service
        subscription = service.events.subscribe()
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

    return app


class _Unauthorized(Exception):
    """The request did not carry the right token."""


async def _close_quietly(websocket: WebSocket) -> None:
    with contextlib.suppress(Exception):
        await websocket.close(code=WS_POLICY_VIOLATION)
