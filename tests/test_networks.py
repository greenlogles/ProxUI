"""Tests for the node network management helpers and endpoints."""

import json
import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


class NetworksTestCase(unittest.TestCase):
    def setUp(self):
        self.flask_app = app.app
        self.flask_app.config["TESTING"] = True
        self.client = self.flask_app.test_client()

        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.connection_metadata.clear()
        app.all_clusters.clear()
        app.network_pending.clear()

        app.all_clusters["cluster-a"] = {"id": "cluster-a", "name": "A", "nodes": []}
        app.current_cluster_id = "cluster-a"

        self.proxmox = Mock()
        app.proxmox_nodes["pve-a"] = self.proxmox
        app.cluster_nodes.append(
            {"name": "pve-a", "status": "online", "connection": self.proxmox}
        )
        app.connection_metadata["pve-a"] = {"host": "10.0.0.5", "user": "root@pam"}

        self.patcher = patch("app.get_proxmox_connection", return_value=self.proxmox)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.connection_metadata.clear()
        app.all_clusters.clear()
        app.network_pending.clear()
        app.current_cluster_id = None

    def _interfaces(self, value):
        self.proxmox.nodes.return_value.network.get.return_value = value


class TestNetworkParams(unittest.TestCase):
    def test_bridge_create_builds_api_params(self):
        params = app._network_params(
            {
                "iface": "vmbr1",
                "type": "bridge",
                "bridge_ports": "eno1",
                "bridge_vlan_aware": True,
                "cidr": "10.0.0.2/24",
                "gateway": "10.0.0.1",
                "mtu": "9000",
                "autostart": True,
                "comments": "",
            },
            creating=True,
        )

        self.assertEqual(
            params,
            {
                "iface": "vmbr1",
                "type": "bridge",
                "bridge_ports": "eno1",
                "bridge_vlan_aware": 1,
                "cidr": "10.0.0.2/24",
                "gateway": "10.0.0.1",
                "mtu": 9000,
                "autostart": 1,
            },
        )

    def test_rejects_bad_interface_name(self):
        with self.assertRaises(ValueError):
            app._network_params({"iface": "1bad name", "type": "bridge"}, creating=True)

    def test_rejects_uncreatable_type(self):
        with self.assertRaises(ValueError):
            app._network_params({"iface": "eno1", "type": "eth"}, creating=True)

    def test_bond_requires_slaves(self):
        with self.assertRaises(ValueError):
            app._network_params({"iface": "bond0", "type": "bond"}, creating=True)

    def test_vlan_requires_raw_device(self):
        with self.assertRaises(ValueError):
            app._network_params({"iface": "vlan5", "type": "vlan"}, creating=True)

    def test_non_numeric_mtu_rejected(self):
        with self.assertRaises(ValueError):
            app._network_params(
                {"iface": "vmbr1", "type": "bridge", "mtu": "big"}, creating=True
            )

    def test_update_does_not_require_a_name(self):
        params = app._network_params({"type": "bridge"}, creating=False)
        self.assertEqual(params, {"type": "bridge"})


class TestStaleKeys(unittest.TestCase):
    def test_cidr_covers_stored_address_and_netmask(self):
        current = {"address": "10.0.0.2", "netmask": "24", "gateway": "10.0.0.1"}
        stale = app._network_stale_keys(current, {"cidr": "10.0.0.3/24"})
        self.assertEqual(stale, ["gateway"])

    def test_cleared_address_is_deleted(self):
        current = {"address": "10.0.0.2", "netmask": "24", "comments": "lan"}
        stale = app._network_stale_keys(current, {"type": "bridge"})
        self.assertEqual(stale, ["address", "netmask", "comments"])

    def test_untouched_keys_are_left_alone(self):
        current = {"bridge_ports": "eno1"}
        stale = app._network_stale_keys(current, {"bridge_ports": "eno1 eno2"})
        self.assertEqual(stale, [])


