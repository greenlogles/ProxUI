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


if __name__ == "__main__":
    unittest.main()
