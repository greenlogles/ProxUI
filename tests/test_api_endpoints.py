#!/usr/bin/env python3
"""
Unit tests for API endpoints
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

# Add parent directory to path to import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from app import app as flask_app


class TestAPIEndpoints(unittest.TestCase):

    def setUp(self):
        """Set up test fixtures"""
        self.app = flask_app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        # Reset global state
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()

        # Set up mock cluster and connections
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
        """Clean up after tests"""
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        app.current_cluster_id = None

    def test_api_clusters(self):
        """Test /api/clusters endpoint"""
        response = self.client.get("/api/clusters")
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertIn("clusters", data)
        self.assertIn("current", data)
        self.assertEqual(data["current"], "test-cluster")

    @patch("app.init_proxmox_connections")
    def test_api_switch_cluster_success(self, mock_init_connections):
        """Test successful cluster switching"""
        mock_init_connections.return_value = True

        response = self.client.post("/api/switch-cluster/test-cluster")
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertTrue(data["success"])
        self.assertIn("cluster", data)

    @patch("app.init_proxmox_connections")
    def test_api_switch_cluster_failure(self, mock_init_connections):
        """Test cluster switching failure"""
        mock_init_connections.return_value = False

        response = self.client.post("/api/switch-cluster/test-cluster")
        self.assertEqual(response.status_code, 500)

        data = json.loads(response.data)
        self.assertIn("error", data)

    def test_api_switch_cluster_not_found(self):
        """Test switching to non-existent cluster"""
        response = self.client.post("/api/switch-cluster/nonexistent")
        self.assertEqual(response.status_code, 404)

        data = json.loads(response.data)
        self.assertIn("error", data)

    def test_api_nodes(self):
        """Test /api/nodes endpoint"""
        response = self.client.get("/api/nodes")
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "test-node")
        self.assertEqual(data[0]["status"], "online")

    @patch("app.get_all_vms_and_containers")
    def test_api_resources(self, mock_get_resources):
        """Test /api/resources endpoint"""
        mock_resources = [
            {"vmid": "100", "type": "qemu", "name": "test-vm"},
            {"vmid": "101", "type": "lxc", "name": "test-container"},
        ]
        mock_get_resources.return_value = mock_resources

        response = self.client.get("/api/resources")
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["vmid"], "100")

    @patch("app.get_proxmox_connection")
    def test_api_node_status_success(self, mock_get_connection):
        """Test successful node status retrieval"""
        mock_connection = Mock()
        mock_connection.nodes.return_value.status.get.return_value = {
            "uptime": 123456,
            "loadavg": [0.1, 0.2, 0.3],
            "memory": {"used": 1073741824, "total": 8589934592},
        }
        mock_get_connection.return_value = mock_connection

        response = self.client.get("/api/node/test-node/status")
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertIn("uptime", data)
        self.assertIn("loadavg", data)

    @patch("app.get_proxmox_connection")
    def test_api_node_status_not_found(self, mock_get_connection):
        """Test node status with connection not found"""
        mock_get_connection.return_value = None

        response = self.client.get("/api/node/test-node/status")
        self.assertEqual(response.status_code, 404)

        data = json.loads(response.data)
        self.assertIn("error", data)

    @patch("app.get_proxmox_connection")
    def test_api_node_storages(self, mock_get_connection):
        """Test node storages endpoint"""
        mock_connection = Mock()
        mock_storages = [
            {
                "storage": "local-lvm",
                "enabled": 1,
                "content": "images,rootdir",
                "total": 21474836480,
                "used": 5368709120,
            },
            {
                "storage": "local",
                "enabled": 1,
                "content": "iso,backup",
                "total": 107374182400,
                "used": 10737418240,
            },
        ]
        mock_connection.nodes.return_value.storage.get.return_value = mock_storages
        mock_get_connection.return_value = mock_connection

        # Test for qemu VMs
        response = self.client.get("/api/node/test-node/storages?vm_type=qemu")
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertEqual(len(data), 1)  # Only local-lvm supports images
        self.assertEqual(data[0]["storage"], "local-lvm")
        self.assertIn("available_gb", data[0])

    @patch("app.get_proxmox_connection")
    def test_api_node_templates(self, mock_get_connection):
        """Test node templates endpoint"""
        mock_connection = Mock()

        # Mock VM templates
        mock_vms = [
            {"vmid": "900", "name": "ubuntu-template", "template": 1},
            {"vmid": "100", "name": "regular-vm", "template": 0},
        ]
        mock_connection.nodes.return_value.qemu.get.return_value = mock_vms

        # Mock storage for LXC templates
        mock_storages = [{"storage": "local", "enabled": 1, "content": "vztmpl,iso"}]
        mock_connection.nodes.return_value.storage.get.return_value = mock_storages

        # Mock LXC templates
        mock_lxc_templates = [
            {"volid": "local:vztmpl/ubuntu-22.04-standard_22.04-1_amd64.tar.zst"}
        ]
        mock_connection.nodes.return_value.storage.return_value.content.get.return_value = (
            mock_lxc_templates
        )
        mock_get_connection.return_value = mock_connection

        response = self.client.get("/api/node/test-node/templates")
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertIn("qemu", data)
        self.assertIn("lxc", data)
        self.assertEqual(len(data["qemu"]), 1)  # Only template VMs
        self.assertEqual(data["qemu"][0]["vmid"], "900")

    @patch("app.get_proxmox_connection")
    def test_api_cluster_nextid(self, mock_get_connection):
        """Test cluster next VMID endpoint"""
        mock_connection = Mock()
        mock_connection.cluster.nextid.get.return_value = 102
        mock_get_connection.return_value = mock_connection

        response = self.client.get("/api/cluster/nextid")
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertEqual(data["vmid"], 102)

    @patch("app.get_proxmox_connection")
    def test_api_cluster_nextid_no_connection(self, mock_get_connection):
        """Test cluster next VMID with no connection"""
        mock_get_connection.return_value = None

        response = self.client.get("/api/cluster/nextid")
        self.assertEqual(response.status_code, 500)

        data = json.loads(response.data)
        self.assertIn("error", data)
        self.assertEqual(data["vmid"], 100)  # Default fallback

    @patch("app.get_proxmox_connection")
    def test_api_vm_config_get(self, mock_get_connection):
        """Test getting VM configuration"""
        mock_connection = Mock()
        mock_config = {
            "cores": 2,
            "memory": 2048,
            "name": "test-vm",
            "net0": "virtio,bridge=vmbr0",
        }
        mock_connection.nodes.return_value.qemu.return_value.config.get.return_value = (
            mock_config
        )
        mock_get_connection.return_value = mock_connection

        response = self.client.get("/api/vm/test-node/100/config")
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertEqual(data["cores"], 2)
        self.assertEqual(data["memory"], 2048)

    @patch("app.get_proxmox_connection")
    def test_api_vm_config_put(self, mock_get_connection):
        """Test updating VM configuration"""
        mock_connection = Mock()
        mock_get_connection.return_value = mock_connection

        config_update = {"cores": 4, "memory": 4096, "net0": "virtio,bridge=vmbr1"}

        response = self.client.put(
            "/api/vm/test-node/100/config",
            data=json.dumps(config_update),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertTrue(data["success"])

        # Verify the API was called with correct parameters
        mock_connection.nodes.return_value.qemu.return_value.config.put.assert_called_once()

    @patch("app.get_proxmox_connection")
    def test_api_vm_resize_disk(self, mock_get_connection):
        """Test VM disk resize"""
        mock_connection = Mock()
        mock_connection.nodes.return_value.qemu.return_value.resize.put.return_value = (
            "UPID:test"
        )
        mock_get_connection.return_value = mock_connection

        resize_data = {"disk": "scsi0", "size": "64G"}

        response = self.client.put(
            "/api/vm/test-node/100/resize-disk",
            data=json.dumps(resize_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertTrue(data["success"])
        self.assertIn("message", data)

    @patch("app.get_proxmox_connection")
    def test_api_vm_clone(self, mock_get_connection):
        """Test VM cloning"""
        mock_connection = Mock()
        mock_connection.nodes.return_value.qemu.return_value.clone.post.return_value = (
            "UPID:test"
        )
        mock_get_connection.return_value = mock_connection

        clone_data = {
            "name": "cloned-vm",
            "vmid": "102",
            "target_node": "test-node",
            "clone_type": "full",
        }

        response = self.client.post(
            "/api/vm/test-node/100/clone",
            data=json.dumps(clone_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertTrue(data["success"])
        self.assertEqual(data["new_vmid"], "102")

    @patch("app.get_proxmox_connection")
    def test_api_vm_delete_success(self, mock_get_connection):
        """Test successful VM deletion"""
        mock_connection = Mock()

        # Mock VM status (stopped)
        mock_status = {"status": "stopped"}
        mock_connection.nodes.return_value.qemu.return_value.status.current.get.return_value = (
            mock_status
        )

        # Mock VM config
        mock_config = {"name": "test-vm"}
        mock_connection.nodes.return_value.qemu.return_value.config.get.return_value = (
            mock_config
        )

        # Mock delete response
        mock_connection.nodes.return_value.qemu.return_value.delete.return_value = (
            "UPID:test"
        )
        mock_get_connection.return_value = mock_connection

        response = self.client.post("/api/vm/test-node/100/delete")
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertTrue(data["success"])
        self.assertIn("message", data)

    @patch("app.get_proxmox_connection")
    def test_api_vm_delete_running(self, mock_get_connection):
        """Test VM deletion when VM is running"""
        mock_connection = Mock()

        # Mock VM status (running)
        mock_status = {"status": "running"}
        mock_connection.nodes.return_value.qemu.return_value.status.current.get.return_value = (
            mock_status
        )
        mock_get_connection.return_value = mock_connection

        response = self.client.post("/api/vm/test-node/100/delete")
        self.assertEqual(response.status_code, 400)

        data = json.loads(response.data)
        self.assertIn("error", data)
        self.assertIn("stopped", data["error"])

    @patch("app.get_proxmox_connection")
    def test_api_cluster_tasks(self, mock_get_connection):
        """Test cluster tasks endpoint"""
        mock_connection = Mock()
        mock_tasks = [
            {
                "upid": "UPID:test-node:123:456:qmstart:100:root@pam:",
                "type": "qmstart",
                "status": "OK",
                "starttime": 1673456789,
                "endtime": 1673456800,
                "user": "root@pam",
            }
        ]
        mock_connection.cluster.tasks.get.return_value = mock_tasks
        mock_get_connection.return_value = mock_connection

        response = self.client.get("/api/tasks")
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["type"], "qmstart")
        self.assertIn("start_time_formatted", data[0])

    @patch("app.get_proxmox_connection")
    def test_api_vm_iso_attach_success(self, mock_get_connection):
        """Test successful ISO attachment to QEMU VM"""
        mock_connection = Mock()

        # Mock VM configuration (QEMU)
        mock_config = {"cores": 2, "memory": 2048, "net0": "virtio,bridge=vmbr0"}
        mock_connection.nodes.return_value.qemu.return_value.config.get.return_value = (
            mock_config
        )
        mock_get_connection.return_value = mock_connection

        attach_data = {"iso_image": "local:iso/ubuntu-20.04.iso", "interface": "ide2"}

        response = self.client.post(
            "/api/vm/test-node/100/iso/attach",
            data=json.dumps(attach_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertTrue(data["success"])
        self.assertIn("message", data)
        self.assertEqual(data["interface"], "ide2")

        # Verify the API was called with correct parameters
        mock_connection.nodes.return_value.qemu.return_value.config.put.assert_called_once_with(
            ide2="local:iso/ubuntu-20.04.iso,media=cdrom"
        )

    @patch("app.get_proxmox_connection")
    def test_api_vm_iso_attach_interface_in_use(self, mock_get_connection):
        """Test ISO attachment when interface is already in use"""
        mock_connection = Mock()

        # Mock VM configuration with ide2 already in use
        mock_config = {
            "cores": 2,
            "memory": 2048,
            "ide2": "local:iso/existing.iso,media=cdrom",
        }
        mock_connection.nodes.return_value.qemu.return_value.config.get.return_value = (
            mock_config
        )
        mock_get_connection.return_value = mock_connection

        attach_data = {"iso_image": "local:iso/ubuntu-20.04.iso", "interface": "ide2"}

        response = self.client.post(
            "/api/vm/test-node/100/iso/attach",
            data=json.dumps(attach_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

        data = json.loads(response.data)
        self.assertIn("error", data)
        self.assertIn("already in use", data["error"])

    @patch("app.get_proxmox_connection")
    def test_api_vm_iso_attach_lxc_container(self, mock_get_connection):
        """Test ISO attachment to LXC container (should fail)"""
        mock_connection = Mock()

        # Mock QEMU failure, LXC success (simulating LXC container)
        mock_connection.nodes.return_value.qemu.return_value.config.get.side_effect = (
            Exception("Not a QEMU VM")
        )
        mock_connection.nodes.return_value.lxc.return_value.config.get.return_value = {
            "hostname": "test-container",
            "memory": 512,
        }
        mock_get_connection.return_value = mock_connection

        attach_data = {"iso_image": "local:iso/ubuntu-20.04.iso", "interface": "ide2"}

        response = self.client.post(
            "/api/vm/test-node/101/iso/attach",
            data=json.dumps(attach_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

        data = json.loads(response.data)
        self.assertIn("error", data)
        self.assertIn("LXC containers do not support ISO attachment", data["error"])

    @patch("app.get_proxmox_connection")
    def test_api_vm_iso_attach_missing_parameters(self, mock_get_connection):
        """Test ISO attachment with missing parameters"""
        mock_connection = Mock()
        mock_get_connection.return_value = mock_connection

        # Test missing iso_image
        response = self.client.post(
            "/api/vm/test-node/100/iso/attach",
            data=json.dumps({"interface": "ide2"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

        data = json.loads(response.data)
        self.assertIn("error", data)
        self.assertIn("Missing iso_image parameter", data["error"])

    @patch("app.get_proxmox_connection")
    def test_api_vm_iso_attach_no_connection(self, mock_get_connection):
        """Test ISO attachment when node connection is not found"""
        mock_get_connection.return_value = None

        attach_data = {"iso_image": "local:iso/ubuntu-20.04.iso", "interface": "ide2"}

        response = self.client.post(
            "/api/vm/test-node/100/iso/attach",
            data=json.dumps(attach_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

        data = json.loads(response.data)
        self.assertIn("error", data)
        self.assertEqual(data["error"], "Node not found")

    @patch("app.get_proxmox_connection")
    def test_api_vm_iso_detach_success(self, mock_get_connection):
        """Test successful ISO detachment from QEMU VM"""
        mock_connection = Mock()

        # Mock VM configuration with ISO attached
        mock_config = {
            "cores": 2,
            "memory": 2048,
            "ide2": "local:iso/ubuntu-20.04.iso,media=cdrom",
        }
        mock_connection.nodes.return_value.qemu.return_value.config.get.return_value = (
            mock_config
        )
        mock_get_connection.return_value = mock_connection

        detach_data = {"interface": "ide2"}

        response = self.client.post(
            "/api/vm/test-node/100/iso/detach",
            data=json.dumps(detach_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertTrue(data["success"])
        self.assertIn("message", data)
        self.assertEqual(data["interface"], "ide2")

        # Verify the API was called with correct parameters
        mock_connection.nodes.return_value.qemu.return_value.config.put.assert_called_once_with(
            delete="ide2"
        )

    @patch("app.get_proxmox_connection")
    def test_api_vm_iso_detach_interface_not_found(self, mock_get_connection):
        """Test ISO detachment when interface doesn't exist"""
        mock_connection = Mock()

        # Mock VM configuration without ide2
        mock_config = {"cores": 2, "memory": 2048}
        mock_connection.nodes.return_value.qemu.return_value.config.get.return_value = (
            mock_config
        )
        mock_get_connection.return_value = mock_connection

        detach_data = {"interface": "ide2"}

        response = self.client.post(
            "/api/vm/test-node/100/iso/detach",
            data=json.dumps(detach_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

        data = json.loads(response.data)
        self.assertIn("error", data)
        self.assertIn("not found", data["error"])

    @patch("app.get_proxmox_connection")
    def test_api_vm_iso_detach_lxc_container(self, mock_get_connection):
        """Test ISO detachment from LXC container (should fail)"""
        mock_connection = Mock()

        # Mock QEMU failure, LXC success (simulating LXC container)
        mock_connection.nodes.return_value.qemu.return_value.config.get.side_effect = (
            Exception("Not a QEMU VM")
        )
        mock_connection.nodes.return_value.lxc.return_value.config.get.return_value = {
            "hostname": "test-container",
            "memory": 512,
        }
        mock_get_connection.return_value = mock_connection

        detach_data = {"interface": "ide2"}

        response = self.client.post(
            "/api/vm/test-node/101/iso/detach",
            data=json.dumps(detach_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

        data = json.loads(response.data)
        self.assertIn("error", data)
        self.assertIn("LXC containers do not support ISO attachment", data["error"])

    @patch("app.get_proxmox_connection")
    def test_api_vm_iso_detach_missing_parameters(self, mock_get_connection):
        """Test ISO detachment with missing parameters"""
        mock_connection = Mock()
        mock_get_connection.return_value = mock_connection

        # Test missing interface
        response = self.client.post(
            "/api/vm/test-node/100/iso/detach",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

        data = json.loads(response.data)
        self.assertIn("error", data)
        self.assertIn("Missing interface parameter", data["error"])

    @patch("app.get_proxmox_connection")
    def test_api_vm_iso_attach_default_interface(self, mock_get_connection):
        """Test ISO attachment with default interface (ide2)"""
        mock_connection = Mock()

        # Mock VM configuration
        mock_config = {"cores": 2, "memory": 2048}
        mock_connection.nodes.return_value.qemu.return_value.config.get.return_value = (
            mock_config
        )
        mock_get_connection.return_value = mock_connection

        # Don't specify interface (should default to ide2)
        attach_data = {"iso_image": "local:iso/ubuntu-20.04.iso"}

        response = self.client.post(
            "/api/vm/test-node/100/iso/attach",
            data=json.dumps(attach_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertTrue(data["success"])
        self.assertEqual(data["interface"], "ide2")  # Should default to ide2

    @patch("app.get_proxmox_connection")
    def test_api_vm_iso_attach_proxmox_error(self, mock_get_connection):
        """Test ISO attachment when Proxmox API raises an error"""
        mock_connection = Mock()

        # Mock VM configuration
        mock_config = {"cores": 2, "memory": 2048}
        mock_connection.nodes.return_value.qemu.return_value.config.get.return_value = (
            mock_config
        )

        # Mock Proxmox API error
        mock_connection.nodes.return_value.qemu.return_value.config.put.side_effect = (
            Exception("Proxmox API error")
        )
        mock_get_connection.return_value = mock_connection

        attach_data = {"iso_image": "local:iso/ubuntu-20.04.iso", "interface": "ide2"}

        response = self.client.post(
            "/api/vm/test-node/100/iso/attach",
            data=json.dumps(attach_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 500)

        data = json.loads(response.data)
        self.assertIn("error", data)
        self.assertIn("Proxmox API error", data["error"])

    @patch("app.get_proxmox_connection")
    def test_api_vm_iso_detach_proxmox_error(self, mock_get_connection):
        """Test ISO detachment when Proxmox API raises an error"""
        mock_connection = Mock()

        # Mock VM configuration with ISO attached
        mock_config = {
            "cores": 2,
            "memory": 2048,
            "ide2": "local:iso/ubuntu-20.04.iso,media=cdrom",
        }
        mock_connection.nodes.return_value.qemu.return_value.config.get.return_value = (
            mock_config
        )

        # Mock Proxmox API error
        mock_connection.nodes.return_value.qemu.return_value.config.put.side_effect = (
            Exception("Proxmox API error")
        )
        mock_get_connection.return_value = mock_connection

        detach_data = {"interface": "ide2"}

        response = self.client.post(
            "/api/vm/test-node/100/iso/detach",
            data=json.dumps(detach_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 500)

        data = json.loads(response.data)
        self.assertIn("error", data)
        self.assertIn("Proxmox API error", data["error"])

    @patch("app.get_proxmox_connection")
    def test_api_vm_boot_order_success(self, mock_get_connection):
        """Test successful boot order update for QEMU VM"""
        mock_connection = Mock()

        # Mock VM configuration (QEMU)
        mock_config = {
            "cores": 2,
            "memory": 2048,
            "scsi0": "local-lvm:vm-100-disk-0,size=32G",
        }
        mock_connection.nodes.return_value.qemu.return_value.config.get.return_value = (
            mock_config
        )
        mock_get_connection.return_value = mock_connection

        boot_data = {"boot_devices": ["scsi0", "ide2", "net"]}

        response = self.client.put(
            "/api/vm/test-node/100/boot-order",
            data=json.dumps(boot_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertTrue(data["success"])
        self.assertIn("message", data)
        self.assertEqual(data["boot_order"], "order=scsi0;ide2;net")

        # Verify the API was called with correct parameters
        mock_connection.nodes.return_value.qemu.return_value.config.put.assert_called_once_with(
            boot="order=scsi0;ide2;net"
        )

    @patch("app.get_proxmox_connection")
    def test_api_vm_boot_order_empty_devices(self, mock_get_connection):
        """Test boot order update with empty device list (default boot)"""
        mock_connection = Mock()

        # Mock VM configuration (QEMU)
        mock_config = {"cores": 2, "memory": 2048}
        mock_connection.nodes.return_value.qemu.return_value.config.get.return_value = (
            mock_config
        )
        mock_get_connection.return_value = mock_connection

        boot_data = {"boot_devices": []}

        response = self.client.put(
            "/api/vm/test-node/100/boot-order",
            data=json.dumps(boot_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertTrue(data["success"])
        self.assertEqual(data["boot_order"], "c")  # Default disk boot

        # Verify the API was called with default boot
        mock_connection.nodes.return_value.qemu.return_value.config.put.assert_called_once_with(
            boot="c"
        )

    @patch("app.get_proxmox_connection")
    def test_api_vm_boot_order_lxc_container(self, mock_get_connection):
        """Test boot order update for LXC container (should fail)"""
        mock_connection = Mock()

        # Mock QEMU failure, LXC success (simulating LXC container)
        mock_connection.nodes.return_value.qemu.return_value.config.get.side_effect = (
            Exception("Not a QEMU VM")
        )
        mock_connection.nodes.return_value.lxc.return_value.config.get.return_value = {
            "hostname": "test-container",
            "memory": 512,
        }
        mock_get_connection.return_value = mock_connection

        boot_data = {"boot_devices": ["scsi0"]}

        response = self.client.put(
            "/api/vm/test-node/101/boot-order",
            data=json.dumps(boot_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

        data = json.loads(response.data)
        self.assertIn("error", data)
        self.assertIn(
            "LXC containers do not support boot order configuration", data["error"]
        )

    @patch("app.get_proxmox_connection")
    def test_api_vm_boot_order_missing_parameters(self, mock_get_connection):
        """Test boot order update with missing parameters"""
        mock_connection = Mock()
        mock_get_connection.return_value = mock_connection

        # Test missing boot_devices
        response = self.client.put(
            "/api/vm/test-node/100/boot-order",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

        data = json.loads(response.data)
        self.assertIn("error", data)
        self.assertIn("Missing boot_devices parameter", data["error"])

    @patch("app.get_proxmox_connection")
    def test_api_vm_boot_order_invalid_parameter_type(self, mock_get_connection):
        """Test boot order update with invalid parameter type"""
        mock_connection = Mock()
        mock_get_connection.return_value = mock_connection

        # Test invalid boot_devices type (should be array)
        boot_data = {"boot_devices": "scsi0,ide2"}  # String instead of array

        response = self.client.put(
            "/api/vm/test-node/100/boot-order",
            data=json.dumps(boot_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

        data = json.loads(response.data)
        self.assertIn("error", data)
        self.assertIn("boot_devices must be an array", data["error"])

    @patch("app.get_proxmox_connection")
    def test_api_vm_boot_order_no_connection(self, mock_get_connection):
        """Test boot order update when node connection is not found"""
        mock_get_connection.return_value = None

        boot_data = {"boot_devices": ["scsi0"]}

        response = self.client.put(
            "/api/vm/test-node/100/boot-order",
            data=json.dumps(boot_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)

        data = json.loads(response.data)
        self.assertIn("error", data)
        self.assertEqual(data["error"], "Node not found")

    @patch("app.get_proxmox_connection")
    def test_api_vm_boot_order_proxmox_error(self, mock_get_connection):
        """Test boot order update when Proxmox API raises an error"""
        mock_connection = Mock()

        # Mock VM configuration
        mock_config = {"cores": 2, "memory": 2048}
        mock_connection.nodes.return_value.qemu.return_value.config.get.return_value = (
            mock_config
        )

        # Mock Proxmox API error
        mock_connection.nodes.return_value.qemu.return_value.config.put.side_effect = (
            Exception("Proxmox API error")
        )
        mock_get_connection.return_value = mock_connection

        boot_data = {"boot_devices": ["scsi0", "net"]}

        response = self.client.put(
            "/api/vm/test-node/100/boot-order",
            data=json.dumps(boot_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 500)

        data = json.loads(response.data)
        self.assertIn("error", data)
        self.assertIn("Proxmox API error", data["error"])

    @patch("app.get_proxmox_connection")
    def test_api_vm_boot_order_single_device(self, mock_get_connection):
        """Test boot order update with single device"""
        mock_connection = Mock()

        # Mock VM configuration (QEMU)
        mock_config = {"cores": 2, "memory": 2048}
        mock_connection.nodes.return_value.qemu.return_value.config.get.return_value = (
            mock_config
        )
        mock_get_connection.return_value = mock_connection

        boot_data = {"boot_devices": ["net"]}

        response = self.client.put(
            "/api/vm/test-node/100/boot-order",
            data=json.dumps(boot_data),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertTrue(data["success"])
        self.assertEqual(data["boot_order"], "order=net")


if __name__ == "__main__":
    unittest.main()
