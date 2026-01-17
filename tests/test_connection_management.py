#!/usr/bin/env python3
"""
Unit tests for connection management functions
"""

import os
import sys
import unittest
from datetime import datetime
from unittest.mock import MagicMock, Mock, patch

# Add parent directory to path to import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (
    PROXMOX_TIMEOUT,
    all_clusters,
    cluster_nodes,
    connection_metadata,
    current_cluster_id,
    get_proxmox_connection,
    init_all_clusters,
    init_proxmox_connections,
    is_authentication_error,
    proxmox_nodes,
    renew_proxmox_connection,
)


class TestConnectionManagement(unittest.TestCase):

    def setUp(self):
        """Set up test fixtures"""
        global proxmox_nodes, connection_metadata, all_clusters, cluster_nodes, current_cluster_id

        # Reset global state
        proxmox_nodes.clear()
        connection_metadata.clear()
        all_clusters.clear()
        cluster_nodes.clear()
        current_cluster_id = None

        # Mock cluster config
        self.test_cluster_config = {
            "test-cluster": {
                "id": "test-cluster",
                "name": "Test Cluster",
                "nodes": [
                    {
                        "host": "192.168.1.100",
                        "user": "root@pam",
                        "password": "testpass",
                        "verify_ssl": False,
                    }
                ],
            }
        }
        all_clusters.update(self.test_cluster_config)

    def tearDown(self):
        """Clean up after tests"""
        global proxmox_nodes, connection_metadata, all_clusters, cluster_nodes, current_cluster_id
        proxmox_nodes.clear()
        connection_metadata.clear()
        all_clusters.clear()
        cluster_nodes.clear()
        current_cluster_id = None

    def test_is_authentication_error(self):
        """Test authentication error detection"""
        # Test various authentication error messages
        auth_errors = [
            "Couldn't authenticate user: root@pam",
            "authentication failed",
            "401 Unauthorized",
            "Permission denied",
            "Invalid ticket",
            "Authentication required",
        ]

        for error_msg in auth_errors:
            self.assertTrue(is_authentication_error(Exception(error_msg)))

        # Test non-authentication errors
        non_auth_errors = [
            "Connection timeout",
            "Network unreachable",
            "500 Internal Server Error",
            "VM not found",
        ]

        for error_msg in non_auth_errors:
            self.assertFalse(is_authentication_error(Exception(error_msg)))

    @patch("app.ProxmoxAPI")
    def test_renew_proxmox_connection_success(self, mock_proxmox_api):
        """Test successful connection renewal"""
        import app

        # Set up test data using module globals
        node_name = "test-node"
        app.connection_metadata[node_name] = {
            "host": "192.168.1.100",
            "user": "root@pam",
            "password": "testpass",
            "verify_ssl": False,
            "last_authenticated": datetime.now(),
        }

        # Mock ProxmoxAPI
        mock_connection = Mock()
        mock_connection.version.get.return_value = {"version": "7.4-15"}
        mock_proxmox_api.return_value = mock_connection

        # Test renewal
        result = renew_proxmox_connection(node_name)

        # Assertions
        self.assertIsNotNone(result)
        self.assertEqual(result, mock_connection)
        self.assertEqual(app.proxmox_nodes[node_name], mock_connection)
        mock_proxmox_api.assert_called_once_with(
            "192.168.1.100",
            user="root@pam",
            password="testpass",
            verify_ssl=False,
            timeout=PROXMOX_TIMEOUT,
        )

    def test_renew_proxmox_connection_no_metadata(self):
        """Test connection renewal with missing metadata"""
        result = renew_proxmox_connection("nonexistent-node")
        self.assertIsNone(result)

    @patch("app.ProxmoxAPI")
    def test_renew_proxmox_connection_failure(self, mock_proxmox_api):
        """Test connection renewal failure"""
        import app

        node_name = "test-node"
        app.connection_metadata[node_name] = {
            "host": "192.168.1.100",
            "user": "root@pam",
            "password": "testpass",
            "verify_ssl": False,
            "last_authenticated": datetime.now(),
        }

        # Mock ProxmoxAPI to raise exception
        mock_proxmox_api.side_effect = Exception("Connection failed")

        result = renew_proxmox_connection(node_name)
        self.assertIsNone(result)

    @patch("app.get_proxmox_for_node")
    @patch("app.renew_proxmox_connection")
    def test_get_proxmox_connection_valid(self, mock_renew, mock_get_node):
        """Test getting valid connection"""
        mock_connection = Mock()
        mock_connection.version.get.return_value = {"version": "7.4-15"}
        mock_get_node.return_value = mock_connection

        result = get_proxmox_connection("test-node")

        self.assertEqual(result, mock_connection)
        mock_connection.version.get.assert_called_once()
        mock_renew.assert_not_called()

    @patch("app.get_proxmox_for_node")
    @patch("app.renew_proxmox_connection")
    @patch("app.is_authentication_error")
    def test_get_proxmox_connection_auth_error(
        self, mock_is_auth_error, mock_renew, mock_get_node
    ):
        """Test connection renewal on authentication error"""
        # Set up mocks
        mock_connection = Mock()
        mock_connection.version.get.side_effect = Exception("401 Unauthorized")
        mock_get_node.return_value = mock_connection
        mock_is_auth_error.return_value = True

        mock_renewed_connection = Mock()
        mock_renew.return_value = mock_renewed_connection

        result = get_proxmox_connection("test-node")

        self.assertEqual(result, mock_renewed_connection)
        mock_renew.assert_called_once_with("test-node")

    @patch("app.get_proxmox_for_node")
    def test_get_proxmox_connection_no_auto_renew(self, mock_get_node):
        """Test getting connection without auto-renewal"""
        mock_connection = Mock()
        mock_get_node.return_value = mock_connection

        result = get_proxmox_connection("test-node", auto_renew=False)

        self.assertEqual(result, mock_connection)
        mock_connection.version.get.assert_not_called()

    def test_init_all_clusters(self):
        """Test cluster initialization"""
        # Mock the config directly
        import app

        original_config = app.config
        original_clusters = app.all_clusters.copy()
        original_cluster_id = app.current_cluster_id

        app.config = {"clusters": [{"id": "test", "name": "Test", "nodes": []}]}

        try:
            init_all_clusters()
            # Check the module globals directly
            self.assertIn("test", app.all_clusters)
            self.assertEqual(app.current_cluster_id, "test")
        finally:
            # Restore original config and state
            app.config = original_config
            app.all_clusters = original_clusters
            app.current_cluster_id = original_cluster_id

    @patch("app.ProxmoxAPI")
    @patch("app.datetime")
    def test_init_proxmox_connections_success(self, mock_datetime, mock_proxmox_api):
        """Test successful Proxmox connections initialization"""
        # Set up current cluster using module globals
        import app

        app.current_cluster_id = "test-cluster"
        app.all_clusters["test-cluster"] = self.test_cluster_config["test-cluster"]

        # Mock datetime
        mock_now = datetime(2023, 1, 1, 12, 0, 0)
        mock_datetime.now.return_value = mock_now

        # Mock ProxmoxAPI
        mock_connection = Mock()
        mock_connection.version.get.return_value = {"version": "7.4-15"}
        mock_connection.nodes.get.return_value = [
            {"node": "node1", "status": "online"},
            {"node": "node2", "status": "online"},
        ]
        mock_proxmox_api.return_value = mock_connection

        result = init_proxmox_connections()

        self.assertTrue(result)
        self.assertGreater(len(app.cluster_nodes), 0)
        self.assertGreater(len(app.proxmox_nodes), 0)
        self.assertIn("192.168.1.100", app.connection_metadata)

    def test_init_proxmox_connections_no_cluster(self):
        """Test initialization with no valid cluster"""
        global current_cluster_id
        current_cluster_id = None

        result = init_proxmox_connections()
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
