#!/usr/bin/env python3
"""
Pytest configuration and fixtures
"""

import os
import sys
import tempfile
from unittest.mock import Mock, patch

import pytest

# Add parent directory to path to import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


@pytest.fixture
def flask_app():
    """Create a Flask test app"""
    app.flask_app.config["TESTING"] = True
    app.flask_app.config["WTF_CSRF_ENABLED"] = False
    return app.flask_app


@pytest.fixture
def client(flask_app):
    """Create a test client"""
    return flask_app.test_client()


@pytest.fixture
def mock_proxmox_connection():
    """Create a mock Proxmox connection"""
    mock_conn = Mock()

    # Mock common API responses
    mock_conn.version.get.return_value = {"version": "7.4-15"}
    mock_conn.nodes.get.return_value = [{"node": "test-node", "status": "online"}]
    mock_conn.cluster.resources.get.return_value = []
    mock_conn.cluster.tasks.get.return_value = []
    mock_conn.cluster.nextid.get.return_value = 100

    return mock_conn


@pytest.fixture
def mock_cluster_config():
    """Create mock cluster configuration"""
    return {
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


@pytest.fixture
def setup_test_environment(mock_cluster_config, mock_proxmox_connection):
    """Set up a complete test environment"""
    # Clear global state
    app.proxmox_nodes.clear()
    app.cluster_nodes.clear()
    app.all_clusters.clear()
    app.connection_metadata.clear()

    # Set up test data
    app.all_clusters.update(mock_cluster_config)
    app.current_cluster_id = "test-cluster"
    app.proxmox_nodes["test-node"] = mock_proxmox_connection
    app.cluster_nodes.append(
        {"name": "test-node", "status": "online", "connection": mock_proxmox_connection}
    )

    yield

    # Clean up
    app.proxmox_nodes.clear()
    app.cluster_nodes.clear()
    app.all_clusters.clear()
    app.connection_metadata.clear()
    app.current_cluster_id = None


@pytest.fixture
def temp_config_file():
    """Create a temporary config file"""
    config_content = """
[[clusters]]
id = "test-cluster"
name = "Test Cluster"

[[clusters.nodes]]
host = "192.168.1.100"
user = "root@pam"
password = "testpass"
verify_ssl = false
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(config_content)
        temp_path = f.name

    yield temp_path

    # Clean up
    os.unlink(temp_path)


@pytest.fixture
def mock_vm_data():
    """Create mock VM data for testing"""
    return [
        {
            "vmid": "100",
            "node": "test-node",
            "type": "qemu",
            "name": "test-vm",
            "status": "running",
            "cpu": 0.1,
            "maxcpu": 2,
            "mem": 1073741824,  # 1GB
            "maxmem": 2147483648,  # 2GB
            "disk": 5368709120,  # 5GB
            "maxdisk": 21474836480,  # 20GB
            "cpu_percent": 10.0,
            "mem_percent": 50.0,
            "disk_percent": 25.0,
        },
        {
            "vmid": "101",
            "node": "test-node",
            "type": "lxc",
            "name": "test-container",
            "status": "stopped",
            "cpu": 0,
            "maxcpu": 1,
            "mem": 0,
            "maxmem": 1073741824,  # 1GB
            "disk": 1073741824,  # 1GB
            "maxdisk": 10737418240,  # 10GB
            "cpu_percent": 0.0,
            "mem_percent": 0.0,
            "disk_percent": 10.0,
        },
    ]


@pytest.fixture
def mock_storage_data():
    """Create mock storage data for testing"""
    return [
        {
            "storage": "local-lvm",
            "node": "test-node",
            "enabled": 1,
            "shared": 0,
            "content": "images,rootdir",
            "total": 21474836480,  # 20GB
            "used": 5368709120,  # 5GB
            "available": 16106127360,  # 15GB
            "used_percent": 25.0,
        },
        {
            "storage": "shared-nfs",
            "node": "test-node",
            "enabled": 1,
            "shared": 1,
            "content": "images,iso,backup",
            "total": 107374182400,  # 100GB
            "used": 10737418240,  # 10GB
            "available": 96636764160,  # 90GB
            "used_percent": 10.0,
        },
    ]


@pytest.fixture(autouse=True)
def reset_global_state():
    """Automatically reset global state before each test"""
    # Store original state
    original_nodes = app.proxmox_nodes.copy()
    original_cluster_nodes = app.cluster_nodes.copy()
    original_clusters = app.all_clusters.copy()
    original_metadata = app.connection_metadata.copy()
    original_cluster_id = app.current_cluster_id

    yield

    # Restore original state
    app.proxmox_nodes.clear()
    app.proxmox_nodes.update(original_nodes)

    app.cluster_nodes.clear()
    app.cluster_nodes.extend(original_cluster_nodes)

    app.all_clusters.clear()
    app.all_clusters.update(original_clusters)

    app.connection_metadata.clear()
    app.connection_metadata.update(original_metadata)

    app.current_cluster_id = original_cluster_id
