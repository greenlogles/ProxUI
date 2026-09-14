#!/usr/bin/env python3
"""
Unit tests for guest hardware add/remove (disks and network interfaces).
"""

import json
import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from app import app as flask_app


class TestHardwareHelpers(unittest.TestCase):
    """Pure-function tests for slot selection and value-string builders."""

    def test_next_key_index_skips_occupied(self):
        config = {"scsi0": "local-lvm:vm-100-disk-0", "scsi1": "local-lvm:vm-100-disk-1"}
        self.assertEqual(app._next_key_index(config, "scsi"), 2)

    def test_next_key_index_fills_gap(self):
        config = {"scsi0": "x", "scsi2": "y"}
        self.assertEqual(app._next_key_index(config, "scsi"), 1)

    def test_next_key_index_empty_config(self):
        self.assertEqual(app._next_key_index({}, "net"), 0)

    def test_next_key_index_ignores_other_prefixes(self):
        config = {"net0": "a", "scsi0": "b", "mp0": "c"}
        self.assertEqual(app._next_key_index(config, "net"), 1)

    def test_build_qemu_disk_value_minimal(self):
        value = app._build_qemu_disk_value("local-lvm", 32)
        self.assertEqual(value, "local-lvm:32")

    def test_build_qemu_disk_value_with_options(self):
        value = app._build_qemu_disk_value(
            "local-lvm", 32, cache="writeback", discard=True, ssd=True,
            iothread=True, backup=False,
        )
        self.assertEqual(
            value,
            "local-lvm:32,cache=writeback,discard=on,ssd=1,iothread=1,backup=0",
        )

    def test_build_qemu_disk_value_backup_true_is_omitted(self):
        # backup defaults to on in PVE, so we don't need to spell it out
        value = app._build_qemu_disk_value("local-lvm", 10, backup=True)
        self.assertEqual(value, "local-lvm:10")

    def test_build_lxc_mp_value(self):
        value = app._build_lxc_mp_value("local-lvm", 8, "/mnt/data")
        self.assertEqual(value, "local-lvm:8,mp=/mnt/data")

    def test_build_lxc_mp_value_no_backup(self):
        value = app._build_lxc_mp_value("local-lvm", 8, "/mnt/data", backup=False)
        self.assertEqual(value, "local-lvm:8,mp=/mnt/data,backup=0")

    def test_build_qemu_net_value_no_mac(self):
        value = app._build_qemu_net_value("virtio", "vmbr0")
        self.assertEqual(value, "virtio,bridge=vmbr0")

    def test_build_qemu_net_value_full(self):
        value = app._build_qemu_net_value(
            "e1000", "vmbr1", vlan=10, firewall=True, mac="AA:BB:CC:DD:EE:FF", rate=5.5
        )
        self.assertEqual(
            value,
            "e1000=AA:BB:CC:DD:EE:FF,bridge=vmbr1,tag=10,firewall=1,rate=5.5",
        )

    def test_build_lxc_net_value_minimal(self):
        value = app._build_lxc_net_value("eth0", "vmbr0")
        self.assertEqual(value, "name=eth0,bridge=vmbr0")

    def test_build_lxc_net_value_full(self):
        value = app._build_lxc_net_value(
            "eth1", "vmbr1", vlan=20, firewall=True, mac="00:11:22:33:44:55", rate=2
        )
        self.assertEqual(
            value,
            "name=eth1,bridge=vmbr1,hwaddr=00:11:22:33:44:55,tag=20,firewall=1,rate=2",
        )