class TestManagementIface(NetworksTestCase):
    def test_matches_the_configured_host_address(self):
        interfaces = [
            {"iface": "vmbr0", "address": "10.0.0.5"},
            {"iface": "vmbr1", "address": "192.168.1.5"},
        ]
        self.assertEqual(app._management_iface("pve-a", interfaces), "vmbr0")

    def test_no_match_when_host_is_a_name(self):
        app.connection_metadata["pve-a"]["host"] = "pve-a.lan"
        interfaces = [{"iface": "vmbr0", "address": "10.0.0.5"}]
        self.assertIsNone(app._management_iface("pve-a", interfaces))

    def test_cluster_ip_wins_over_the_entry_point_host(self):
        # Every node is reached through one entry point, so only that node's
        # interface would ever match the configured host.
        interfaces = [
            {"iface": "vmbr0", "address": "10.0.0.5"},
            {"iface": "vmbr1", "address": "10.0.0.9"},
        ]
        self.assertEqual(
            app._management_iface("pve-b", interfaces, "10.0.0.9"), "vmbr1"
        )

    def test_falls_back_to_the_host_without_a_cluster_ip(self):
        interfaces = [{"iface": "vmbr0", "address": "10.0.0.5"}]
        self.assertEqual(app._management_iface("pve-a", interfaces, None), "vmbr0")

    def test_cluster_ips_reads_node_entries_only(self):
        self.proxmox.cluster.status.get.return_value = [
            {"type": "cluster", "name": "kube"},
            {"type": "node", "name": "pve-a", "ip": "10.0.0.5"},
            {"type": "node", "name": "pve-b"},
        ]
        self.assertEqual(app._node_cluster_ips(), {"pve-a": "10.0.0.5"})

    def test_cluster_ips_survives_a_standalone_node(self):
        self.proxmox.cluster.status.get.side_effect = Exception("no cluster")
        self.assertEqual(app._node_cluster_ips(), {})


class TestNodeNetworkGet(NetworksTestCase):
    def _raw(self, status, payload):
        response = Mock(status_code=status)
        response.json.return_value = payload
        self.proxmox._store = {
            "session": Mock(**{"request.return_value": response}),
            "base_url": "https://pve-a:8006/api2/json",
        }
        return response

    def test_returns_the_diff_from_the_envelope(self):
        self._raw(200, {"data": [{"iface": "vmbr0"}], "changes": "@@ -1 +1 @@\n"})
        interfaces, changes = app._node_network_get(self.proxmox, "pve-a")
        self.assertEqual(interfaces, [{"iface": "vmbr0"}])
        self.assertEqual(changes, "@@ -1 +1 @@\n")

    def test_no_changes_key_means_no_pending_diff(self):
        self._raw(200, {"data": []})
        self.assertEqual(app._node_network_get(self.proxmox, "pve-a")[1], "")

    def test_falls_back_to_proxmoxer_on_a_bad_status(self):
        self._raw(500, {})
        self._interfaces([{"iface": "eno1"}])
        interfaces, changes = app._node_network_get(self.proxmox, "pve-a")
        self.assertEqual(interfaces, [{"iface": "eno1"}])
        self.assertEqual(changes, "")

    def test_falls_back_when_the_private_store_is_gone(self):
        self.proxmox._store = None
        self._interfaces([{"iface": "eno1"}])
        self.assertEqual(
            app._node_network_get(self.proxmox, "pve-a"), ([{"iface": "eno1"}], "")
        )


class TestCollectNodeNetworks(NetworksTestCase):
    def test_a_diff_from_pve_marks_the_node_pending(self):
        # Nothing in network_pending: the node was edited outside ProxUI.
        response = Mock(status_code=200)
        response.json.return_value = {
            "data": [{"iface": "vmbr0", "type": "bridge"}],
            "changes": "@@ -1 +1 @@\n+auto vmbr9\n",
        }
        self.proxmox._store = {
            "session": Mock(**{"request.return_value": response}),
            "base_url": "https://pve-a:8006/api2/json",
        }
        self.proxmox.cluster.status.get.return_value = []

        node = app._collect_node_networks()[0]

        self.assertTrue(node["pending"])
        self.assertIn("+auto vmbr9", node["changes"])

    def test_sorts_by_priority_and_flags_pending(self):
        self._interfaces(
            [
                {"iface": "vmbr0", "priority": 5, "type": "bridge"},
                {"iface": "eno1", "priority": 2, "type": "eth", "address": "10.0.0.5"},
            ]
        )
        app.network_pending["pve-a"] = "2026-09-11T10:00:00"

        nodes = app._collect_node_networks()

        self.assertEqual(len(nodes), 1)
        self.assertTrue(nodes[0]["online"])
        self.assertTrue(nodes[0]["pending"])
        self.assertEqual(
            [i["iface"] for i in nodes[0]["interfaces"]], ["eno1", "vmbr0"]
        )
        self.assertEqual(nodes[0]["management"], "eno1")

    def test_unreachable_node_reports_the_error(self):
        self.proxmox.nodes.return_value.network.get.side_effect = Exception("boom")

        nodes = app._collect_node_networks()

        self.assertFalse(nodes[0]["online"])
        self.assertIn("boom", nodes[0]["error"])
        self.assertEqual(nodes[0]["interfaces"], [])


