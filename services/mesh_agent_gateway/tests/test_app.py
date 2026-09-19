# Copyright (c) 2026-present Mesh contributors
# SPDX-License-Identifier: AGPL-3.0-only

import json
import asyncio
import subprocess
import time
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from starlette.testclient import TestClient
from google.protobuf.struct_pb2 import Struct

from services.mesh_agent_gateway.app import OpenClawExecutor, _worktree, create_app, execution_environment, plain_metadata, redact


def test_nested_a2a_metadata_is_json_serializable():
    payload = {"required_evidence": ["summary", "tests"], "context": {"version": 1}}
    metadata = Struct()
    metadata.update(payload)
    assert json.loads(json.dumps(plain_metadata(dict(metadata)))) == payload


def test_runtime_does_not_inherit_gateway_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "gateway-test-secret")
    monkeypatch.setenv("PLANE_AGENT_TOKEN_MAP", "test-secret-map")
    monkeypatch.setenv("MESH_AGENT_GATEWAY_TOKEN", "cloud-test-secret")
    env = execution_environment(tmp_path / "config.json")
    assert "MESH_GATEWAY_TOKEN" not in env
    assert "MESH_AGENT_GATEWAY_TOKEN" not in env
    assert "PLANE_AGENT_TOKEN_MAP" not in env
    assert env["OPENCLAW_CONFIG_PATH"].endswith("config.json")


def test_embedded_runtime_scopes_mcp_and_workspace(tmp_path, monkeypatch):
    source = tmp_path / "openclaw.json"
    source.write_text(json.dumps({
        "agents": {"list": [{"id": "iris", "tools": {"profile": "coding"}}]},
        "mcp": {"servers": {"plane-native-iris": {}, "plane-native-hekate": {}, "agentpm-plane": {}}},
    }))
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(source))
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "not-for-the-agent")
    metadata = Struct()
    metadata.update({"required_evidence": ["summary"]})
    context = SimpleNamespace(metadata=dict(metadata), task_id="test-task", get_user_input=lambda: "test")

    class Process:
        returncode = 0

        async def communicate(self):
            return b'{"result":{"payloads":[]}}', b""

    async def spawn(*args, **kwargs):
        config = json.loads(Path(kwargs["env"]["OPENCLAW_CONFIG_PATH"]).read_text())
        assert list(config["mcp"]["servers"]) == ["plane-native-iris"]
        assert config["agents"]["list"][0]["tools"]["alsoAllow"] == ["plane-native-iris__*"]
        assert config["agents"]["list"][0]["workspace"] == str(tmp_path)
        assert "MESH_GATEWAY_TOKEN" not in kwargs["env"]
        assert kwargs["start_new_session"] is True
        assert "--local" in args
        return Process()

    with patch("services.mesh_agent_gateway.app.asyncio.create_subprocess_exec", spawn):
        asyncio.run(OpenClawExecutor("iris")._run_openclaw(context, tmp_path))
    assert "workspace" not in json.loads(source.read_text())["agents"]["list"][0]


def _headers():
    return {
        "Authorization": "Bearer gateway-test-token",
        "A2A-Version": "1.0",
        "Idempotency-Key": "attempt-iris-1",
    }


def _send_body():
    return {
        "jsonrpc": "2.0",
        "id": "attempt-iris-1",
        "method": "SendMessage",
        "params": {
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": "Implement the Mesh stage"}],
                "messageId": "attempt-iris-1",
                "contextId": "loop-1",
                "metadata": {"required_evidence": ["summary"]},
            },
            "configuration": {"returnImmediately": True},
        },
    }


def test_agent_card_auth_idempotency_and_sqlite_recovery(tmp_path, monkeypatch):
    state_path = tmp_path / "gateway.sqlite3"
    monkeypatch.setenv("MESH_GATEWAY_STATE_DB", str(state_path))
    monkeypatch.setenv("MESH_GATEWAY_TOKEN", "gateway-test-token")
    monkeypatch.setenv("MESH_GATEWAY_PUBLIC_URL", "http://100.118.86.67:18890")

    calls = []

    async def fake_openclaw(self, context, worktree):
        calls.append((self.agent_id, context.task_id))
        return {
            "model": "test-model",
            "result": {
                "payloads": [
                    {
                        "text": (
                            '{"outcome":"succeeded","evidence":'
                            '[{"key":"summary","kind":"text","title":"Implemented"}]}'
                        )
                    }
                ]
            },
        }

    with patch.object(OpenClawExecutor, "_run_openclaw", fake_openclaw):
        with TestClient(create_app()) as client:
            assert client.get("/health").json()["protocol_version"] == "1.0"
            card = client.get("/agents/iris/.well-known/agent-card.json").json()
            assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
            assert client.post("/agents/iris/a2a", json={}).status_code == 401
            assert client.post("/agents/unknown/a2a", json={}, headers=_headers()).status_code == 404

            first = client.post("/agents/iris/a2a", json=_send_body(), headers=_headers())
            second = client.post("/agents/iris/a2a", json=_send_body(), headers=_headers())
            assert first.status_code == second.status_code == 200
            first_task = first.json()["result"]["task"]
            assert second.json()["result"]["task"]["id"] == first_task["id"]

            get_body = {
                "jsonrpc": "2.0",
                "id": "poll-1",
                "method": "GetTask",
                "params": {"id": first_task["id"], "historyLength": 10},
            }
            for _ in range(40):
                task = client.post(
                    "/agents/iris/a2a", json=get_body, headers={**_headers(), "Idempotency-Key": "poll-1"}
                ).json()["result"]
                if task["status"]["state"] == "TASK_STATE_COMPLETED":
                    break
                time.sleep(0.025)
            assert task["status"]["state"] == "TASK_STATE_COMPLETED"
            assert task["artifacts"][0]["name"] == "mesh-stage-completion.json"
            assert calls == [("iris", first_task["id"])]

    with TestClient(create_app()) as recovered:
        restored = recovered.post(
            "/agents/iris/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "recovery-poll",
                "method": "GetTask",
                "params": {"id": first_task["id"]},
            },
            headers={**_headers(), "Idempotency-Key": "recovery-poll"},
        )
        assert restored.json()["result"]["status"]["state"] == "TASK_STATE_COMPLETED"


def test_redact_removes_plane_and_bearer_tokens():
    value = redact("plane_api_secret and Bearer abc.def.ghi")
    assert "plane_api_secret" not in value
    assert "abc.def.ghi" not in value


def test_worktree_recovers_an_existing_run_branch(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.email", "mesh-test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Mesh Test"], check=True)
    (repository / "README.md").write_text("mesh\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-m", "test"], check=True, capture_output=True)
    monkeypatch.setenv("MESH_GATEWAY_PROJECT_REPOS", json.dumps({"project-1": str(repository)}))
    monkeypatch.setenv("MESH_GATEWAY_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setenv("MESH_GATEWAY_GIT_BASE_REF", "main")

    metadata = {"project_id": "project-1", "mesh_run_id": "12345678-1234-1234-1234-123456789abc"}
    first, branch = _worktree(metadata)
    subprocess.run(["git", "-C", str(repository), "worktree", "remove", "--force", str(first)], check=True)
    recovered, recovered_branch = _worktree(metadata)

    assert recovered == first
    assert recovered_branch == branch
    assert (recovered / ".git").exists()