class TestHardwareEndpoints(unittest.TestCase):
    """Endpoint-level tests against a mocked proxmoxer connection."""

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
            "nodes": [{"host": "192.168.1.100", "user": "root@pam", "password": "x"}],
        }
        app.current_cluster_id = "test-cluster"

        self.mock_connection = Mock()
        app.proxmox_nodes["test-node"] = self.mock_connection
        app.cluster_nodes.append(
            {"name": "test-node", "status": "online", "connection": self.mock_connection}
        )

    def tearDown(self):
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        app.current_cluster_id = None

    def _mock_qemu(self, config):
        """Route qemu(vmid) calls to a config-returning mock, lxc(vmid) to a failure."""
        qemu_mock = Mock()
        qemu_mock.status.current.get.return_value = {"status": "stopped"}
        qemu_mock.config.get.return_value = dict(config)
        self.mock_connection.nodes.return_value.qemu.return_value = qemu_mock
        return qemu_mock

    def _mock_lxc(self, config):
        """Make the qemu status probe raise so the app falls back to vm_type=lxc."""
        self.mock_connection.nodes.return_value.qemu.return_value.status.current.get.side_effect = Exception(
            "no such vm"
        )
        lxc_mock = Mock()
        lxc_mock.config.get.return_value = dict(config)
        self.mock_connection.nodes.return_value.lxc.return_value = lxc_mock
        return lxc_mock

    # --- Add disk: QEMU ---

    def test_add_disk_qemu_picks_next_free_scsi_slot(self):
        qemu_mock = self._mock_qemu({"scsi0": "local-lvm:vm-100-disk-0"})

        resp = self.client.post(
            "/api/vm/test-node/100/disks",
            json={"storage": "local-lvm", "size_gb": 20, "bus": "scsi"},
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data["success"])
        self.assertEqual(data["key"], "scsi1")
        qemu_mock.config.put.assert_called_once_with(scsi1="local-lvm:20")

    def test_add_disk_qemu_invalid_bus(self):
        self._mock_qemu({})
        resp = self.client.post(
            "/api/vm/test-node/100/disks",
            json={"storage": "local-lvm", "size_gb": 20, "bus": "nvme"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_add_disk_qemu_missing_storage(self):
        self._mock_qemu({})
        resp = self.client.post(
            "/api/vm/test-node/100/disks", json={"size_gb": 20, "bus": "scsi"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_add_disk_qemu_with_options(self):
        qemu_mock = self._mock_qemu({})
        resp = self.client.post(
            "/api/vm/test-node/100/disks",
            json={
                "storage": "local-lvm",
                "size_gb": 50,
                "bus": "virtio",
                "cache": "writeback",
                "discard": True,
                "ssd": True,
                "iothread": True,
                "backup": False,
            },
        )
        self.assertEqual(resp.status_code, 200)
        qemu_mock.config.put.assert_called_once_with(
            virtio0="local-lvm:50,cache=writeback,discard=on,ssd=1,iothread=1,backup=0"
        )

    # --- Add disk: LXC ---

    def test_add_disk_lxc_uses_mp_and_requires_path(self):
        self._mock_lxc({"rootfs": "local-lvm:8", "mp0": "local-lvm:4,mp=/data0"})
        resp = self.client.post(
            "/api/vm/test-node/101/disks",
            json={"storage": "local-lvm", "size_gb": 16},
        )
        self.assertEqual(resp.status_code, 400)

    def test_add_disk_lxc_next_mp_slot(self):
        lxc_mock = self._mock_lxc({"rootfs": "local-lvm:8", "mp0": "local-lvm:4,mp=/data0"})
        resp = self.client.post(
            "/api/vm/test-node/101/disks",
            json={"storage": "local-lvm", "size_gb": 16, "path": "/mnt/extra"},
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["key"], "mp1")
        lxc_mock.config.put.assert_called_once_with(mp1="local-lvm:16,mp=/mnt/extra")

    # --- Remove disk ---

    def test_remove_disk_rejects_rootfs(self):
        resp = self.client.delete("/api/vm/test-node/101/disks/rootfs")
        self.assertEqual(resp.status_code, 400)

    def test_remove_disk_rejects_unknown_key_shape(self):
        resp = self.client.delete("/api/vm/test-node/100/disks/memory")
        self.assertEqual(resp.status_code, 400)

    def test_remove_disk_not_found(self):
        self._mock_qemu({"scsi0": "local-lvm:vm-100-disk-0"})
        resp = self.client.delete("/api/vm/test-node/100/disks/scsi5")
        self.assertEqual(resp.status_code, 404)

    def test_remove_disk_success_uses_plain_delete(self):
        qemu_mock = self._mock_qemu({"scsi1": "local-lvm:vm-100-disk-1"})
        resp = self.client.delete("/api/vm/test-node/100/disks/scsi1")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data["success"])
        self.assertIn("unused", data["message"])
        # No force=1 — PVE default keeps the volume as unusedN
        qemu_mock.config.put.assert_called_once_with(delete="scsi1")

    # --- Add NIC ---

    def test_add_netif_qemu_default_model(self):
        qemu_mock = self._mock_qemu({"net0": "virtio,bridge=vmbr0"})
        resp = self.client.post(
            "/api/vm/test-node/100/netifs", json={"bridge": "vmbr1"}
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["key"], "net1")
        qemu_mock.config.put.assert_called_once_with(net1="virtio,bridge=vmbr1")

    def test_add_netif_qemu_full_options(self):
        qemu_mock = self._mock_qemu({})
        resp = self.client.post(
            "/api/vm/test-node/100/netifs",
            json={
                "bridge": "vmbr0",
                "model": "e1000",
                "vlan": 15,
                "firewall": True,
                "mac": "AA:BB:CC:DD:EE:FF",
                "rate": 10,
            },
        )
        self.assertEqual(resp.status_code, 200)
        qemu_mock.config.put.assert_called_once_with(
            net0="e1000=AA:BB:CC:DD:EE:FF,bridge=vmbr0,tag=15,firewall=1,rate=10.0"
        )

    def test_add_netif_qemu_invalid_model(self):
        self._mock_qemu({})
        resp = self.client.post(
            "/api/vm/test-node/100/netifs", json={"bridge": "vmbr0", "model": "bogus"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_add_netif_qemu_invalid_vlan(self):
        self._mock_qemu({})
        resp = self.client.post(
            "/api/vm/test-node/100/netifs", json={"bridge": "vmbr0", "vlan": 5000}
        )
        self.assertEqual(resp.status_code, 400)

    def test_add_netif_missing_bridge(self):
        self._mock_qemu({})
        resp = self.client.post("/api/vm/test-node/100/netifs", json={})
        self.assertEqual(resp.status_code, 400)

    # --- Add NIC: LXC uses name= not model= ---

    def test_add_netif_lxc_uses_name_not_model(self):
        lxc_mock = self._mock_lxc({})
        resp = self.client.post(
            "/api/vm/test-node/101/netifs", json={"bridge": "vmbr0"}
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["key"], "net0")
        lxc_mock.config.put.assert_called_once_with(net0="name=eth0,bridge=vmbr0")

    def test_add_netif_lxc_custom_name(self):
        lxc_mock = self._mock_lxc({"net0": "name=eth0,bridge=vmbr0"})
        resp = self.client.post(
            "/api/vm/test-node/101/netifs",
            json={"bridge": "vmbr1", "name": "wan0", "firewall": True},
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["key"], "net1")
        lxc_mock.config.put.assert_called_once_with(
            net1="name=wan0,bridge=vmbr1,firewall=1"
        )

    # --- Remove NIC ---

    def test_remove_netif_invalid_key(self):
        resp = self.client.delete("/api/vm/test-node/100/netifs/bridge0")
        self.assertEqual(resp.status_code, 400)

    def test_remove_netif_not_found(self):
        self._mock_qemu({"net0": "virtio,bridge=vmbr0"})
        resp = self.client.delete("/api/vm/test-node/100/netifs/net3")
        self.assertEqual(resp.status_code, 404)

    def test_remove_netif_success(self):
        qemu_mock = self._mock_qemu({"net0": "virtio,bridge=vmbr0"})
        resp = self.client.delete("/api/vm/test-node/100/netifs/net0")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data["success"])
        qemu_mock.config.put.assert_called_once_with(delete="net0")

    def test_remove_netif_lxc(self):
        lxc_mock = self._mock_lxc({"net0": "name=eth0,bridge=vmbr0"})
        resp = self.client.delete("/api/vm/test-node/101/netifs/net0")
        self.assertEqual(resp.status_code, 200)
        lxc_mock.config.put.assert_called_once_with(delete="net0")


if __name__ == "__main__":
    unittest.main()
