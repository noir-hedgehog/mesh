#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse
from pathlib import Path


def load_shell_env(path: Path) -> dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"Plane agent env file not found: {path}")
    result = subprocess.run(
        ["bash", "-c", 'set -a; source "$1"; env -0', "bash", str(path)],
        check=True,
        capture_output=True,
    )
    entries = result.stdout.decode().split("\0")
    return {key: value for entry in entries if "=" in entry for key, value in [entry.split("=", 1)]}


def token_for_agent(env: dict[str, str], agent_id: str) -> str:
    token_map = json.loads(env.get("PLANE_AGENT_TOKEN_MAP", "{}"))
    if not isinstance(token_map, dict):
        raise ValueError("PLANE_AGENT_TOKEN_MAP must be a JSON object")
    token = token_map.get(agent_id)
    if isinstance(token, dict):
        token = token.get("token")
    if not token:
        raise KeyError(f"missing Plane token for agent_id={agent_id}")
    return str(token)


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def forward(
    url: str, token: str, payload: dict, *, timeout: int = 60, allow_redirects: bool = True
) -> dict | None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Api-Key": token},
        method="POST",
    )
    hostname = urlparse(url).hostname or ""
    direct = hostname == "localhost" or hostname.endswith(".ts.net")
    try:
        address = ipaddress.ip_address(hostname)
        direct = direct or address.is_loopback or address in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        pass
    handlers = [urllib.request.ProxyHandler({})] if direct else []
    if not allow_redirects:
        handlers.append(RejectRedirects())
    opener = urllib.request.build_opener(*handlers)
    with opener.open(request, timeout=timeout) as response:  # nosec B310 - operator-configured Plane URL
        body = response.read().decode("utf-8")
        return json.loads(body) if body else None


class AgentCheckError(Exception):
    pass


def check_agent(url: str, token: str, agent_id: str) -> dict:
    protocol_version = "2024-11-05"

    def request(method: str, params: dict, request_id: int | None = None) -> dict:
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            payload["id"] = request_id
        response = forward(url, token, payload, timeout=10, allow_redirects=False)
        if request_id is None:
            return {}
        if (
            not isinstance(response, dict)
            or response.get("jsonrpc") != "2.0"
            or response.get("id") != request_id
            or "error" in response
            or not isinstance(response.get("result"), dict)
        ):
            raise AgentCheckError(f"{method} did not return a valid MCP result")
        return response["result"]

    initialized = request(
        "initialize",
        {
            "protocolVersion": protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "mesh-agent-check", "version": "1"},
        },
        1,
    )
    if initialized.get("protocolVersion") != protocol_version:
        raise AgentCheckError("the endpoint returned an unsupported MCP protocol version")
    request("notifications/initialized", {})

    tools = request("tools/list", {}, 2).get("tools")
    required = {"mesh_get_me", "plane_list_projects"}
    if not isinstance(tools, list) or not required.issubset(
        tool.get("name") for tool in tools if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    ):
        raise AgentCheckError("the endpoint does not expose Mesh identity and Plane project discovery")

    result = request("tools/call", {"name": "mesh_get_me", "arguments": {}}, 3)
    content = result.get("content")
    if result.get("isError") or not isinstance(content, list) or not content:
        raise AgentCheckError("Mesh could not read the token's identity in this workspace")
    block = content[0]
    if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
        raise AgentCheckError("Mesh returned an invalid Agent identity")
    identity = json.loads(block["text"])
    agent = identity.get("agent") if isinstance(identity, dict) else None
    if not isinstance(agent, dict) or identity.get("account_type") != "agent" or agent.get("agent_id") != agent_id:
        raise AgentCheckError("the token does not belong to the configured Mesh Agent in this workspace")

    return {"status": "ok", "agent_id": agent_id, "protocol_version": protocol_version}


def main() -> None:
    parser = argparse.ArgumentParser(description="Secret-safe stdio proxy for Plane-native MCP")
    parser.add_argument("--agent-id", default=os.environ.get("AGENTPM_MCP_AGENT_ID", "hekate"))
    parser.add_argument("--url", default=os.environ.get("PLANE_NATIVE_MCP_URL"))
    parser.add_argument("--env-file", default=os.environ.get("AGENTPM_PLANE_AGENT_ENV_FILE", ".agentpm/plane-agent-env.sh"))
    parser.add_argument("--check", action="store_true", help="Verify the MCP endpoint and Agent identity without starting stdio")
    args = parser.parse_args()

    try:
        env = load_shell_env(Path(args.env_file).expanduser().resolve())
        token = token_for_agent(env, args.agent_id)
    except FileNotFoundError:
        parser.exit(1, "Mesh MCP: Agent environment file not found. Check --env-file.\n")
    except (OSError, subprocess.CalledProcessError, KeyError, ValueError):
        parser.exit(1, "Mesh MCP: could not load the configured Agent token. Check the environment file and Agent ID.\n")

    workspace = env.get("PLANE_WORKSPACE_SLUG", "agentpm")
    base_url = env.get("PLANE_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    url = args.url or f"{base_url}/api/v1/workspaces/{workspace}/mcp/"

    if args.check:
        try:
            result = check_agent(url, token, args.agent_id)
        except urllib.error.HTTPError as exc:
            hint = {
                401: "Check the Agent token and whether it has expired.",
                403: "Check the Agent token and its access to the workspace.",
                404: "Check the workspace slug and MCP endpoint URL.",
            }.get(exc.code, "Check the Mesh service and MCP endpoint URL.")
            if 300 <= exc.code < 400:
                hint = "Use the final MCP endpoint URL directly, including its trailing slash."
            parser.exit(1, f"Mesh MCP check failed: HTTP {exc.code}. {hint}\n")
        except (urllib.error.URLError, OSError):
            parser.exit(1, "Mesh MCP check failed: could not connect to the endpoint. Check the URL and network.\n")
        except ValueError:
            parser.exit(1, "Mesh MCP check failed: invalid endpoint URL or response. Check the MCP endpoint URL.\n")
        except AgentCheckError as exc:
            parser.exit(1, f"Mesh MCP check failed: {exc}.\n")
        print(json.dumps(result))
        return

    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        response = forward(url, token, request)
        if response is not None and request.get("id") is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
