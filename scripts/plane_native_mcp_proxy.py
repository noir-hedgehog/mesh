#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import subprocess
import sys
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
    token = token_map.get(agent_id)
    if isinstance(token, dict):
        token = token.get("token")
    if not token:
        raise KeyError(f"missing Plane token for agent_id={agent_id}")
    return str(token)


def forward(url: str, token: str, payload: dict) -> dict | None:
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
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if direct else urllib.request.build_opener()
    with opener.open(request, timeout=60) as response:  # nosec B310 - operator-configured Plane URL
        body = response.read().decode("utf-8")
        return json.loads(body) if body else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Secret-safe stdio proxy for Plane-native MCP")
    parser.add_argument("--agent-id", default=os.environ.get("AGENTPM_MCP_AGENT_ID", "hekate"))
    parser.add_argument("--url", default=os.environ.get("PLANE_NATIVE_MCP_URL"))
    parser.add_argument("--env-file", default=os.environ.get("AGENTPM_PLANE_AGENT_ENV_FILE", ".agentpm/plane-agent-env.sh"))
    args = parser.parse_args()

    env = load_shell_env(Path(args.env_file).expanduser().resolve())
    workspace = env.get("PLANE_WORKSPACE_SLUG", "agentpm")
    base_url = env.get("PLANE_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    url = args.url or f"{base_url}/api/v1/workspaces/{workspace}/mcp/"
    token = token_for_agent(env, args.agent_id)

    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        response = forward(url, token, request)
        if response is not None and request.get("id") is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
