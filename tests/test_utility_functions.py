#!/usr/bin/env python3
"""
Unit tests for utility functions
"""

import os
import sys
import unittest
from unittest.mock import Mock, patch

# Add parent directory to path to import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (
    cluster_nodes,
    get_proxmox_for_node,
    group_shared_storages,
    humanize_uptime,
    proxmox_nodes,
)


class TestUtilityFunctions(unittest.TestCase):

    def setUp(self):
        """Set up test fixtures"""
        global proxmox_nodes, cluster_nodes

        # Reset global state
        proxmox_nodes.clear()
        cluster_nodes.clear()

    def tearDown(self):
        """Clean up after tests"""
        global proxmox_nodes, cluster_nodes
        proxmox_nodes.clear()
        cluster_nodes.clear()

    def test_humanize_uptime_seconds(self):
        """Test uptime formatting for seconds"""
        self.assertEqual(humanize_uptime(30), "30s")
        self.assertEqual(humanize_uptime(59), "59s")

    def test_humanize_uptime_minutes(self):
        """Test uptime formatting for minutes"""
        self.assertEqual(humanize_uptime(60), "1m 0s")
        self.assertEqual(humanize_uptime(90), "1m 30s")
        self.assertEqual(humanize_uptime(3599), "59m 59s")

    def test_humanize_uptime_hours(self):
        """Test uptime formatting for hours"""
        self.assertEqual(humanize_uptime(3600), "1h 0m")
        self.assertEqual(humanize_uptime(3661), "1h 1m")
        self.assertEqual(humanize_uptime(7200), "2h 0m")
        self.assertEqual(humanize_uptime(86399), "23h 59m")

    def test_humanize_uptime_days(self):
        """Test uptime formatting for days"""
        self.assertEqual(humanize_uptime(86400), "1d 0h 0m")
        self.assertEqual(humanize_uptime(90061), "1d 1h 1m")
        self.assertEqual(humanize_uptime(172800), "2d 0h 0m")

    def test_humanize_uptime_edge_cases(self):
        """Test uptime formatting edge cases"""
        self.assertEqual(humanize_uptime(0), "N/A")
        self.assertEqual(humanize_uptime(None), "N/A")
        self.assertEqual(humanize_uptime(""), "N/A")
        self.assertEqual(humanize_uptime("invalid"), "N/A")

        # Test string numbers
        self.assertEqual(humanize_uptime("3600"), "1h 0m")
        self.assertEqual(humanize_uptime("3600.5"), "1h 0m")

    def test_get_proxmox_for_node_direct_connection(self):
        """Test getting direct connection for node"""
        import app

        mock_connection = Mock()
        app.proxmox_nodes["test-node"] = mock_connection

        result = get_proxmox_for_node("test-node")
        self.assertEqual(result, mock_connection)

    def test_get_proxmox_for_node_cluster_connection(self):
        """Test getting connection through cluster nodes"""
        import app

        mock_connection = Mock()
        app.cluster_nodes.append(
            {"name": "test-node", "status": "online", "connection": mock_connection}
        )

        result = get_proxmox_for_node("test-node")
        self.assertEqual(result, mock_connection)

    def test_get_proxmox_for_node_fallback(self):
        """Test fallback to any available connection"""
        import app

        mock_connection = Mock()
        app.proxmox_nodes["other-node"] = mock_connection

        result = get_proxmox_for_node("nonexistent-node")
        self.assertEqual(result, mock_connection)

    def test_get_proxmox_for_node_none(self):
        """Test when no connections are available"""
        result = get_proxmox_for_node("test-node")
        self.assertIsNone(result)

    def test_group_shared_storages_shared_storage(self):
        """Test grouping shared storage"""
        all_storages = [
            {
                "storage": "shared-storage",
                "node": "node1",
                "shared": 1,
                "enabled": 1,
                "status": "available",
                "disk": 10737418240,  # 10GB
                "maxdisk": 107374182400,  # 100GB
            },
            {
                "storage": "shared-storage",
                "node": "node2",
                "shared": 1,
                "enabled": 1,
                "status": "available",
                "disk": 10737418240,  # 10GB (same storage, different view)
                "maxdisk": 107374182400,  # 100GB
            },
        ]

        result = group_shared_storages(all_storages)

        # Should combine into single entry
        self.assertEqual(len(result), 1)

        storage = result[0]
        self.assertEqual(storage["storage"], "shared-storage")
        self.assertEqual(storage["node_count"], 2)
        self.assertIn("node1", storage["nodes"])
        self.assertIn("node2", storage["nodes"])
        self.assertEqual(storage["disk"], 10737418240)  # Max value, not sum
        self.assertEqual(storage["maxdisk"], 107374182400)
        self.assertEqual(storage["used_percent"], 10.0)  # 10GB / 100GB * 100
        self.assertTrue(storage["is_active"])

    def test_group_shared_storages_local_storage(self):
        """Test grouping local (non-shared) storage"""
        all_storages = [
            {
                "storage": "local-lvm",
                "node": "node1",
                "shared": 0,
                "enabled": 1,
                "status": "available",
                "disk": 5368709120,  # 5GB
                "maxdisk": 53687091200,  # 50GB
            },
            {
                "storage": "local-lvm",
                "node": "node2",
                "shared": 0,
                "enabled": 1,
                "status": "available",
                "disk": 10737418240,  # 10GB
                "maxdisk": 53687091200,  # 50GB
            },
        ]

        result = group_shared_storages(all_storages)

        # Should keep separate entries for local storage
        self.assertEqual(len(result), 2)

        for storage in result:
            self.assertEqual(storage["storage"], "local-lvm")
            self.assertTrue(storage["is_active"])
            # Should not have node_count for local storage
            self.assertNotIn("node_count", storage)

    def test_group_shared_storages_mixed(self):
        """Test grouping mixed shared and local storage"""
        all_storages = [
            {
                "storage": "shared-nfs",
                "node": "node1",
                "shared": 1,
                "enabled": 1,
                "disk": 10737418240,
                "maxdisk": 107374182400,
            },
            {
                "storage": "shared-nfs",
                "node": "node2",
                "shared": 1,
                "enabled": 1,
                "disk": 10737418240,
                "maxdisk": 107374182400,
            },
            {
                "storage": "local",
                "node": "node1",
                "shared": 0,
                "enabled": 1,
                "disk": 5368709120,
                "maxdisk": 53687091200,
            },
            {
                "storage": "local",
                "node": "node2",
                "shared": 0,
                "enabled": 1,
                "disk": 5368709120,
                "maxdisk": 53687091200,
            },
        ]

        result = group_shared_storages(all_storages)

        # Should have 3 entries: 1 shared + 2 local
        self.assertEqual(len(result), 3)

        # Find shared storage
        shared = next(s for s in result if "node_count" in s)
        self.assertEqual(shared["storage"], "shared-nfs")
        self.assertEqual(shared["node_count"], 2)

        # Find local storages
        local_storages = [s for s in result if "node_count" not in s]
        self.assertEqual(len(local_storages), 2)

    def test_group_shared_storages_inactive_storage(self):
        """Test grouping with inactive storage"""
        all_storages = [
            {
                "storage": "test-storage",
                "node": "node1",
                "shared": 1,
                "enabled": 0,  # Disabled
                "status": "unavailable",
                "disk": 0,
                "maxdisk": 0,
            },
            {
                "storage": "test-storage",
                "node": "node2",
                "shared": 1,
                "enabled": 1,  # Enabled
                "status": "available",
                "disk": 5368709120,
                "maxdisk": 53687091200,
            },
        ]

        result = group_shared_storages(all_storages)

        self.assertEqual(len(result), 1)
        storage = result[0]
        self.assertTrue(storage["is_active"])  # Should be active if enabled on any node

    def test_group_shared_storages_empty_input(self):
        """Test grouping with empty input"""
        result = group_shared_storages([])
        self.assertEqual(result, [])

    def test_group_shared_storages_no_shared_flag(self):
        """Test grouping storage without shared flag"""
        all_storages = [
            {
                "storage": "test-storage",
                "node": "node1",
                "enabled": 1,
                "disk": 5368709120,
                "maxdisk": 53687091200,
            }
        ]

        result = group_shared_storages(all_storages)

        # Storage without shared flag should be treated as local
        self.assertEqual(len(result), 1)
        storage = result[0]
        self.assertNotIn("node_count", storage)


if __name__ == "__main__":
    unittest.main()
