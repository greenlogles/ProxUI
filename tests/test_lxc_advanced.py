"""Unit tests for LXC advanced configuration helpers and API routes."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from app import app as flask_app


class TestParseLxcFeatures(unittest.TestCase):
    def test_empty_string(self):
        self.assertEqual(app.parse_lxc_features(""), {})

    def test_none(self):
        self.assertEqual(app.parse_lxc_features(None), {})

    def test_single_flag(self):
        self.assertEqual(app.parse_lxc_features("nesting=1"), {"nesting": "1"})

    def test_multiple_flags(self):
        result = app.parse_lxc_features("nesting=1,keyctl=1,fuse=1")
        self.assertEqual(result, {"nesting": "1", "keyctl": "1", "fuse": "1"})

    def test_flag_without_value(self):
        self.assertEqual(app.parse_lxc_features("nesting"), {"nesting": "1"})

    def test_whitespace_tolerance(self):
        result = app.parse_lxc_features(" nesting=1 , keyctl=1 ")
        self.assertEqual(result, {"nesting": "1", "keyctl": "1"})

    def test_unknown_flags_preserved(self):
        result = app.parse_lxc_features("nesting=1,force_rw_sys=1")
        self.assertEqual(result, {"nesting": "1", "force_rw_sys": "1"})


class TestSerializeLxcFeatures(unittest.TestCase):
    def test_empty_dict(self):
        self.assertEqual(app.serialize_lxc_features({}), "")

    def test_single_enabled_flag(self):
        self.assertEqual(app.serialize_lxc_features({"nesting": "1"}), "nesting=1")

    def test_disabled_flags_excluded(self):
        result = app.serialize_lxc_features({"nesting": "1", "keyctl": "0"})
        self.assertIn("nesting=1", result)
        self.assertNotIn("keyctl", result)

    def test_canonical_order(self):
        flags = {"mount": "1", "nesting": "1", "fuse": "1"}
        parts = app.serialize_lxc_features(flags).split(",")
        self.assertLess(parts.index("nesting=1"), parts.index("fuse=1"))
        self.assertLess(parts.index("fuse=1"), parts.index("mount=1"))

    def test_roundtrip(self):
        original = "nesting=1,keyctl=1,fuse=1"
        parsed = app.parse_lxc_features(original)
        serialized = app.serialize_lxc_features(parsed)
        self.assertEqual(app.parse_lxc_features(serialized), parsed)

    def test_all_disabled_returns_empty(self):
        flags = {f: "0" for f, _ in app.LXC_FEATURE_FLAGS}
        self.assertEqual(app.serialize_lxc_features(flags), "")

    def test_unknown_flags_included(self):
        result = app.serialize_lxc_features({"nesting": "1", "force_rw_sys": "1"})
        self.assertIn("nesting=1", result)
        self.assertIn("force_rw_sys=1", result)


class TestLxcFeaturesApi(unittest.TestCase):
    def setUp(self):
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()

        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        app.lxc_config_backups.clear()

        app.all_clusters["test-cluster"] = {
            "id": "test-cluster",
            "name": "Test Cluster",
            "nodes": [{"host": "192.168.1.100", "user": "root@pam", "password": "test"}],
        }
        app.current_cluster_id = "test-cluster"

        self.mock_proxmox = Mock()
        app.proxmox_nodes["test-node"] = self.mock_proxmox
        app.cluster_nodes.append(
            {"name": "test-node", "status": "online", "connection": self.mock_proxmox}
        )

    def tearDown(self):
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        app.lxc_config_backups.clear()
        app.current_cluster_id = None

    def _csrf(self):
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "test-token"
        return "test-token"

    def test_get_features_returns_parsed_flags(self):
        self.mock_proxmox.nodes.return_value.lxc.return_value.config.get.return_value = {
            "features": "nesting=1,keyctl=1"
        }
        self.mock_proxmox.nodes.return_value.lxc.return_value.status.current.get.return_value = {
            "status": "stopped"
        }
        self.mock_proxmox.access.permissions.get.return_value = {"VM.Config.Options": 1}

        self._csrf()
        resp = self.client.get("/api/vm/test-node/100/lxc/features")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["features"]["nesting"], "1")
        self.assertEqual(data["features"]["keyctl"], "1")
        self.assertFalse(data["running"])
        self.assertTrue(data["can_write"])

    def test_get_features_running_container(self):
        self.mock_proxmox.nodes.return_value.lxc.return_value.config.get.return_value = {}
        self.mock_proxmox.nodes.return_value.lxc.return_value.status.current.get.return_value = {
            "status": "running"
        }
        self.mock_proxmox.access.permissions.get.return_value = {"VM.Config.Options": 1}

        self._csrf()
        resp = self.client.get("/api/vm/test-node/100/lxc/features")
        data = resp.get_json()
        self.assertTrue(data["running"])

    def test_put_features_success(self):
        self.mock_proxmox.nodes.return_value.lxc.return_value.config.get.return_value = {
            "features": "nesting=1"
        }
        self.mock_proxmox.nodes.return_value.lxc.return_value.status.current.get.return_value = {
            "status": "stopped"
        }
        self.mock_proxmox.nodes.return_value.lxc.return_value.config.put.return_value = None

        token = self._csrf()
        resp = self.client.put(
            "/api/vm/test-node/100/lxc/features",
            data=json.dumps({"features": {"nesting": "1", "keyctl": "1"}}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertIn("nesting=1", data["features_raw"])
        self.assertIn("keyctl=1", data["features_raw"])

    def test_put_features_creates_backup(self):
        self.mock_proxmox.nodes.return_value.lxc.return_value.config.get.return_value = {
            "features": "nesting=1"
        }
        self.mock_proxmox.nodes.return_value.lxc.return_value.status.current.get.return_value = {
            "status": "stopped"
        }
        self.mock_proxmox.nodes.return_value.lxc.return_value.config.put.return_value = None

        token = self._csrf()
        self.client.put(
            "/api/vm/test-node/200/lxc/features",
            data=json.dumps({"features": {"nesting": "1"}}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        self.assertIn("test-node:200", app.lxc_config_backups)
        self.assertEqual(app.lxc_config_backups["test-node:200"]["features"], "nesting=1")

    def test_put_features_running_sets_restart_required(self):
        self.mock_proxmox.nodes.return_value.lxc.return_value.config.get.return_value = {}
        self.mock_proxmox.nodes.return_value.lxc.return_value.status.current.get.return_value = {
            "status": "running"
        }
        self.mock_proxmox.nodes.return_value.lxc.return_value.config.put.return_value = None

        token = self._csrf()
        resp = self.client.put(
            "/api/vm/test-node/300/lxc/features",
            data=json.dumps({"features": {"nesting": "1"}}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        data = resp.get_json()
        self.assertTrue(data["restart_required"])
        self.assertIn("restart", data["message"].lower())

    def test_config_snapshot_returns_backup(self):
        app.lxc_config_backups["test-node:100"] = {
            "config": {},
            "timestamp": "2026-06-24T12:00:00",
            "features": "nesting=1",
        }
        resp = self.client.get("/api/vm/test-node/100/lxc/config-snapshot")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["features"], "nesting=1")
        self.assertEqual(data["timestamp"], "2026-06-24T12:00:00")

    def test_config_snapshot_404_when_no_backup(self):
        resp = self.client.get("/api/vm/test-node/999/lxc/config-snapshot")
        self.assertEqual(resp.status_code, 404)

    def test_config_restore_success(self):
        app.lxc_config_backups["test-node:400"] = {
            "config": {"features": "nesting=1"},
            "timestamp": "2026-06-24T00:00:00",
            "features": "nesting=1",
        }
        self.mock_proxmox.nodes.return_value.lxc.return_value.config.get.return_value = {
            "features": "nesting=1,keyctl=1"
        }
        self.mock_proxmox.nodes.return_value.lxc.return_value.config.put.return_value = None

        token = self._csrf()
        resp = self.client.post(
            "/api/vm/test-node/400/lxc/config-restore",
            data=json.dumps({}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertNotIn("test-node:400", app.lxc_config_backups)

    def test_config_restore_clears_backup(self):
        app.lxc_config_backups["test-node:500"] = {
            "config": {},
            "timestamp": "2026-06-24T00:00:00",
            "features": "",
        }
        self.mock_proxmox.nodes.return_value.lxc.return_value.config.get.return_value = {}
        self.mock_proxmox.nodes.return_value.lxc.return_value.config.put.return_value = None

        token = self._csrf()
        self.client.post(
            "/api/vm/test-node/500/lxc/config-restore",
            data=json.dumps({}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        self.assertNotIn("test-node:500", app.lxc_config_backups)

    def test_config_restore_404_when_no_backup(self):
        token = self._csrf()
        resp = self.client.post(
            "/api/vm/test-node/999/lxc/config-restore",
            data=json.dumps({}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(resp.status_code, 404)


class TestParseLxcDev(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(app.parse_lxc_dev(""), {})

    def test_path_only(self):
        result = app.parse_lxc_dev("/dev/dri/renderD128")
        self.assertEqual(result["path"], "/dev/dri/renderD128")
        self.assertNotIn("gid", result)

    def test_with_gid_uid(self):
        result = app.parse_lxc_dev("/dev/dri/renderD128,gid=104,uid=0")
        self.assertEqual(result["path"], "/dev/dri/renderD128")
        self.assertEqual(result["gid"], "104")
        self.assertEqual(result["uid"], "0")

    def test_roundtrip(self):
        original = "/dev/dri/renderD128,gid=104,uid=0"
        parsed = app.parse_lxc_dev(original)
        serialized = app.serialize_lxc_dev(parsed)
        reparsed = app.parse_lxc_dev(serialized)
        self.assertEqual(reparsed["path"], parsed["path"])
        self.assertEqual(reparsed["gid"], parsed["gid"])


class TestParseLxcMp(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(app.parse_lxc_mp(""), {})

    def test_host_path_only(self):
        result = app.parse_lxc_mp("/mnt/data,mp=/data")
        self.assertEqual(result["host_path"], "/mnt/data")
        self.assertEqual(result["mp"], "/data")

    def test_with_ro_backup(self):
        result = app.parse_lxc_mp("/mnt/data,mp=/data,ro=1,backup=0")
        self.assertEqual(result["ro"], "1")
        self.assertEqual(result["backup"], "0")

    def test_is_bind_mount_true(self):
        self.assertTrue(app.is_lxc_bind_mount("/mnt/data,mp=/data"))

    def test_is_bind_mount_false_for_volume(self):
        self.assertFalse(app.is_lxc_bind_mount("local-lvm:subvol-100-disk-0,size=8G"))

    def test_serialize_roundtrip(self):
        mp_dict = {"host_path": "/mnt/data", "mp": "/data", "ro": "1", "backup": "0"}
        serialized = app.serialize_lxc_mp(mp_dict)
        reparsed = app.parse_lxc_mp(serialized)
        self.assertEqual(reparsed["host_path"], "/mnt/data")
        self.assertEqual(reparsed["mp"], "/data")
        self.assertEqual(reparsed["ro"], "1")


class TestGetPveVersionTuple(unittest.TestCase):
    def setUp(self):
        app.connection_metadata.clear()

    def tearDown(self):
        app.connection_metadata.clear()

    def test_known_version(self):
        app.connection_metadata["test-node"] = {"pve_version": "8.2.4"}
        self.assertEqual(app.get_pve_version_tuple("test-node"), (8, 2, 4))

    def test_version_with_dash(self):
        app.connection_metadata["test-node"] = {"pve_version": "8.2-4"}
        ver = app.get_pve_version_tuple("test-node")
        self.assertEqual(ver[0], 8)
        self.assertEqual(ver[1], 2)

    def test_missing_node(self):
        self.assertEqual(app.get_pve_version_tuple("unknown-node"), (0, 0, 0))

    def test_supports_dev_n(self):
        app.connection_metadata["pve82"] = {"pve_version": "8.2.0"}
        self.assertGreaterEqual(app.get_pve_version_tuple("pve82"), (8, 2, 0))

    def test_no_dev_n_support(self):
        app.connection_metadata["pve7"] = {"pve_version": "7.4.1"}
        self.assertLess(app.get_pve_version_tuple("pve7"), (8, 2, 0))


class TestNextKeyIndex(unittest.TestCase):
    def test_empty_config(self):
        self.assertEqual(app._next_key_index({}, "dev"), 0)

    def test_fills_gap(self):
        config = {"dev0": "x", "dev2": "x"}
        self.assertEqual(app._next_key_index(config, "dev"), 1)

    def test_appends_after_last(self):
        config = {"mp0": "x", "mp1": "x"}
        self.assertEqual(app._next_key_index(config, "mp"), 2)

    def test_ignores_other_prefixes(self):
        config = {"mp0": "x", "dev0": "x"}
        self.assertEqual(app._next_key_index(config, "dev"), 1)


class TestLxcDevicesApi(unittest.TestCase):
    def setUp(self):
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        app.lxc_config_backups.clear()
        app.all_clusters["test-cluster"] = {
            "id": "test-cluster", "name": "Test", "nodes": [{"host": "192.168.1.100", "user": "root@pam", "password": "test"}],
        }
        app.current_cluster_id = "test-cluster"
        app.connection_metadata["test-node"] = {"pve_version": "8.2.4"}
        self.mock = Mock()
        app.proxmox_nodes["test-node"] = self.mock
        app.cluster_nodes.append({"name": "test-node", "status": "online", "connection": self.mock})

    def tearDown(self):
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        app.lxc_config_backups.clear()
        app.current_cluster_id = None

    def _csrf(self):
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "tok"
        return "tok"

    def test_list_devices_empty(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {}
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        self.mock.access.permissions.get.return_value = {"VM.Config.Options": 1}
        resp = self.client.get("/api/vm/test-node/100/lxc/devices")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["devices"], [])
        self.assertTrue(data["pve_supports_dev_n"])

    def test_list_devices_with_dev_n(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {
            "dev0": "/dev/dri/renderD128,gid=104,uid=0"
        }
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        self.mock.access.permissions.get.return_value = {"VM.Config.Options": 1}
        resp = self.client.get("/api/vm/test-node/100/lxc/devices")
        data = resp.get_json()
        self.assertEqual(len(data["devices"]), 1)
        self.assertEqual(data["devices"][0]["path"], "/dev/dri/renderD128")
        self.assertEqual(data["devices"][0]["gid"], "104")

    def test_add_device_pve82(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {}
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        self.mock.nodes.return_value.lxc.return_value.config.put.return_value = None
        token = self._csrf()
        resp = self.client.post(
            "/api/vm/test-node/100/lxc/devices",
            data=json.dumps({"path": "/dev/dri/renderD128", "gid": "104", "uid": "0"}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertIn("dev0", data["key"])

    def test_add_device_rejected_if_running(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {}
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "running"}
        token = self._csrf()
        resp = self.client.post(
            "/api/vm/test-node/100/lxc/devices",
            data=json.dumps({"path": "/dev/dri/renderD128"}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.get_json()["stop_required"])

    def test_delete_device(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {
            "dev0": "/dev/dri/renderD128,gid=104"
        }
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        self.mock.nodes.return_value.lxc.return_value.config.put.return_value = None
        token = self._csrf()
        resp = self.client.delete(
            "/api/vm/test-node/100/lxc/devices/dev0",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])

    def test_delete_device_rejected_if_running(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {"dev0": "/dev/dri/card0"}
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "running"}
        token = self._csrf()
        resp = self.client.delete(
            "/api/vm/test-node/100/lxc/devices/dev0",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(resp.status_code, 409)


class TestLxcMountsApi(unittest.TestCase):
    def setUp(self):
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        app.lxc_config_backups.clear()
        app.all_clusters["test-cluster"] = {
            "id": "test-cluster", "name": "Test", "nodes": [{"host": "192.168.1.100", "user": "root@pam", "password": "test"}],
        }
        app.current_cluster_id = "test-cluster"
        app.connection_metadata["test-node"] = {"pve_version": "9.0.0", "user": "root@pam"}
        self.mock = Mock()
        app.proxmox_nodes["test-node"] = self.mock
        app.cluster_nodes.append({"name": "test-node", "status": "online", "connection": self.mock})

    def tearDown(self):
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        app.lxc_config_backups.clear()
        app.current_cluster_id = None

    def _csrf(self):
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "tok"
        return "tok"

    def test_list_mounts_empty(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {}
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        self.mock.access.permissions.get.return_value = {"VM.Config.Options": 1}
        resp = self.client.get("/api/vm/test-node/100/lxc/mounts")
        data = resp.get_json()
        self.assertEqual(data["mounts"], [])

    def test_list_mounts_only_bind_mounts(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {
            "mp0": "/mnt/data,mp=/data,ro=0",
            "mp1": "local-lvm:subvol-100-disk-0,mp=/var/lib,size=8G",
        }
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        self.mock.access.permissions.get.return_value = {"VM.Config.Options": 1}
        resp = self.client.get("/api/vm/test-node/100/lxc/mounts")
        data = resp.get_json()
        self.assertEqual(len(data["mounts"]), 1)
        self.assertEqual(data["mounts"][0]["key"], "mp0")

    def test_add_mount_success(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {}
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        self.mock.nodes.return_value.lxc.return_value.config.put.return_value = None
        token = self._csrf()
        resp = self.client.post(
            "/api/vm/test-node/100/lxc/mounts",
            data=json.dumps({"host_path": "/mnt/nvme/data", "mp": "/data", "ro": False, "backup": False}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["key"], "mp0")

    def test_add_mount_rejected_if_running(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {}
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "running"}
        token = self._csrf()
        resp = self.client.post(
            "/api/vm/test-node/100/lxc/mounts",
            data=json.dumps({"host_path": "/mnt/data", "mp": "/data"}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(resp.status_code, 409)

    def test_add_mount_requires_absolute_host_path(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {}
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        token = self._csrf()
        resp = self.client.post(
            "/api/vm/test-node/100/lxc/mounts",
            data=json.dumps({"host_path": "relative/path", "mp": "/data"}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(resp.status_code, 400)

    def test_delete_mount(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {
            "mp0": "/mnt/data,mp=/data"
        }
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        self.mock.nodes.return_value.lxc.return_value.config.put.return_value = None
        token = self._csrf()
        resp = self.client.delete(
            "/api/vm/test-node/100/lxc/mounts/mp0",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])


class TestParseLxcIdmap(unittest.TestCase):
    def test_full_format(self):
        result = app.parse_lxc_idmap_line("lxc.idmap = u 0 100000 65536")
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "u")
        self.assertEqual(result["ct_id"], 0)
        self.assertEqual(result["host_id"], 100000)
        self.assertEqual(result["count"], 65536)

    def test_bare_format(self):
        result = app.parse_lxc_idmap_line("g 0 100000 65536")
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "g")

    def test_invalid_type(self):
        self.assertIsNone(app.parse_lxc_idmap_line("lxc.idmap = x 0 0 1"))

    def test_non_idmap_line(self):
        self.assertIsNone(app.parse_lxc_idmap_line("lxc.cgroup2.devices.allow = c 226:128 rwm"))

    def test_empty(self):
        self.assertIsNone(app.parse_lxc_idmap_line(""))

    def test_serialise_roundtrip(self):
        m = {"type": "u", "ct_id": 0, "host_id": 100000, "count": 65536}
        raw = app.serialize_lxc_idmap_line(m)
        self.assertIn("lxc.idmap", raw)
        reparsed = app.parse_lxc_idmap_line(raw)
        self.assertEqual(reparsed["host_id"], 100000)

    def test_collect_idmaps_filters_correctly(self):
        config = {
            "lxc0": "lxc.idmap = u 0 100000 65536",
            "lxc1": "lxc.idmap = g 0 100000 65536",
            "lxc2": "lxc.cgroup2.devices.allow = c 226:128 rwm",
            "dev0": "/dev/dri/renderD128",
        }
        maps = app.collect_lxc_idmaps(config)
        self.assertEqual(len(maps), 2)
        types = {m["type"] for m in maps}
        self.assertIn("u", types)
        self.assertIn("g", types)


class TestLxcIdmapApi(unittest.TestCase):
    def setUp(self):
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        app.lxc_config_backups.clear()
        app.all_clusters["test-cluster"] = {
            "id": "test-cluster", "name": "T",
            "nodes": [{"host": "192.168.1.100", "user": "root@pam", "password": "t"}],
        }
        app.current_cluster_id = "test-cluster"
        self.mock = Mock()
        app.proxmox_nodes["test-node"] = self.mock
        app.cluster_nodes.append({"name": "test-node", "status": "online", "connection": self.mock})

    def tearDown(self):
        app.proxmox_nodes.clear(); app.cluster_nodes.clear(); app.all_clusters.clear()
        app.connection_metadata.clear(); app.lxc_config_backups.clear()
        app.current_cluster_id = None

    def _csrf(self):
        with self.client.session_transaction() as sess:
            sess["_csrf_token"] = "tok"
        return "tok"

    def test_list_idmaps(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {
            "lxc0": "lxc.idmap = u 0 100000 65536",
            "lxc1": "lxc.idmap = g 0 100000 65536",
        }
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        self.mock.access.permissions.get.return_value = {"VM.Config.Options": 1}
        resp = self.client.get("/api/vm/test-node/100/lxc/idmap")
        data = resp.get_json()
        self.assertEqual(len(data["idmaps"]), 2)

    def test_add_idmap_success(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {}
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        self.mock.nodes.return_value.lxc.return_value.config.put.return_value = None
        token = self._csrf()
        resp = self.client.post(
            "/api/vm/test-node/100/lxc/idmap",
            data=json.dumps({"type": "u", "ct_id": 0, "host_id": 100000, "count": 65536}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertIn("lxc.idmap", data["raw"])

    def test_add_idmap_rejected_if_running(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {}
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "running"}
        token = self._csrf()
        resp = self.client.post(
            "/api/vm/test-node/100/lxc/idmap",
            data=json.dumps({"type": "u", "ct_id": 0, "host_id": 100000, "count": 65536}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(resp.status_code, 409)

    def test_add_idmap_invalid_type(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {}
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        token = self._csrf()
        resp = self.client.post(
            "/api/vm/test-node/100/lxc/idmap",
            data=json.dumps({"type": "x", "ct_id": 0, "host_id": 100000, "count": 65536}),
            content_type="application/json",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(resp.status_code, 400)

    def test_delete_idmap_success(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {
            "lxc0": "lxc.idmap = u 0 100000 65536"
        }
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        self.mock.nodes.return_value.lxc.return_value.config.put.return_value = None
        token = self._csrf()
        resp = self.client.delete(
            "/api/vm/test-node/100/lxc/idmap/lxc0",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["success"])

    def test_delete_idmap_wrong_key_type(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {
            "lxc0": "lxc.cgroup2.devices.allow = c 226:128 rwm"
        }
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        token = self._csrf()
        resp = self.client.delete(
            "/api/vm/test-node/100/lxc/idmap/lxc0",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(resp.status_code, 400)


class TestComputeProfileDiff(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "name": "Test",
            "features": {"nesting": "1", "keyctl": "1"},
            "devices": [{"path": "/dev/dri/renderD128", "gid": "104", "uid": "0"}],
        }

    def test_all_missing(self):
        diff = app.compute_profile_diff(self.profile, {})
        self.assertIn("nesting=1", diff["features_to_add"])
        self.assertIn("keyctl=1", diff["features_to_add"])
        self.assertEqual(len(diff["devices_to_add"]), 1)
        self.assertEqual(diff["devices_to_add"][0]["path"], "/dev/dri/renderD128")
        self.assertTrue(diff["changes_needed"])

    def test_features_already_set(self):
        config = {"features": "nesting=1,keyctl=1"}
        diff = app.compute_profile_diff(self.profile, config)
        self.assertEqual(diff["features_to_add"], [])
        self.assertIn("nesting=1", diff["features_already_set"])
        self.assertIn("keyctl=1", diff["features_already_set"])

    def test_device_already_present(self):
        config = {"dev0": "/dev/dri/renderD128,gid=104,uid=0"}
        diff = app.compute_profile_diff(self.profile, config)
        self.assertEqual(diff["devices_to_add"], [])
        self.assertEqual(len(diff["devices_already_present"]), 1)

    def test_no_changes_needed(self):
        config = {
            "features": "nesting=1,keyctl=1",
            "dev0": "/dev/dri/renderD128,gid=104,uid=0",
        }
        diff = app.compute_profile_diff(self.profile, config)
        self.assertFalse(diff["changes_needed"])

    def test_partial_features(self):
        config = {"features": "nesting=1"}
        diff = app.compute_profile_diff(self.profile, config)
        self.assertNotIn("nesting=1", diff["features_to_add"])
        self.assertIn("keyctl=1", diff["features_to_add"])
        self.assertTrue(diff["changes_needed"])


class TestLxcProfilesApi(unittest.TestCase):
    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        self.client = flask_app.test_client()
        self.mock = MagicMock()

        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        app.lxc_config_backups.clear()

        app.all_clusters["test-cluster"] = {
            "id": "test-cluster",
            "name": "Test Cluster",
            "nodes": [{"host": "192.168.1.100", "user": "root@pam", "password": "test"}],
        }
        app.current_cluster_id = "test-cluster"
        app.proxmox_nodes["test-node"] = self.mock
        app.cluster_nodes.append({"name": "test-node", "status": "online", "connection": self.mock})
        app.connection_metadata["test-node"] = {"pve_version": "8.2.4", "user": "root@pam"}

        # Inject test profiles
        self._orig_profiles = app.LXC_PROFILES
        app.LXC_PROFILES = {
            "jellyfin": {
                "name": "Jellyfin",
                "description": "Test profile",
                "tags": ["media"],
                "features": {"nesting": "1"},
                "devices": [{"path": "/dev/dri/renderD128", "gid": "104", "uid": "0"}],
            }
        }

    def tearDown(self):
        app.LXC_PROFILES = self._orig_profiles
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        app.lxc_config_backups.clear()
        app.current_cluster_id = None

    def _csrf(self):
        with self.client.session_transaction() as sess:
            token = "test-csrf-token"
            sess["_csrf_token"] = token
        return token

    def test_list_profiles(self):
        resp = self.client.get("/api/lxc-profiles")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("profiles", data)
        self.assertEqual(len(data["profiles"]), 1)
        p = data["profiles"][0]
        self.assertEqual(p["id"], "jellyfin")
        self.assertEqual(p["name"], "Jellyfin")
        self.assertIn("description", p)
        self.assertIn("tags", p)

    def test_profile_diff_no_config(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {}
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        resp = self.client.get("/api/vm/test-node/100/lxc/profile-diff/jellyfin")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("nesting=1", data["features_to_add"])
        self.assertTrue(data["changes_needed"])

    def test_profile_diff_already_applied(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {
            "features": "nesting=1",
            "dev0": "/dev/dri/renderD128,gid=104,uid=0",
        }
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        resp = self.client.get("/api/vm/test-node/100/lxc/profile-diff/jellyfin")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["changes_needed"])

    def test_profile_diff_unknown_id(self):
        resp = self.client.get("/api/vm/test-node/100/lxc/profile-diff/nonexistent")
        self.assertEqual(resp.status_code, 404)

    def test_profile_apply_success(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {}
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        self.mock.nodes.return_value.lxc.return_value.config.put.return_value = None
        token = self._csrf()
        resp = self.client.post(
            "/api/vm/test-node/100/lxc/profile-apply/jellyfin",
            headers={"X-CSRF-Token": token, "Content-Type": "application/json"},
            data=json.dumps({}),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])

    def test_profile_apply_already_applied(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {
            "features": "nesting=1",
            "dev0": "/dev/dri/renderD128,gid=104,uid=0",
        }
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "stopped"}
        token = self._csrf()
        resp = self.client.post(
            "/api/vm/test-node/100/lxc/profile-apply/jellyfin",
            headers={"X-CSRF-Token": token, "Content-Type": "application/json"},
            data=json.dumps({}),
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertIn("already", data["message"].lower())

    def test_profile_apply_unknown_id(self):
        token = self._csrf()
        resp = self.client.post(
            "/api/vm/test-node/100/lxc/profile-apply/nonexistent",
            headers={"X-CSRF-Token": token, "Content-Type": "application/json"},
            data=json.dumps({}),
        )
        self.assertEqual(resp.status_code, 404)

    def test_profile_apply_requires_stop_when_running(self):
        self.mock.nodes.return_value.lxc.return_value.config.get.return_value = {}
        self.mock.nodes.return_value.lxc.return_value.status.current.get.return_value = {"status": "running"}
        token = self._csrf()
        resp = self.client.post(
            "/api/vm/test-node/100/lxc/profile-apply/jellyfin",
            headers={"X-CSRF-Token": token, "Content-Type": "application/json"},
            data=json.dumps({}),
        )
        # Either 409 (devices require stop) or 200 (only feature changes OK when running)
        data = resp.get_json()
        if resp.status_code == 409:
            self.assertTrue(data.get("stop_required"))
        else:
            self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
