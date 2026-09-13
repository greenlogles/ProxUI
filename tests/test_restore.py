#!/usr/bin/env python3
"""
Unit tests for restoring a guest from a backup archive
"""

import json
import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from app import app as flask_app


class TestParseBackupVolid(unittest.TestCase):
    """The guest type and source VMID come from the archive name, not a probe."""

    def test_qemu_archive(self):
        self.assertEqual(
            app._parse_backup_volid(
                "local:backup/vzdump-qemu-100-2026_01_01-00_00_00.vma.zst"
            ),
            ("local", "qemu", 100),
        )

    def test_lxc_archive(self):
        self.assertEqual(
            app._parse_backup_volid(
                "nas-backup:backup/vzdump-lxc-201-2026_01_01-00_00_00.tar.zst"
            ),
            ("nas-backup", "lxc", 201),
        )

    def test_openvz_archive_is_a_container(self):
        self.assertEqual(
            app._parse_backup_volid(
                "local:backup/vzdump-openvz-105-2015_01_01-00_00_00.tar.lzo"
            ),
            ("local", "lxc", 105),
        )

    def test_pbs_vm_snapshot(self):
        self.assertEqual(
            app._parse_backup_volid("lan-pbs:backup/vm/100/2026-07-12T06:00:09Z"),
            ("lan-pbs", "qemu", 100),
        )

    def test_pbs_ct_snapshot(self):
        self.assertEqual(
            app._parse_backup_volid("lan-pbs:backup/ct/201/2026-07-12T06:00:09Z"),
            ("lan-pbs", "lxc", 201),
        )

    def test_malformed_volids_are_rejected(self):
        for bad in (
            "",
            None,
            "local",
            "local:",
            ":backup/vzdump-qemu-100-2026_01_01-00_00_00.vma.zst",
            "local:iso/debian-12.iso",
            "local:backup/vzdump-qemu-abc-2026_01_01-00_00_00.vma.zst",
            "lan-pbs:backup/host/foo/2026-07-12T06:00:09Z",
        ):
            with self.subTest(volid=bad):
                with self.assertRaises(ValueError):
                    app._parse_backup_volid(bad)


class TestRestoreParams(unittest.TestCase):
    """qemu and lxc restores take genuinely different parameters."""

    def test_qemu_uses_archive(self):
        params = app._restore_params("qemu", "local:backup/a.vma.zst", 100)
        self.assertEqual(params, {"vmid": 100, "archive": "local:backup/a.vma.zst"})

    def test_lxc_uses_ostemplate_and_restore_flag(self):
        params = app._restore_params("lxc", "local:backup/a.tar.zst", 201)
        self.assertEqual(
            params,
            {"vmid": 201, "ostemplate": "local:backup/a.tar.zst", "restore": 1},
        )
        self.assertNotIn("archive", params)

    def test_optional_flags_are_omitted_when_unset(self):
        params = app._restore_params("qemu", "local:backup/a.vma.zst", 100)
        for key in ("force", "unique", "start", "storage"):
            self.assertNotIn(key, params)

    def test_optional_flags_are_included_when_set(self):
        params = app._restore_params(
            "qemu",
            "local:backup/a.vma.zst",
            100,
            storage="local-lvm",
            force=True,
            start=True,
            unique=True,
        )
        self.assertEqual(params["storage"], "local-lvm")
        self.assertEqual(params["force"], 1)
        self.assertEqual(params["start"], 1)
        self.assertEqual(params["unique"], 1)


QEMU_VOLID = "local:backup/vzdump-qemu-100-2026_01_01-00_00_00.vma.zst"
LXC_VOLID = "local:backup/vzdump-lxc-201-2026_01_01-00_00_00.tar.zst"