class TestNetworkEndpoints(NetworksTestCase):
    def test_create_marks_the_node_pending(self):
        r = self.client.post(
            "/api/network/pve-a",
            json={"iface": "vmbr9", "type": "bridge", "bridge_ports": "eno1"},
        )

        self.assertEqual(r.status_code, 200)
        self.proxmox.nodes.return_value.network.post.assert_called_once_with(
            iface="vmbr9", type="bridge", bridge_ports="eno1"
        )
        self.assertIn("pve-a", app.network_pending)

    def test_create_validation_error_is_a_400(self):
        r = self.client.post("/api/network/pve-a", json={"iface": "", "type": "bridge"})

        self.assertEqual(r.status_code, 400)
        self.proxmox.nodes.return_value.network.post.assert_not_called()
        self.assertNotIn("pve-a", app.network_pending)

    def test_update_deletes_the_fields_left_empty(self):
        self.proxmox.nodes.return_value.network.return_value.get.return_value = {
            "type": "bridge",
            "address": "10.0.0.9",
            "netmask": "24",
            "comments": "old",
        }

        r = self.client.put(
            "/api/network/pve-a/vmbr1",
            json={"type": "bridge", "cidr": "10.0.0.9/24"},
        )

        self.assertEqual(r.status_code, 200)
        kwargs = self.proxmox.nodes.return_value.network.return_value.put.call_args[1]
        self.assertEqual(kwargs["cidr"], "10.0.0.9/24")
        self.assertEqual(kwargs["delete"], "comments")

    def test_delete_refuses_the_management_interface(self):
        self._interfaces([{"iface": "vmbr0", "address": "10.0.0.5"}])

        r = self.client.delete("/api/network/pve-a/vmbr0")

        self.assertEqual(r.status_code, 400)
        self.assertIn("cut off", r.get_json()["error"])
        self.proxmox.nodes.return_value.network.return_value.delete.assert_not_called()
        self.assertNotIn("pve-a", app.network_pending)

    def test_delete_removes_a_normal_interface(self):
        self._interfaces([{"iface": "vmbr0", "address": "10.0.0.5"}])

        r = self.client.delete("/api/network/pve-a/vmbr9")

        self.assertEqual(r.status_code, 200)
        self.proxmox.nodes.return_value.network.assert_called_with("vmbr9")
        self.assertIn("pve-a", app.network_pending)

    def test_apply_reloads_and_clears_pending(self):
        app.network_pending["pve-a"] = "2026-09-11T10:00:00"
        self.proxmox.nodes.return_value.network.put.return_value = "UPID:pve-a:1234"

        r = self.client.post("/api/network/pve-a/apply")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["upid"], "UPID:pve-a:1234")
        self.proxmox.nodes.return_value.network.put.assert_called_once_with()
        self.assertNotIn("pve-a", app.network_pending)

    def test_failed_apply_keeps_the_node_pending(self):
        app.network_pending["pve-a"] = "2026-09-11T10:00:00"
        self.proxmox.nodes.return_value.network.put.side_effect = Exception("400 bad")

        r = self.client.post("/api/network/pve-a/apply")

        self.assertEqual(r.status_code, 400)
        self.assertIn("pve-a", app.network_pending)

    def test_revert_discards_pending(self):
        app.network_pending["pve-a"] = "2026-09-11T10:00:00"

        r = self.client.post("/api/network/pve-a/revert")

        self.assertEqual(r.status_code, 200)
        self.proxmox.nodes.return_value.network.delete.assert_called_once_with()
        self.assertNotIn("pve-a", app.network_pending)

    def test_unknown_node_is_a_404(self):
        self.patcher.stop()
        with patch("app.get_proxmox_connection", return_value=None):
            r = self.client.post(
                "/api/network/nope", json={"iface": "vmbr1", "type": "bridge"}
            )
        self.patcher.start()

        self.assertEqual(r.status_code, 404)

    def test_api_networks_returns_the_node_list(self):
        self._interfaces([{"iface": "vmbr0", "type": "bridge", "priority": 1}])

        r = self.client.get("/api/networks")

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["nodes"][0]["node"], "pve-a")


class TestDemoMode(NetworksTestCase):
    def setUp(self):
        super().setUp()
        app.DEMO_MODE = True

    def tearDown(self):
        app.DEMO_MODE = False
        super().tearDown()

    def test_writes_are_blocked(self):
        calls = [
            self.client.post(
                "/api/network/pve-a", json={"iface": "vmbr1", "type": "bridge"}
            ),
            self.client.put("/api/network/pve-a/vmbr1", json={"type": "bridge"}),
            self.client.delete("/api/network/pve-a/vmbr1"),
            self.client.post("/api/network/pve-a/apply"),
            self.client.post("/api/network/pve-a/revert"),
        ]

        for r in calls:
            self.assertEqual(r.status_code, 403)
        self.assertEqual(app.network_pending, {})


if __name__ == "__main__":
    unittest.main()
