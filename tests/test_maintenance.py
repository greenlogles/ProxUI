"""Focused tests for node package-maintenance helpers and endpoints."""

import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


class TestMaintenance(unittest.TestCase):
    def setUp(self):
        self.flask_app = app.app
        self.flask_app.config["TESTING"] = True
        self.client = self.flask_app.test_client()

        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        with app.job_queue.lock:
            app.job_queue.jobs.clear()
        with app._node_reboot_status_lock:
            app._node_reboot_status.clear()

        app.all_clusters["cluster-a"] = {
            "id": "cluster-a",
            "name": "Cluster A",
            "nodes": [],
        }
        app.current_cluster_id = "cluster-a"
        self.proxmox = Mock()
        self.proxmox.version.get.return_value = {"version": "8.4"}
        self.proxmox.cluster.status.get.return_value = [
            {"type": "node", "name": "pve-a", "ip": "10.0.0.10"}
        ]
        app.proxmox_nodes["pve-a"] = self.proxmox
        app.cluster_nodes.append(
            {"name": "pve-a", "status": "online", "connection": self.proxmox}
        )

    def tearDown(self):
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        with app.job_queue.lock:
            app.job_queue.jobs.clear()
        with app._node_reboot_status_lock:
            app._node_reboot_status.clear()
        app.current_cluster_id = None

    def test_dedupe_apt_updates_keeps_one_entry_per_package(self):
        updates = [
            {"Package": "pve-kernel", "Version": "1"},
            {"Package": "zfsutils", "Version": "2"},
            {"Package": "pve-kernel", "Version": "1"},
        ]

        self.assertEqual(
            [update["Package"] for update in app._dedupe_apt_updates(updates)],
            ["pve-kernel", "zfsutils"],
        )

    def test_apt_updates_endpoint_returns_deduplicated_updates(self):
        self.proxmox.nodes.return_value.apt.update.get.return_value = [
            {"Package": "zfsutils", "OldVersion": "1", "Version": "2"},
            {"Package": "pve-kernel", "OldVersion": "1", "Version": "2"},
            {"Package": "zfsutils", "OldVersion": "1", "Version": "2"},
        ]

        response = self.client.get("/api/node/pve-a/apt/updates")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [update["Package"] for update in response.get_json()["updates"]],
            ["pve-kernel", "zfsutils"],
        )

    @patch("app.threading.Thread")
    def test_upgrade_job_pins_node_address_and_reuses_active_job(self, mock_thread):
        response = self.client.post("/api/node/pve-a/apt/upgrade")

        self.assertEqual(response.status_code, 200)
        first = response.get_json()
        self.assertTrue(first["success"])
        self.assertNotIn("existing", first)
        mock_thread.assert_called_once()
        self.assertEqual(
            mock_thread.call_args.kwargs["args"],
            (first["job_id"], "pve-a", "10.0.0.10", self.proxmox),
        )
        self.assertEqual(
            app.job_queue.get_job(first["job_id"])["params"],
            {"node": "pve-a", "cluster_id": "cluster-a"},
        )

        duplicate = self.client.post("/api/node/pve-a/apt/upgrade")

        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(
            duplicate.get_json(),
            {
                "success": True,
                "job_id": first["job_id"],
                "existing": True,
            },
        )
        mock_thread.assert_called_once()

    def test_upgrade_requires_node_address(self):
        self.proxmox.cluster.status.get.return_value = []

        response = self.client.post("/api/node/pve-a/apt/upgrade")

        self.assertEqual(response.status_code, 502)
        self.assertIn("Could not resolve", response.get_json()["error"])
