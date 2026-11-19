import json
import unittest
from unittest.mock import patch

from scripts.plane_native_mcp_proxy import AgentCheckError, RejectRedirects, check_agent, forward


class NativeMcpTransportTests(unittest.TestCase):
    def test_tailnet_does_not_use_system_http_proxy(self):
        for host in ("100.79.187.62", "mesh.example.ts.net", "127.0.0.1"):
            with self.subTest(host=host), patch("urllib.request.build_opener") as build, patch("urllib.request.ProxyHandler") as proxy:
                build.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"result": {}}'
                self.assertEqual(forward(f"http://{host}/mcp/", "test-token", {}), {"result": {}})
                proxy.assert_called_once_with({})

    def test_public_endpoint_retains_operator_proxy_configuration(self):
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.return_value.__enter__.return_value.read.return_value = b""
            self.assertIsNone(forward("https://mesh.example.com/mcp/", "test-token", {}))
            build.assert_called_once_with()

    def test_preflight_transport_does_not_follow_redirects_with_agent_token(self):
        with patch("urllib.request.build_opener") as build:
            build.return_value.open.return_value.__enter__.return_value.read.return_value = b""
            forward("https://mesh.example.com/mcp/", "test-token", {}, allow_redirects=False)
            handler, = build.call_args.args
            self.assertIsInstance(handler, RejectRedirects)
            self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://example.com/"))


class AgentCheckTests(unittest.TestCase):
    url = "https://mesh.example.com/mcp/"

    def responses(self, *, agent_id="iris", tools=("mesh_get_me", "plane_list_projects"), is_error=False):
        identity = {
            "account_type": "agent" if agent_id else "human",
            "agent": {"agent_id": agent_id} if agent_id else None,
        }
        return [
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05"}},
            None,
            {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": name} for name in tools]}},
            {"jsonrpc": "2.0", "id": 3, "result": {
                "content": [{"type": "text", "text": json.dumps(identity)}],
                "isError": is_error,
            }},
        ]

    def test_checks_the_read_only_mcp_sequence_and_returns_agent_identity(self):
        with patch("scripts.plane_native_mcp_proxy.forward", side_effect=self.responses()) as send:
            self.assertEqual(check_agent(self.url, "test-token", "iris"), {
                "status": "ok", "agent_id": "iris", "protocol_version": "2024-11-05",
            })
            calls = [call.args[2] for call in send.call_args_list]
            self.assertEqual([call["method"] for call in calls], [
                "initialize", "notifications/initialized", "tools/list", "tools/call",
            ])
            self.assertNotIn("id", calls[1])
            self.assertEqual(calls[-1]["params"], {"name": "mesh_get_me", "arguments": {}})
            self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in send.call_args_list))

    def test_rejects_a_token_for_another_agent_or_human(self):
        for agent_id in ("hekate", None):
            with self.subTest(agent_id=agent_id), patch(
                "scripts.plane_native_mcp_proxy.forward", side_effect=self.responses(agent_id=agent_id)
            ):
                with self.assertRaisesRegex(AgentCheckError, "does not belong"):
                    check_agent(self.url, "test-token", "iris")

    def test_rejects_an_endpoint_without_mesh_tools(self):
        with patch("scripts.plane_native_mcp_proxy.forward", side_effect=self.responses(tools=("plane_list_projects",))):
            with self.assertRaisesRegex(AgentCheckError, "does not expose Mesh identity"):
                check_agent(self.url, "test-token", "iris")

    def test_rejects_identity_tool_error_even_when_http_succeeded(self):
        with patch("scripts.plane_native_mcp_proxy.forward", side_effect=self.responses(is_error=True)):
            with self.assertRaisesRegex(AgentCheckError, "could not read"):
                check_agent(self.url, "test-token", "iris")
