#!/usr/bin/env python3
"""
Unit tests for cluster storage add/edit/remove endpoints
"""

import json
import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from app import app as flask_app


class TestStorageConfig(unittest.TestCase):

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

    # ------------------------------------------------------------------
    # Create: required-field validation per type
    # ------------------------------------------------------------------

    def test_create_nfs_missing_export(self):
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "nasnfs",
                "type": "nfs",
                "server": "10.0.0.5",
                "content": ["backup"],
            },
        )
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertIn("export", data["error"].lower())
        self.mock_connection.storage.post.assert_not_called()

    def test_create_nfs_missing_server(self):
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "nasnfs",
                "type": "nfs",
                "export": "/tank/data",
                "content": ["backup"],
            },
        )
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertIn("server", data["error"].lower())

    def test_create_cifs_missing_share(self):
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "nascifs",
                "type": "cifs",
                "server": "10.0.0.5",
                "content": ["backup"],
            },
        )
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertIn("share", data["error"].lower())

    def test_create_cifs_missing_server(self):
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "nascifs",
                "type": "cifs",
                "share": "data",
                "content": ["backup"],
            },
        )
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertIn("server", data["error"].lower())

    def test_create_dir_missing_path(self):
        response = self.client.post(
            "/api/storages",
            json={"storage": "localdir", "type": "dir", "content": ["backup"]},
        )
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertIn("path", data["error"].lower())

    def test_create_missing_content(self):
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "localdir",
                "type": "dir",
                "path": "/mnt/data",
                "content": [],
            },
        )
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertIn("content", data["error"].lower())

    def test_create_invalid_type(self):
        response = self.client.post(
            "/api/storages",
            json={"storage": "x", "type": "zfs", "content": ["images"]},
        )
        self.assertEqual(response.status_code, 400)
        self.mock_connection.storage.post.assert_not_called()

    def test_create_invalid_storage_id(self):
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "9bad id!",
                "type": "dir",
                "path": "/mnt/data",
                "content": ["backup"],
            },
        )
        self.assertEqual(response.status_code, 400)

    # ------------------------------------------------------------------
    # Create: content types joined the way the API expects
    # ------------------------------------------------------------------

    def test_create_dir_success_joins_content(self):
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "localdir",
                "type": "dir",
                "path": "/mnt/data",
                "content": ["images", "rootdir", "iso"],
                "mkdir": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["success"])
        self.mock_connection.storage.post.assert_called_once()
        kwargs = self.mock_connection.storage.post.call_args.kwargs
        self.assertEqual(kwargs["storage"], "localdir")
        self.assertEqual(kwargs["type"], "dir")
        self.assertEqual(kwargs["path"], "/mnt/data")
        self.assertEqual(kwargs["content"], "images,rootdir,iso")
        self.assertEqual(kwargs["mkdir"], 1)

    def test_create_dir_ignores_unknown_content_types(self):
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "localdir",
                "type": "dir",
                "path": "/mnt/data",
                "content": ["images", "bogus"],
            },
        )
        self.assertEqual(response.status_code, 200)
        kwargs = self.mock_connection.storage.post.call_args.kwargs
        self.assertEqual(kwargs["content"], "images")

    def test_create_nfs_success(self):
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "nasnfs",
                "type": "nfs",
                "server": "10.0.0.5",
                "export": "/tank/data",
                "options": "vers=4",
                "content": ["backup", "iso"],
                "nodes": ["test-node"],
                "shared": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        kwargs = self.mock_connection.storage.post.call_args.kwargs
        self.assertEqual(kwargs["server"], "10.0.0.5")
        self.assertEqual(kwargs["export"], "/tank/data")
        self.assertEqual(kwargs["options"], "vers=4")
        self.assertEqual(kwargs["content"], "backup,iso")
        self.assertEqual(kwargs["nodes"], "test-node")
        self.assertEqual(kwargs["shared"], 1)

    def test_create_cifs_success(self):
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "nascifs",
                "type": "cifs",
                "server": "10.0.0.5",
                "share": "data",
                "username": "homelab",
                "password": "s3cret",
                "domain": "WORKGROUP",
                "content": ["backup"],
            },
        )
        self.assertEqual(response.status_code, 200)
        kwargs = self.mock_connection.storage.post.call_args.kwargs
        self.assertEqual(kwargs["share"], "data")
        self.assertEqual(kwargs["username"], "homelab")
        self.assertEqual(kwargs["password"], "s3cret")
        self.assertEqual(kwargs["domain"], "WORKGROUP")

    # ------------------------------------------------------------------
    # Update: storage id / type / location are immutable
    # ------------------------------------------------------------------

    def test_update_does_not_send_export_share_path_or_type(self):
        self.mock_connection.storage.return_value.get.return_value = {
            "storage": "nasnfs",
            "type": "nfs",
            "server": "10.0.0.5",
            "export": "/tank/data",
            "content": "backup",
        }
        response = self.client.put(
            "/api/storages/nasnfs",
            json={
                "content": ["backup", "iso"],
                "server": "10.0.0.6",
                "export": "/tank/other",  # must be ignored -- PUT has no such param
                "shared": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.mock_connection.storage.return_value.put.assert_called_once()
        kwargs = self.mock_connection.storage.return_value.put.call_args.kwargs
        self.assertEqual(kwargs["server"], "10.0.0.6")
        self.assertNotIn("export", kwargs)
        self.assertNotIn("type", kwargs)
        self.assertNotIn("storage", kwargs)
        self.assertEqual(kwargs["content"], "backup,iso")

    def test_update_unsupported_type_rejected(self):
        # iscsi needs target/LUN setup ProxUI does not model, so it stays
        # read-only here even though PVE itself accepts the type.
        self.mock_connection.storage.return_value.get.return_value = {
            "storage": "sanstore",
            "type": "iscsi",
        }
        response = self.client.put(
            "/api/storages/sanstore", json={"content": ["images"]}
        )
        self.assertEqual(response.status_code, 400)
        self.mock_connection.storage.return_value.put.assert_not_called()

    # ------------------------------------------------------------------
    # Block, Ceph and PBS types

    def test_create_pbs_requires_datastore(self):
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "pbs1",
                "type": "pbs",
                "server": "pbs.lan",
                "content": ["backup"],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("datastore", json.loads(response.data)["error"].lower())
        self.mock_connection.storage.post.assert_not_called()

    def test_create_pbs_success(self):
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "pbs1",
                "type": "pbs",
                "server": "pbs.lan",
                "datastore": "store1",
                "username": "root@pam",
                "password": "sec",
                "fingerprint": "AA:BB",
                "content": ["backup"],
            },
        )
        self.assertEqual(response.status_code, 200)
        kwargs = self.mock_connection.storage.post.call_args.kwargs
        self.assertEqual(kwargs["type"], "pbs")
        self.assertEqual(kwargs["datastore"], "store1")
        self.assertEqual(kwargs["fingerprint"], "AA:BB")

    def test_create_zfspool_requires_pool(self):
        response = self.client.post(
            "/api/storages",
            json={"storage": "zfs1", "type": "zfspool", "content": ["images"]},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("pool", json.loads(response.data)["error"].lower())

    def test_create_lvmthin_requires_thinpool(self):
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "thin1",
                "type": "lvmthin",
                "vgname": "pve",
                "content": ["images"],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("thin pool", json.loads(response.data)["error"].lower())

    def test_create_lvmthin_success(self):
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "thin1",
                "type": "lvmthin",
                "vgname": "pve",
                "thinpool": "data",
                "content": ["images", "rootdir"],
            },
        )
        self.assertEqual(response.status_code, 200)
        kwargs = self.mock_connection.storage.post.call_args.kwargs
        self.assertEqual(kwargs["vgname"], "pve")
        self.assertEqual(kwargs["thinpool"], "data")

    def test_create_rbd_allows_hyperconverged_without_monhost(self):
        # A Proxmox-managed Ceph cluster supplies monhost/keyring itself.
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "ceph1",
                "type": "rbd",
                "pool": "rbd",
                "content": ["images"],
            },
        )
        self.assertEqual(response.status_code, 200)
        kwargs = self.mock_connection.storage.post.call_args.kwargs
        self.assertEqual(kwargs["pool"], "rbd")
        self.assertNotIn("monhost", kwargs)
        self.assertNotIn("keyring", kwargs)

    def test_update_lvmthin_does_not_send_create_only_keys(self):
        self.mock_connection.storage.return_value.get.return_value = {
            "storage": "thin1",
            "type": "lvmthin",
        }
        response = self.client.put(
            "/api/storages/thin1",
            json={"vgname": "other", "thinpool": "other", "content": ["images"]},
        )
        self.assertEqual(response.status_code, 200)
        kwargs = self.mock_connection.storage.return_value.put.call_args.kwargs
        for key in ("vgname", "thinpool", "type", "storage"):
            self.assertNotIn(key, kwargs)

    def test_update_zfspool_does_not_repoint_the_pool(self):
        self.mock_connection.storage.return_value.get.return_value = {
            "storage": "zfs1",
            "type": "zfspool",
        }
        response = self.client.put(
            "/api/storages/zfs1",
            json={"pool": "other", "content": ["images"]},
        )
        self.assertEqual(response.status_code, 200)
        kwargs = self.mock_connection.storage.return_value.put.call_args.kwargs
        self.assertNotIn("pool", kwargs)

    def test_keyring_is_not_echoed_in_an_error(self):
        secret = "[client.admin] key = SUPERSECRET"
        self.mock_connection.storage.post.side_effect = Exception(
            f"400 Bad Request: bad value '{secret}' for keyring"
        )
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "ceph1",
                "type": "rbd",
                "pool": "rbd",
                "keyring": secret,
                "content": ["images"],
            },
        )
        self.assertNotIn("SUPERSECRET", response.get_data(as_text=True))

    # ------------------------------------------------------------------
    # Delete: destructive confirmation
    # ------------------------------------------------------------------

    def test_delete_requires_matching_confirmation(self):
        response = self.client.delete(
            "/api/storages/nasnfs", json={"confirm": "wrong-name"}
        )
        self.assertEqual(response.status_code, 400)
        self.mock_connection.storage.return_value.delete.assert_not_called()

    def test_delete_success(self):
        response = self.client.delete(
            "/api/storages/nasnfs", json={"confirm": "nasnfs"}
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertTrue(data["success"])
        self.assertIn("not deleted", data["message"])
        self.mock_connection.storage.return_value.delete.assert_called_once()

    # ------------------------------------------------------------------
    # Security: CIFS password never leaks into a response payload
    # ------------------------------------------------------------------

    def test_password_not_in_create_response(self):
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "nascifs",
                "type": "cifs",
                "server": "10.0.0.5",
                "share": "data",
                "password": "s3cretpw",
                "content": ["backup"],
            },
        )
        self.assertNotIn(b"s3cretpw", response.data)

    def test_password_not_in_storage_list(self):
        self.mock_connection.storage.get.return_value = [
            {
                "storage": "nascifs",
                "type": "cifs",
                "server": "10.0.0.5",
                "share": "data",
            }
        ]
        response = self.client.get("/api/storages")
        self.assertNotIn(b"password", response.data)

    def test_password_not_in_create_error_response(self):
        self.mock_connection.storage.post.side_effect = Exception(
            "400 Bad Request: parameter verification failed"
        )
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "nascifs",
                "type": "cifs",
                "server": "10.0.0.5",
                "share": "data",
                "password": "s3cretpw",
                "content": ["backup"],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(b"s3cretpw", response.data)

    def test_password_redacted_when_echoed_by_api_error(self):
        # Defense in depth: even if a PVE error ever echoed a submitted value
        # back, the password must not reach the client.
        self.mock_connection.storage.post.side_effect = Exception(
            "400 Bad Request: bad value 's3cretpw' for password"
        )
        response = self.client.post(
            "/api/storages",
            json={
                "storage": "nascifs",
                "type": "cifs",
                "server": "10.0.0.5",
                "share": "data",
                "password": "s3cretpw",
                "content": ["backup"],
            },
        )
        self.assertNotIn(b"s3cretpw", response.data)

    def test_password_not_in_scan_cifs_response_on_error(self):
        self.mock_connection.nodes.return_value.scan.cifs.get.side_effect = Exception(
            "401 auth failed for user with password s3cretpw"
        )
        response = self.client.post(
            "/api/storage-scan/cifs",
            json={"node": "test-node", "server": "10.0.0.5", "password": "s3cretpw"},
        )
        self.assertNotIn(b"s3cretpw", response.data)

    # ------------------------------------------------------------------
    # Scan helpers
    # ------------------------------------------------------------------

    def test_scan_nfs_requires_server(self):
        response = self.client.post("/api/storage-scan/nfs", json={"node": "test-node"})
        self.assertEqual(response.status_code, 400)

    def test_scan_nfs_success(self):
        self.mock_connection.nodes.return_value.scan.nfs.get.return_value = [
            {"path": "/tank/data", "options": "rw"}
        ]
        response = self.client.post(
            "/api/storage-scan/nfs",
            json={"node": "test-node", "server": "10.0.0.5"},
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data[0]["path"], "/tank/data")
        self.mock_connection.nodes.return_value.scan.nfs.get.assert_called_with(
            server="10.0.0.5"
        )

    def test_scan_cifs_no_connection(self):
        # No proxmox_nodes/cluster_nodes at all -> get_proxmox_connection has
        # nothing to fall back to and returns None. current_cluster_id is also
        # cleared: otherwise get_proxmox_for_node self-heals by calling
        # init_proxmox_connections, which dials the fixture host for real and
        # blocks the suite for the full 30s connect timeout.
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        self.addCleanup(setattr, app, "current_cluster_id", app.current_cluster_id)
        app.current_cluster_id = None
        response = self.client.post(
            "/api/storage-scan/cifs",
            json={"node": "missing-node", "server": "10.0.0.5"},
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
