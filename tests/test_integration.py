#!/usr/bin/env python3
"""
Integration tests for ProxUI2
"""

import json
import os
import sys
import unittest
from unittest.mock import Mock, patch

# Add parent directory to path to import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from app import app as flask_app


class TestIntegration(unittest.TestCase):
    """Integration tests that test multiple components together"""

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
        app.current_cluster_id = None

    def tearDown(self):
        """Clean up after tests"""
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        app.current_cluster_id = None

    @patch("app.get_proxmox_connection")
    def test_complete_vm_lifecycle(self, mock_get_connection):
        """Test complete VM lifecycle: list -> detail -> action -> delete"""
        # Set up mock connection
        mock_connection = Mock()
        mock_get_connection.return_value = mock_connection

        # Set up cluster
        app.all_clusters["test-cluster"] = {
            "id": "test-cluster",
            "name": "Test Cluster",
        }
        app.current_cluster_id = "test-cluster"
        app.proxmox_nodes["test-node"] = mock_connection

        # Mock VM data
        mock_vm_data = {
            "vmid": "100",
            "node": "test-node",
            "type": "qemu",
            "name": "test-vm",
            "status": "running",
        }

        # 1. Test VM listing
        mock_connection.cluster.resources.get.return_value = [mock_vm_data]
        response = self.client.get("/api/resources")
        self.assertEqual(response.status_code, 200)
        vms = json.loads(response.data)
        self.assertEqual(len(vms), 1)
        self.assertEqual(vms[0]["vmid"], "100")

        # 2. Test VM config API instead of detail page
        mock_connection.nodes.return_value.qemu.return_value.config.get.return_value = {
            "name": "test-vm",
            "cores": 2,
            "memory": 2048,
        }

        response = self.client.get("/api/vm/test-node/100/config")
        self.assertEqual(response.status_code, 200)
        config_data = json.loads(response.data)
        self.assertEqual(config_data["name"], "test-vm")

        # 3. Test VM action (stop)
        response = self.client.post("/vm/test-node/100/stop")
        self.assertEqual(response.status_code, 302)  # Redirect after action

        # 4. Test VM deletion
        mock_connection.nodes.return_value.qemu.return_value.status.current.get.return_value = {
            "status": "stopped"
        }
        mock_connection.nodes.return_value.qemu.return_value.config.get.return_value = {
            "name": "test-vm"
        }
        # Mock the delete operation to return a task ID string
        mock_connection.nodes.return_value.qemu.return_value.delete.return_value = (
            "UPID:test-node:123456:456789:qmdestroy:100:root@pam:"
        )

        response = self.client.post("/api/vm/test-node/100/delete")
        if response.status_code != 200:
            print(f"Delete response status: {response.status_code}")
            print(f"Delete response data: {response.data}")
        self.assertEqual(response.status_code, 200)
        result = json.loads(response.data)
        self.assertTrue(result["success"])

    @patch("app.get_proxmox_connection")
    def test_authentication_error_recovery(self, mock_get_connection):
        """Test that authentication errors are properly handled and recovered"""
        # Set up cluster
        app.all_clusters["test-cluster"] = {
            "id": "test-cluster",
            "name": "Test Cluster",
        }
        app.current_cluster_id = "test-cluster"

        # Set up connection metadata for renewal
        app.connection_metadata["test-node"] = {
            "host": "192.168.1.100",
            "user": "root@pam",
            "password": "testpass",
            "verify_ssl": False,
        }

        # First connection fails with auth error
        failing_connection = Mock()
        failing_connection.version.get.side_effect = Exception("401 Unauthorized")

        # Second connection succeeds after renewal
        working_connection = Mock()
        working_connection.version.get.return_value = {"version": "7.4-15"}
        working_connection.cluster.resources.get.return_value = []

        # Mock the get_proxmox_connection to return first failing, then working
        call_count = 0

        def mock_connection_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return failing_connection
            return working_connection

        with patch("app.is_authentication_error", return_value=True), patch(
            "app.renew_proxmox_connection", return_value=working_connection
        ):

            mock_get_connection.side_effect = mock_connection_side_effect

            # This should trigger auth error and recovery
            response = self.client.get("/api/resources")
            self.assertEqual(response.status_code, 200)

    @patch("app.get_proxmox_connection")
    def test_storage_management_workflow(self, mock_get_connection):
        """Test storage listing and management workflow"""
        # Set up mock connection
        mock_connection = Mock()
        mock_get_connection.return_value = mock_connection

        # Set up cluster
        app.all_clusters["test-cluster"] = {
            "id": "test-cluster",
            "name": "Test Cluster",
        }
        app.current_cluster_id = "test-cluster"
        app.proxmox_nodes["test-node"] = mock_connection

        # Mock storage data
        mock_storages = [
            {
                "storage": "local-lvm",
                "node": "test-node",
                "enabled": 1,
                "content": "images,rootdir",
                "total": 21474836480,
                "used": 5368709120,
            }
        ]

        # Test cluster storage listing
        mock_connection.cluster.resources.get.return_value = mock_storages
        response = self.client.get("/storages")
        self.assertEqual(response.status_code, 200)

        # Test node-specific storage API
        mock_connection.nodes.return_value.storage.get.return_value = mock_storages
        response = self.client.get("/api/node/test-node/storages")
        self.assertEqual(response.status_code, 200)

        storages = json.loads(response.data)
        self.assertEqual(len(storages), 1)
        self.assertEqual(storages[0]["storage"], "local-lvm")

    @patch("app.get_proxmox_connection")
    def test_cluster_switching_workflow(self, mock_get_connection):
        """Test cluster switching workflow"""
        # Set up multiple clusters
        app.all_clusters.update(
            {
                "cluster1": {
                    "id": "cluster1",
                    "name": "Cluster 1",
                    "nodes": [{"host": "192.168.1.100"}],
                },
                "cluster2": {
                    "id": "cluster2",
                    "name": "Cluster 2",
                    "nodes": [{"host": "192.168.1.200"}],
                },
            }
        )
        app.current_cluster_id = "cluster1"

        # Test cluster listing
        response = self.client.get("/api/clusters")
        self.assertEqual(response.status_code, 200)

        data = json.loads(response.data)
        self.assertEqual(len(data["clusters"]), 2)
        self.assertEqual(data["current"], "cluster1")

        # Test cluster switching
        with patch("app.init_proxmox_connections", return_value=True):
            response = self.client.post("/api/switch-cluster/cluster2")
            self.assertEqual(response.status_code, 200)

            result = json.loads(response.data)
            self.assertTrue(result["success"])

    @patch("app.get_proxmox_connection")
    def test_vm_creation_workflow(self, mock_get_connection):
        """Test VM creation workflow"""
        # Set up mock connection
        mock_connection = Mock()
        mock_get_connection.return_value = mock_connection

        # Set up cluster
        app.all_clusters["test-cluster"] = {
            "id": "test-cluster",
            "name": "Test Cluster",
        }
        app.current_cluster_id = "test-cluster"
        app.cluster_nodes.append({"name": "test-node", "status": "online"})

        # Test VM creation form
        response = self.client.get("/create_vm")
        self.assertEqual(response.status_code, 200)

        # Test VM creation POST
        vm_data = {
            "node": "test-node",
            "type": "qemu",
            "creation_method": "blank",
            "name": "new-vm",
            "vmid": "102",
            "memory": "2048",
            "cores": "2",
            "sockets": "1",
            "storage": "local-lvm",
            "disk_size": "32",
            "bridge": "vmbr0",
        }

        response = self.client.post("/create_vm", data=vm_data)
        # Should redirect on success
        self.assertEqual(response.status_code, 302)

    def test_error_handling_workflow(self):
        """Test error handling across the application"""
        # Ensure we have at least one cluster set up for the test
        app.all_clusters["test-cluster"] = {
            "id": "test-cluster",
            "name": "Test Cluster",
        }

        # Test cluster API endpoints (should always work)
        response = self.client.get("/api/clusters")
        self.assertEqual(response.status_code, 200)

        # Test VM API with no connection
        response = self.client.get("/api/vm/nonexistent-node/100/config")
        self.assertEqual(
            response.status_code, 404
        )  # Should return 404 for no connection

        # Test nodes API with no connections
        response = self.client.get("/api/nodes")
        self.assertEqual(response.status_code, 200)  # Should return empty list


if __name__ == "__main__":
    unittest.main()
