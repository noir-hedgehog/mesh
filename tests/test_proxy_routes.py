from pathlib import Path
import unittest


class MeshProxyRouteTests(unittest.TestCase):
    def setUp(self):
        self.config = (Path(__file__).resolve().parents[1] / "plane/apps/proxy/Caddyfile.ce").read_text()

    def test_workspace_routes_are_not_service_aliases(self):
        self.assertNotIn("path_regexp agentpm_path", self.config)
        self.assertNotIn("redir /agentpm ", self.config)
        self.assertNotIn("reverse_proxy /mesh/*", self.config)
        for workspace in ("agentpm", "mesh"):
            for suffix in ("projects/project/issues/issue/", "settings/members/", "settings/projects/project/mesh/policy/"):
                self.assertNotIn(f"/{workspace}/{suffix}", self.config)
        self.assertIn("reverse_proxy /* web:3000", self.config)

    def test_health_compatibility_is_explicit(self):
        self.assertIn("@agentpm_health path /agentpm/health /agentpm/health/", self.config)
        self.assertIn("redir @agentpm_health /mesh/health/ permanent", self.config)
        self.assertIn("@mesh_service path /mesh /mesh/ /mesh/health /mesh/health/", self.config)
        self.assertIn("reverse_proxy @mesh_service api:8000", self.config)
