import unittest
from unittest.mock import MagicMock, patch

from scripts.plane_native_mcp_proxy import forward


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