class TestRestoreEndpoint(unittest.TestCase):

    def setUp(self):
        self.app = flask_app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()

        app.all_clusters["test-cluster"] = {
            "id": "test-cluster",
            "name": "Test Cluster",
            "nodes": [
                {"host": "192.168.1.100", "user": "root@pam", "password": "test"}
            ],
        }
        app.current_cluster_id = "test-cluster"

        self.mock_connection = Mock()
        self.mock_connection.nodes.return_value.qemu.post.return_value = "UPID:qemu"
        self.mock_connection.nodes.return_value.lxc.post.return_value = "UPID:lxc"
        app.proxmox_nodes["test-node"] = self.mock_connection
        app.cluster_nodes.append(
            {
                "name": "test-node",
                "status": "online",
                "connection": self.mock_connection,
            }
        )

    def tearDown(self):
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        app.current_cluster_id = None

    def post(self, payload):
        return self.client.post(
            "/api/backups/restore",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def qemu_kwargs(self):
        return self.mock_connection.nodes.return_value.qemu.post.call_args.kwargs

    def lxc_kwargs(self):
        return self.mock_connection.nodes.return_value.lxc.post.call_args.kwargs

    def test_qemu_restore_posts_to_qemu_with_archive(self):
        response = self.post({"volid": QEMU_VOLID, "node": "test-node"})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["success"])
        self.assertEqual(data["upid"], "UPID:qemu")
        self.assertEqual(data["type"], "qemu")
        self.assertEqual(data["vmid"], 100)

        self.mock_connection.nodes.return_value.lxc.post.assert_not_called()
        self.assertEqual(self.qemu_kwargs()["archive"], QEMU_VOLID)
        self.assertEqual(self.qemu_kwargs()["vmid"], 100)

    def test_lxc_restore_posts_to_lxc_with_ostemplate(self):
        response = self.post({"volid": LXC_VOLID, "node": "test-node"})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data["type"], "lxc")
        self.assertEqual(data["vmid"], 201)

        self.mock_connection.nodes.return_value.qemu.post.assert_not_called()
        kwargs = self.lxc_kwargs()
        self.assertEqual(kwargs["ostemplate"], LXC_VOLID)
        self.assertEqual(kwargs["restore"], 1)
        self.assertNotIn("archive", kwargs)

    def test_force_absent_unless_requested(self):
        response = self.post({"volid": QEMU_VOLID, "node": "test-node"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("force", self.qemu_kwargs())

    def test_force_ignored_when_not_literally_true(self):
        response = self.post(
            {
                "volid": QEMU_VOLID,
                "node": "test-node",
                "force": "yes",
                "confirm_vmid": "100",
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("force", self.qemu_kwargs())

    def test_force_requires_matching_confirmation(self):
        response = self.post(
            {"volid": QEMU_VOLID, "node": "test-node", "vmid": 100, "force": True}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("confirmed", json.loads(response.data)["error"])
        self.mock_connection.nodes.return_value.qemu.post.assert_not_called()

    def test_force_rejected_when_confirmation_names_another_vmid(self):
        response = self.post(
            {
                "volid": QEMU_VOLID,
                "node": "test-node",
                "vmid": 150,
                "force": True,
                "confirm_vmid": "100",
            }
        )
        self.assertEqual(response.status_code, 400)
        self.mock_connection.nodes.return_value.qemu.post.assert_not_called()

    def test_force_passed_when_confirmed(self):
        response = self.post(
            {
                "volid": QEMU_VOLID,
                "node": "test-node",
                "vmid": 150,
                "force": True,
                "confirm_vmid": "150",
            }
        )
        self.assertEqual(response.status_code, 200)
        kwargs = self.qemu_kwargs()
        self.assertEqual(kwargs["force"], 1)
        self.assertEqual(kwargs["vmid"], 150)

    def test_target_vmid_overrides_the_archive_vmid(self):
        response = self.post({"volid": QEMU_VOLID, "node": "test-node", "vmid": "777"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.qemu_kwargs()["vmid"], 777)

    def test_storage_and_flags_forwarded(self):
        response = self.post(
            {
                "volid": QEMU_VOLID,
                "node": "test-node",
                "storage": "local-lvm",
                "start": True,
                "unique": True,
            }
        )
        self.assertEqual(response.status_code, 200)
        kwargs = self.qemu_kwargs()
        self.assertEqual(kwargs["storage"], "local-lvm")
        self.assertEqual(kwargs["start"], 1)
        self.assertEqual(kwargs["unique"], 1)

    def test_blank_storage_is_not_forwarded(self):
        response = self.post(
            {"volid": QEMU_VOLID, "node": "test-node", "storage": "  "}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("storage", self.qemu_kwargs())

    def test_malformed_volid_rejected(self):
        response = self.post({"volid": "local:iso/debian-12.iso", "node": "test-node"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", json.loads(response.data))
        self.mock_connection.nodes.return_value.qemu.post.assert_not_called()
        self.mock_connection.nodes.return_value.lxc.post.assert_not_called()

    def test_missing_volid_rejected(self):
        response = self.post({"node": "test-node"})
        self.assertEqual(response.status_code, 400)

    def test_missing_node_rejected(self):
        response = self.post({"volid": QEMU_VOLID})
        self.assertEqual(response.status_code, 400)

    def test_non_numeric_vmid_rejected(self):
        response = self.post({"volid": QEMU_VOLID, "node": "test-node", "vmid": "abc"})
        self.assertEqual(response.status_code, 400)

    def test_low_vmid_rejected(self):
        response = self.post({"volid": QEMU_VOLID, "node": "test-node", "vmid": 99})
        self.assertEqual(response.status_code, 400)

    def test_no_connection_returns_404(self):
        # An unknown node name still resolves: get_proxmox_for_node() falls back
        # to any cluster connection. Only having none at all is a 404.
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.current_cluster_id = None
        response = self.post({"volid": QEMU_VOLID, "node": "test-node"})
        self.assertEqual(response.status_code, 404)


class TestBackupItemType(unittest.TestCase):
    """The listing carries the guest type so the UI knows what it is restoring."""

    def test_backup_item_tags_guest_type(self):
        item = app._backup_item({"volid": QEMU_VOLID, "vmid": 100}, "local", "test-node")
        self.assertEqual(item["type"], "qemu")

    def test_backup_item_tolerates_an_unparseable_volid(self):
        item = app._backup_item({"volid": "weird", "vmid": None}, "local", "test-node")
        self.assertEqual(item["type"], "")


if __name__ == "__main__":
    unittest.main()
