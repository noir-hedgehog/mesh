# Copyright (c) 2026-present Mesh contributors
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from uuid import UUID
from pathlib import Path
from typing import Any

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import DatabaseTaskStore, TaskUpdater
from a2a.helpers.proto_helpers import new_task_from_user_message
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    HTTPAuthSecurityScheme,
    Part,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from sqlalchemy.ext.asyncio import create_async_engine
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


DEFAULT_AGENTS = {
    "hekate": {"name": "Hekate", "roles": ["pm", "reviewer"]},
    "iris": {"name": "Iris", "roles": ["developer"]},
    "lingxi": {"name": "Lingxi", "roles": ["tester"]},
    "taichi": {"name": "Taichi", "roles": ["observer"]},
}
TOKEN_PATTERNS = (
    (re.compile(r"plane_api_[A-Za-z0-9_-]+"), "[REDACTED_API_TOKEN]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
)


def configured_agents() -> dict[str, dict[str, Any]]:
    raw = os.environ.get("MESH_GATEWAY_AGENTS", "")
    if not raw:
        return DEFAULT_AGENTS
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError("MESH_GATEWAY_AGENTS must be a non-empty JSON object")
    return {str(key): dict(value or {}) for key, value in parsed.items()}


def project_repositories() -> dict[str, Path]:
    raw = json.loads(os.environ.get("MESH_GATEWAY_PROJECT_REPOS", "{}"))
    return {str(key): Path(str(value)).expanduser().resolve() for key, value in raw.items()}


def redact(value: str) -> str:
    result = value
    for pattern, replacement in TOKEN_PATTERNS:
        result = pattern.sub(replacement, result)
    return result[-4000:]


def plain_metadata(value: Any) -> Any:
    # A2A RequestContext exposes nested Struct values as protobuf messages.
    if isinstance(value, Message):
        return MessageToDict(value)
    if isinstance(value, dict):
        return {key: plain_metadata(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_metadata(item) for item in value]
    return value


def _extract_json(text: str) -> dict[str, Any]:
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    candidates.append(text.strip())
    for candidate in reversed(candidates):
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _extract_openclaw_text(payload: dict[str, Any]) -> str:
    values: list[str] = []
    result = payload.get("result", payload)
    for item in result.get("payloads", []):
        if not isinstance(item, dict):
            continue
        value = item.get("text") or item.get("content") or item.get("message")
        if value:
            values.append(str(value))
    return "\n".join(values).strip() or "Agent completed the assigned Mesh stage."


def _completion_payload(result: dict[str, Any], text: str, *, agent_id: str, branch: str | None) -> dict[str, Any]:
    runtime = result.get("result", result)
    meta = runtime.get("meta", {})
    agent_meta = meta.get("agentMeta", {})
    reported = _extract_json(text)
    evidence = reported.get("evidence") if isinstance(reported.get("evidence"), list) else []
    if not evidence:
        evidence = [{"key": "summary", "kind": "text", "title": "Agent summary", "summary": text[:4000]}]
    usage = agent_meta.get("usage") or runtime.get("usage") or {}
    return {
        "schema_version": 1,
        "outcome": str(reported.get("outcome") or "succeeded"),
        "evidence": evidence,
        "handoff_target_agent_id": reported.get("handoff_target_agent_id"),
        "selected_next_node_id": reported.get("selected_next_node_id"),
        "agent_id": agent_id,
        "provider": agent_meta.get("provider") or "openclaw",
        "model": agent_meta.get("model") or runtime.get("model") or "unspecified",
        "usage": usage,
        "latency_ms": meta.get("durationMs", 0),
        "branch": branch,
    }


def _worktree(metadata: dict[str, Any]) -> tuple[Path | None, str | None]:
    project_id = str(metadata.get("project_id") or "")
    run_id = str(metadata.get("mesh_run_id") or "")
    repository = project_repositories().get(project_id)
    if not repository or not run_id:
        return None, None
    run_id = str(UUID(run_id))
    if not (repository / ".git").exists():
        raise ValueError(f"Configured repository is not a git checkout: {repository}")
    root = Path(os.environ.get("MESH_GATEWAY_WORKTREE_ROOT", "~/.mesh/worktrees")).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    worktree = root / run_id
    branch = f"mesh/run-{run_id[:12]}"
    if worktree.exists():
        return worktree, branch
    base_ref = os.environ.get("MESH_GATEWAY_GIT_BASE_REF", "origin/main")
    branch_exists = subprocess.run(
        ["git", "-C", str(repository), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False,
    ).returncode == 0
    arguments = ["git", "-C", str(repository), "worktree", "add"]
    if not branch_exists:
        arguments.extend(["-b", branch])
    arguments.extend([str(worktree), branch if branch_exists else base_ref])
    subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
    )
    return worktree, branch


class OpenClawExecutor(AgentExecutor):
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or context.message.message_id
        context_id = context.context_id or task_id
        updater = TaskUpdater(event_queue, task_id, context_id)
        if context.current_task is None:
            await event_queue.enqueue_event(new_task_from_user_message(context.message))
        await updater.start_work()
        try:
            worktree, branch = await asyncio.to_thread(_worktree, context.metadata)
            result = await self._run_openclaw(context, worktree)
            summary = _extract_openclaw_text(result)
            completion = _completion_payload(result, summary, agent_id=self.agent_id, branch=branch)
            await updater.add_artifact(
                parts=[Part(text=json.dumps(completion, ensure_ascii=False))],
                name="mesh-stage-completion.json",
                metadata={"mesh_artifact_type": "stage_completion", "schema_version": 1},
                last_chunk=True,
            )
            await updater.complete()
        except asyncio.CancelledError:
            await updater.cancel()
            raise
        except Exception as exc:
            message = updater.new_agent_message([Part(text=redact(str(exc)))])
            await updater.failed(message=message)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or ""
        process = self._processes.get(task_id)
        if process and process.returncode is None:
            process.terminate()
        updater = TaskUpdater(event_queue, task_id, context.context_id or task_id)
        await updater.cancel()

    async def _run_openclaw(self, context: RequestContext, worktree: Path | None) -> dict[str, Any]:
        metadata = plain_metadata(context.metadata)
        required = list(metadata.get("required_evidence") or [])
        instruction = context.get_user_input() or "Complete the assigned Mesh stage."
        prompt = (
            f"{instruction}\n\n"
            "Use the installed mesh-plane-workflow Skill and production Mesh MCP for project context. "
            "Do not expose tokens. Work only in the supplied workspace when present.\n"
            f"Mesh metadata: {json.dumps(metadata, ensure_ascii=False)}\n"
            f"Task workspace: {worktree or 'not configured'}. Do not push or merge.\n"
            "Read the Work Item to obtain its requested change. Read the latest Policy. Discover Skills and Knowledge; "
            "include their actual version and Page/heading references in Evidence. "
            "Return the completion Artifact here; the Runner advances the Stage, so do not call mesh_complete_stage yourself.\n"
            f"Required evidence keys: {json.dumps(required, ensure_ascii=False)}\n"
            "Return one JSON object with outcome, evidence, and optional handoff_target_agent_id. "
            "Each evidence item must contain key, kind, and title; summary, uri, and metadata are optional."
        )
        session_key = f"agent:{self.agent_id}:mesh:{context.task_id}"
        command = [
            os.environ.get("OPENCLAW_BIN", "/opt/homebrew/bin/openclaw"),
            "agent",
            "--agent",
            self.agent_id,
            "--local",
            "--session-key",
            session_key,
            "--message",
            prompt,
            "--json",
            "--timeout",
            os.environ.get("MESH_GATEWAY_RUN_TIMEOUT_SECONDS", "3600"),
        ]
        # Embedded execution reads a private per-task config so OpenClaw's
        # configured workspace cannot override the Loop worktree via its Gateway.
        source = Path(os.environ.get("OPENCLAW_CONFIG_PATH", "~/.openclaw/openclaw.json")).expanduser()
        config = json.loads(source.read_text())
        for agent in config.get("agents", {}).get("list", []):
            if agent.get("id") == self.agent_id and worktree:
                agent["workspace"] = str(worktree)
        servers = config.get("mcp", {}).get("servers", {})
        config.setdefault("mcp", {})["servers"] = {
            name: server for name, server in servers.items()
            if not name.startswith(("plane-native-", "agentpm-plane")) or name == f"plane-native-{self.agent_id}"
        }
        with tempfile.TemporaryDirectory(prefix="mesh-run-config-") as directory:
            config_path = Path(directory) / "openclaw.json"
            config_path.write_text(json.dumps(config))
            config_path.chmod(0o600)
            process = await asyncio.create_subprocess_exec(
                *command, cwd=str(worktree) if worktree else None,
                env={**os.environ, "OPENCLAW_CONFIG_PATH": str(config_path)},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            self._processes[context.task_id or session_key] = process
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=int(os.environ.get("MESH_GATEWAY_RUN_TIMEOUT_SECONDS", "3600")) + 30,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
            finally:
                self._processes.pop(context.task_id or session_key, None)
        if process.returncode:
            raise RuntimeError(f"OpenClaw exited with status {process.returncode}: {redact(stderr.decode())}")
        try:
            return json.loads(stdout.decode())
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OpenClaw returned invalid JSON: {redact(stdout.decode())}") from exc


class BearerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health" or request.url.path.endswith("/.well-known/agent-card.json"):
            return await call_next(request)
        expected = os.environ.get("MESH_GATEWAY_TOKEN", "")
        if not expected or request.headers.get("Authorization") != f"Bearer {expected}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


class IdempotencyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, database_path: Path):
        super().__init__(app)
        self.database_path = database_path.with_name(f"{database_path.stem}-idempotency.sqlite3")
        self._locks: dict[str, asyncio.Lock] = {}
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS gateway_idempotency ("
                "idempotency_key TEXT PRIMARY KEY, status_code INTEGER NOT NULL, "
                "content_type TEXT NOT NULL, response_body BLOB NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )

    async def dispatch(self, request: Request, call_next):
        key = request.headers.get("Idempotency-Key", "").strip()
        if request.method != "POST" or not key:
            return await call_next(request)
        try:
            payload = json.loads((await request.body()).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return await call_next(request)
        if not isinstance(payload, dict) or payload.get("method") != "SendMessage":
            return await call_next(request)
        if len(key) > 255:
            return JSONResponse({"error": "Idempotency-Key must be at most 255 characters"}, status_code=400)
        key = f"{request.url.path}:{key}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = await asyncio.to_thread(self._read, key)
            if cached:
                return Response(content=cached[2], status_code=cached[0], media_type=cached[1])
            response = await call_next(request)
            body = b"".join([chunk async for chunk in response.body_iterator])
            content_type = response.media_type or response.headers.get("content-type", "application/json").split(";", 1)[0]
            if 200 <= response.status_code < 300:
                await asyncio.to_thread(self._write, key, response.status_code, content_type, body)
            headers = dict(response.headers)
            headers.pop("content-length", None)
            return Response(
                content=body,
                status_code=response.status_code,
                headers=headers,
                media_type=content_type,
                background=response.background,
            )

    def _read(self, key: str):
        with sqlite3.connect(self.database_path) as connection:
            return connection.execute(
                "SELECT status_code, content_type, response_body FROM gateway_idempotency WHERE idempotency_key = ?",
                (key,),
            ).fetchone()

    def _write(self, key: str, status_code: int, content_type: str, body: bytes) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO gateway_idempotency "
                "(idempotency_key, status_code, content_type, response_body) VALUES (?, ?, ?, ?)",
                (key, status_code, content_type, body),
            )


def _agent_card(agent_id: str, config: dict[str, Any], base_url: str) -> AgentCard:
    roles = [str(value) for value in config.get("roles") or []]
    return AgentCard(
        name=str(config.get("name") or agent_id),
        description=f"Mesh Agent {agent_id} for roles: {', '.join(roles) or 'member'}",
        supported_interfaces=[
            AgentInterface(
                url=f"{base_url}/agents/{agent_id}/a2a",
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        provider=AgentProvider(organization="Mesh", url=base_url),
        version="0.2.0",
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        security_schemes={
            "bearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="bearer", bearer_format="Mesh gateway token"
                )
            )
        },
        security_requirements=[SecurityRequirement(schemes={"bearer": StringList(list=[])})],
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id="mesh-stage-execution",
                name="Mesh stage execution",
                description="Execute a Mesh Loop stage and return structured Evidence and an optional handoff.",
                tags=["mesh", *roles],
                input_modes=["text/plain", "application/json"],
                output_modes=["application/json"],
            )
        ],
    )


def create_app() -> Starlette:
    host = os.environ.get("MESH_GATEWAY_PUBLIC_URL", "http://127.0.0.1:18890").rstrip("/")
    state_path = Path(os.environ.get("MESH_GATEWAY_STATE_DB", "~/.mesh/mesh-agent-gateway.sqlite3")).expanduser()
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    engine = create_async_engine(f"sqlite+aiosqlite:///{state_path}")
    task_store = DatabaseTaskStore(engine)
    routes = [Route("/health", lambda request: JSONResponse({"status": "ok", "service": "mesh-agent-gateway", "protocol_version": "1.0"}))]
    for agent_id, config in configured_agents().items():
        card = _agent_card(agent_id, config, host)
        agent_store = DatabaseTaskStore(engine, owner_resolver=lambda context, owner=agent_id: owner)
        handler = DefaultRequestHandler(
            agent_executor=OpenClawExecutor(agent_id),
            task_store=agent_store,
            agent_card=card,
        )
        routes.extend(create_jsonrpc_routes(handler, f"/agents/{agent_id}/a2a"))
        routes.extend(
            create_agent_card_routes(card, card_url=f"/agents/{agent_id}/.well-known/agent-card.json")
        )
    app = Starlette(routes=routes)
    app.add_middleware(IdempotencyMiddleware, database_path=state_path)
    app.add_middleware(BearerMiddleware)
    app.state.task_store = task_store
    app.state.engine = engine
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "services.mesh_agent_gateway.app:app",
        host=os.environ.get("MESH_GATEWAY_HOST", "127.0.0.1"),
        port=int(os.environ.get("MESH_GATEWAY_PORT", "18890")),
        log_level=os.environ.get("MESH_GATEWAY_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
