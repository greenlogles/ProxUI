"""Tests for the VM/LXC snapshot endpoints."""

import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


class TestSnapshotNameValidation(unittest.TestCase):
    def test_accepts_normal_names(self):
        for name in ["before-upgrade", "snap1", "A_b-2", "ab", "a" * 40]:
            self.assertIsNone(app._validate_snapshot_name(name), name)

    def test_rejects_bad_names(self):
        for name in ["", "1snap", "a", "-snap", "with space", "dot.name", "a" * 41]:
            self.assertIsNotNone(app._validate_snapshot_name(name), name)

    def test_rejects_reserved_current(self):
        self.assertIn("reserved", app._validate_snapshot_name("current"))


class SnapshotTestCase(unittest.TestCase):
    def setUp(self):
        self.flask_app = app.app
        self.flask_app.config["TESTING"] = True
        self.client = self.flask_app.test_client()

        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.connection_metadata.clear()
        app.all_clusters.clear()

        app.all_clusters["cluster-a"] = {"id": "cluster-a", "name": "A", "nodes": []}
        app.current_cluster_id = "cluster-a"
        # check_config() redirects every request to /connect without these.
        self._had_config = app.config_file_exists
        app.config_file_exists = True

        self.proxmox = Mock()
        app.proxmox_nodes["pve-a"] = self.proxmox
        app.cluster_nodes.append(
            {"name": "pve-a", "status": "online", "connection": self.proxmox}
        )
        app.connection_metadata["pve-a"] = {"host": "10.0.0.5", "user": "root@pam"}

        self.patcher = patch("app.get_proxmox_connection", return_value=self.proxmox)
        self.patcher.start()

        self.qemu = self.proxmox.nodes.return_value.qemu.return_value
        self.lxc = self.proxmox.nodes.return_value.lxc.return_value
        # PVE answers every write with a UPID string.
        for guest in (self.qemu, self.lxc):
            guest.snapshot.post.return_value = "UPID:pve-a:0:0:snapshot:100:root@pam:"
            guest.snapshot.return_value.delete.return_value = (
                "UPID:pve-a:0:0:d:100:root@pam:"
            )
            guest.snapshot.return_value.rollback.post.return_value = (
                "UPID:pve-a:0:0:r:100:root@pam:"
            )

    def tearDown(self):
        self.patcher.stop()
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.connection_metadata.clear()
        app.all_clusters.clear()
        app.current_cluster_id = None
        app.config_file_exists = self._had_config

    def _make_lxc(self):
        """Make the QEMU probe fail so the guest is detected as a container."""
        self.qemu.status.current.get.side_effect = Exception("500 no such VM")


class TestSnapshotList(SnapshotTestCase):
    def test_lists_qemu_snapshots_sorted_with_current_split_out(self):
        self.qemu.snapshot.get.return_value = [
            {
                "name": "later",
                "description": "second\n",
                "snaptime": 200,
                "parent": "early",
            },
            {"name": "current", "description": "You are here!", "parent": "later"},
            {"name": "early", "description": "first", "snaptime": 100, "vmstate": 1},
        ]
        r = self.client.get("/api/vm/pve-a/100/snapshots")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["vm_type"], "qemu")
        self.assertEqual([s["name"] for s in data["snapshots"]], ["early", "later"])
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["snapshots"][0]["vmstate"], 1)
        self.assertEqual(data["snapshots"][1]["vmstate"], 0)
        self.assertEqual(data["snapshots"][1]["description"], "second")
        self.assertEqual(data["current"]["name"], "current")
        self.assertEqual(data["current"]["parent"], "later")

    def test_falls_back_to_lxc_when_qemu_probe_fails(self):
        self._make_lxc()
        self.lxc.snapshot.get.return_value = []
        r = self.client.get("/api/vm/pve-a/101/snapshots")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["vm_type"], "lxc")
        self.lxc.snapshot.get.assert_called_once_with()

    def test_reports_api_errors(self):
        self.qemu.snapshot.get.side_effect = Exception(
            "501 storage does not support snapshots"
        )
        r = self.client.get("/api/vm/pve-a/100/snapshots")
        self.assertEqual(r.status_code, 500)
        self.assertIn("does not support snapshots", r.get_json()["error"])

    def test_missing_node_returns_404(self):
        with patch("app.get_proxmox_connection", return_value=None):
            r = self.client.get("/api/vm/nope/100/snapshots")
        self.assertEqual(r.status_code, 404)


