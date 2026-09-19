# Copyright (c) 2026-present Mesh contributors
# SPDX-License-Identifier: AGPL-3.0-only

from __future__ import annotations

import ipaddress
import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse

from django.utils import timezone


def validate_agent_endpoint(endpoint_url: str) -> None:
    parsed = urlparse(endpoint_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Agent endpoint must be an HTTP(S) URL")
    hostname = parsed.hostname.lower()
    if hostname.endswith(".ts.net"):
        return
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise ValueError("Agent endpoint hostname must be a Tailscale address or .ts.net name") from exc
    if address in ipaddress.ip_network("100.64.0.0/10"):
        return
    if address.is_loopback and os.environ.get("MESH_ENVIRONMENT", "development") != "production":
        return
    raise ValueError("Agent endpoint must be reachable only through Tailscale")


def agent_card_url(endpoint_url: str) -> str:
    parsed = urlparse(endpoint_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/a2a"):
        path = path[:-4]
    return parsed._replace(path=f"{path}/.well-known/agent-card.json", params="", query="", fragment="").geturl()


def sync_agent_card(profile, *, timeout: int = 10):
    # Invalidate the previous Card before any network or schema validation.
    profile.agent_card = {"available": False, "sync_pending": True}
    profile.save(update_fields=["agent_card", "updated_at"])
    if not profile.endpoint_url:
        raise ValueError("Agent endpoint_url is required before syncing its Agent Card")
    validate_agent_endpoint(profile.endpoint_url)
    request = urllib.request.Request(agent_card_url(profile.endpoint_url), headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            card = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        profile.agent_card = {**dict(profile.agent_card or {}), "available": False, "sync_error": str(exc)[:500]}
        profile.save(update_fields=["agent_card", "updated_at"])
        raise ValueError(f"Agent Card sync failed: {exc}") from exc
    if not isinstance(card, dict):
        raise ValueError("Agent Card must be an object")
    interfaces = card.get("supportedInterfaces") or card.get("supported_interfaces") or []
    interface = next(
        (
            item
            for item in interfaces
            if isinstance(item, dict)
            and str(item.get("protocolVersion") or item.get("protocol_version")) == "1.0"
            and str(item.get("protocolBinding") or item.get("protocol_binding")).upper() == "JSONRPC"
        ),
        None,
    )
    if not interface:
        raise ValueError("Agent Card must expose an A2A 1.0 JSONRPC interface")
    endpoint_url = str(interface.get("url") or profile.endpoint_url)
    validate_agent_endpoint(endpoint_url)
    profile.endpoint_url = endpoint_url
    profile.agent_card = {**card, "available": True, "synced_at": timezone.now().isoformat()}
    profile.last_seen_at = timezone.now()
    profile.save(update_fields=["endpoint_url", "agent_card", "last_seen_at", "updated_at"])
    return profile