class TestSnapshotCreate(SnapshotTestCase):
    def test_creates_qemu_snapshot_with_vmstate(self):
        self.qemu.snapshot.post.return_value = "UPID:pve-a:1:2:qmsnapshot:100:root@pam:"
        r = self.client.post(
            "/api/vm/pve-a/100/snapshots",
            json={"name": "before-upgrade", "description": "pre-apt", "vmstate": True},
        )
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["success"])
        self.assertTrue(body["upid"].startswith("UPID:"))
        self.qemu.snapshot.post.assert_called_once_with(
            snapname="before-upgrade", description="pre-apt", vmstate=1
        )

    def test_omits_empty_description(self):
        self.client.post("/api/vm/pve-a/100/snapshots", json={"name": "plain"})
        self.qemu.snapshot.post.assert_called_once_with(snapname="plain")

    def test_vmstate_is_dropped_for_lxc(self):
        self._make_lxc()
        r = self.client.post(
            "/api/vm/pve-a/101/snapshots", json={"name": "ct-snap", "vmstate": True}
        )
        self.assertEqual(r.status_code, 200)
        self.lxc.snapshot.post.assert_called_once_with(snapname="ct-snap")

    def test_rejects_invalid_name_without_calling_the_api(self):
        r = self.client.post("/api/vm/pve-a/100/snapshots", json={"name": "bad name"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Invalid snapshot name", r.get_json()["error"])
        self.qemu.snapshot.post.assert_not_called()

    def test_rejects_reserved_name(self):
        r = self.client.post("/api/vm/pve-a/100/snapshots", json={"name": "current"})
        self.assertEqual(r.status_code, 400)
        self.qemu.snapshot.post.assert_not_called()

    def test_rejects_missing_name(self):
        r = self.client.post("/api/vm/pve-a/100/snapshots", json={})
        self.assertEqual(r.status_code, 400)
        self.qemu.snapshot.post.assert_not_called()

    def test_surfaces_storage_error(self):
        self.qemu.snapshot.post.side_effect = Exception(
            "400 Parameter verification failed"
        )
        r = self.client.post("/api/vm/pve-a/100/snapshots", json={"name": "snap1"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Parameter verification failed", r.get_json()["error"])


class TestSnapshotDelete(SnapshotTestCase):
    def test_deletes_qemu_snapshot(self):
        self.qemu.snapshot.return_value.delete.return_value = (
            "UPID:pve-a:3:4:qmdelsnapshot:100:root@pam:"
        )
        r = self.client.delete("/api/vm/pve-a/100/snapshots/snap1")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["success"])
        self.qemu.snapshot.assert_called_once_with("snap1")
        self.qemu.snapshot.return_value.delete.assert_called_once_with()

    def test_deletes_lxc_snapshot(self):
        self._make_lxc()
        r = self.client.delete("/api/vm/pve-a/101/snapshots/snap1")
        self.assertEqual(r.status_code, 200)
        self.lxc.snapshot.assert_called_once_with("snap1")
        self.lxc.snapshot.return_value.delete.assert_called_once_with()

    def test_rejects_invalid_name(self):
        r = self.client.delete("/api/vm/pve-a/100/snapshots/current")
        self.assertEqual(r.status_code, 400)
        self.qemu.snapshot.return_value.delete.assert_not_called()

    def test_surfaces_api_error(self):
        self.qemu.snapshot.return_value.delete.side_effect = Exception("403 Forbidden")
        r = self.client.delete("/api/vm/pve-a/100/snapshots/snap1")
        self.assertEqual(r.status_code, 403)


class TestSnapshotRollback(SnapshotTestCase):
    def test_rollback_without_start(self):
        self.qemu.snapshot.return_value.rollback.post.return_value = "UPID:x"
        r = self.client.post("/api/vm/pve-a/100/snapshots/snap1/rollback", json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["upid"], "UPID:x")
        self.qemu.snapshot.assert_called_once_with("snap1")
        self.qemu.snapshot.return_value.rollback.post.assert_called_once_with()

    def test_rollback_with_start(self):
        self.client.post(
            "/api/vm/pve-a/100/snapshots/snap1/rollback", json={"start": True}
        )
        self.qemu.snapshot.return_value.rollback.post.assert_called_once_with(start=1)

    def test_rollback_lxc(self):
        self._make_lxc()
        r = self.client.post("/api/vm/pve-a/101/snapshots/snap1/rollback", json={})
        self.assertEqual(r.status_code, 200)
        self.lxc.snapshot.return_value.rollback.post.assert_called_once_with()

    def test_rejects_invalid_name(self):
        r = self.client.post("/api/vm/pve-a/100/snapshots/bad%20name/rollback", json={})
        self.assertEqual(r.status_code, 400)
        self.qemu.snapshot.return_value.rollback.post.assert_not_called()

    def test_surfaces_api_error(self):
        self.qemu.snapshot.return_value.rollback.post.side_effect = Exception(
            "500 VM is locked (backup)"
        )
        r = self.client.post("/api/vm/pve-a/100/snapshots/snap1/rollback", json={})
        self.assertEqual(r.status_code, 500)
        self.assertIn("locked", r.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
