#!/usr/bin/env python3
"""
Unit tests for VM and container management functions
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

# Add parent directory to path to import app
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (
    cluster_nodes,
    get_all_vms_and_containers,
    get_all_vms_and_containers_fallback,
    get_qemu_guest_disk_info,
    parse_ssh_keys,
    parse_vm_configuration,
    proxmox_nodes,
)


class TestVMManagement(unittest.TestCase):

    def setUp(self):
        """Set up test fixtures"""
        import app

        # Reset global state using module globals
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()

        # Set up mock connections
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
        import app

        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()

    @patch("app.get_proxmox_connection")
    def test_get_all_vms_and_containers_success(self, mock_get_connection):
        """Test successful retrieval of VMs and containers"""
        # Mock connection
        mock_connection = Mock()
        mock_get_connection.return_value = mock_connection

        # Mock cluster resources response
        mock_resources = [
            {
                "node": "test-node",
                "vmid": "100",
                "type": "qemu",
                "name": "test-vm",
                "status": "running",
                "cpu": 0.1,
                "maxcpu": 2,
                "mem": 1073741824,  # 1GB
                "maxmem": 2147483648,  # 2GB
                "disk": 5368709120,  # 5GB
                "maxdisk": 21474836480,  # 20GB
            },
            {
                "node": "test-node",
                "vmid": "101",
                "type": "lxc",
                "name": "test-container",
                "status": "stopped",
                "cpu": 0,
                "maxcpu": 1,
                "mem": 0,
                "maxmem": 1073741824,
                "disk": 1073741824,
                "maxdisk": 10737418240,
            },
        ]
        mock_connection.cluster.resources.get.return_value = mock_resources

        result = get_all_vms_and_containers()

        self.assertEqual(len(result), 2)

        # Check VM data
        vm = result[0]
        self.assertEqual(vm["vmid"], "100")
        self.assertEqual(vm["type"], "qemu")
        self.assertEqual(vm["cpu_percent"], 10.0)  # 0.1 * 100
        self.assertEqual(vm["mem_percent"], 50.0)  # 1GB / 2GB * 100
        self.assertEqual(vm["disk_percent"], 25.0)  # 5GB / 20GB * 100

        # Check container data
        container = result[1]
        self.assertEqual(container["vmid"], "101")
        self.assertEqual(container["type"], "lxc")

    @patch("app.get_proxmox_connection")
    @patch("app.get_all_vms_and_containers_fallback")
    def test_get_all_vms_and_containers_fallback_on_error(
        self, mock_fallback, mock_get_connection
    ):
        """Test fallback when cluster endpoint fails"""
        # Mock connection that raises exception
        mock_connection = Mock()
        mock_connection.cluster.resources.get.side_effect = Exception(
            "Cluster endpoint failed"
        )
        mock_get_connection.return_value = mock_connection

        mock_fallback.return_value = [{"vmid": "100", "type": "qemu"}]

        result = get_all_vms_and_containers()

        mock_fallback.assert_called_once()
        self.assertEqual(len(result), 1)

    @patch("app.get_proxmox_connection")
    def test_get_all_vms_and_containers_fallback(self, mock_get_connection):
        """Test fallback method for getting VMs and containers"""
        # Mock connection
        mock_connection = Mock()
        mock_get_connection.return_value = mock_connection

        # Mock node responses
        mock_vms = [
            {
                "vmid": "100",
                "name": "test-vm",
                "status": "running",
                "cpu": 0.2,
                "mem": 1073741824,
                "maxmem": 2147483648,
                "disk": 5368709120,
                "maxdisk": 21474836480,
            }
        ]

        mock_containers = [
            {
                "vmid": "101",
                "name": "test-container",
                "status": "stopped",
                "cpu": 0,
                "mem": 0,
                "maxmem": 1073741824,
                "disk": 1073741824,
                "maxdisk": 10737418240,
            }
        ]

        mock_connection.nodes.return_value.qemu.get.return_value = mock_vms
        mock_connection.nodes.return_value.lxc.get.return_value = mock_containers

        result = get_all_vms_and_containers_fallback()

        self.assertEqual(len(result), 2)

        # Check that node and type are added
        vm = next(r for r in result if r["vmid"] == "100")
        self.assertEqual(vm["node"], "test-node")
        self.assertEqual(vm["type"], "qemu")

        container = next(r for r in result if r["vmid"] == "101")
        self.assertEqual(container["node"], "test-node")
        self.assertEqual(container["type"], "lxc")

    def test_get_qemu_guest_disk_info_success(self):
        """Test successful QEMU guest disk info retrieval"""
        mock_proxmox = Mock()

        # Mock filesystem info response
        mock_fsinfo = {
            "result": [
                {
                    "name": "sda1",
                    "mountpoint": "/",
                    "type": "ext4",
                    "used-bytes": 5368709120,  # 5GB
                    "total-bytes": 21474836480,  # 20GB
                    "disk": [{"serial": "drive-scsi0"}],
                },
                {
                    "name": "tmpfs",
                    "mountpoint": "/tmp",
                    "type": "squashfs",  # Should be ignored
                    "used-bytes": 1048576,
                    "total-bytes": 104857600,
                },
            ]
        }

        mock_proxmox.nodes.return_value.qemu.return_value.agent.get.return_value = (
            mock_fsinfo
        )

        result = get_qemu_guest_disk_info(mock_proxmox, "test-node", "100")

        self.assertEqual(len(result), 1)  # squashfs should be filtered out

        disk = result[0]
        self.assertEqual(disk["name"], "sda1")
        self.assertEqual(disk["mountpoint"], "/")
        self.assertEqual(disk["type"], "ext4")
        self.assertEqual(disk["used_percent"], 25.0)  # 5GB / 20GB * 100
        self.assertEqual(disk["used_gb"], 5.0)
        self.assertEqual(disk["total_gb"], 20.0)
        self.assertEqual(disk["free_gb"], 15.0)

    def test_get_qemu_guest_disk_info_failure(self):
        """Test QEMU guest disk info retrieval failure"""
        mock_proxmox = Mock()
        mock_proxmox.nodes.return_value.qemu.return_value.agent.get.side_effect = (
            Exception("Guest agent not available")
        )

        result = get_qemu_guest_disk_info(mock_proxmox, "test-node", "100")

        self.assertIsNone(result)

    def test_parse_vm_configuration(self):
        """Test VM configuration parsing"""
        config = {
            # CPU config
            "cores": 2,
            "sockets": 1,
            "cpu": "host",
            "cpulimit": 1,
            # Memory config
            "memory": 2048,
            "balloon": 1024,
            # Network config
            "net0": "virtio=52:54:00:12:34:56,bridge=vmbr0,firewall=1",
            "net1": "e1000,bridge=vmbr1",
            # Storage config
            "scsi0": "local-lvm:vm-100-disk-0,size=32G",
            "ide2": "local:iso/ubuntu-22.04.iso,media=cdrom",
            # General config
            "name": "test-vm",
            "ostype": "l26",
            "boot": "order=scsi0;ide2",
            "onboot": 1,
            # Cloud-init config
            "ciuser": "ubuntu",
            "sshkeys": "ssh-rsa%20AAAAB3NzaC1yc2EAAAADAQABAAABAQ%0A",
            "ipconfig0": "ip=192.168.1.100/24,gw=192.168.1.1",
            # Other config
            "digest": "abc123",
            "unknown_field": "value",
        }

        result = parse_vm_configuration(config, "qemu")

        # Check CPU section
        self.assertEqual(result["cpu"]["cores"], 2)
        self.assertEqual(result["cpu"]["sockets"], 1)
        self.assertEqual(result["cpu"]["cpu"], "host")

        # Check memory section
        self.assertEqual(result["memory"]["memory"], 2048)
        self.assertEqual(result["memory"]["balloon"], 1024)

        # Check network section
        self.assertEqual(len(result["network"]), 2)
        net0 = result["network"][0]
        self.assertEqual(net0["interface"], "net0")

        # Check storage section
        self.assertEqual(len(result["storage"]), 2)
        scsi0 = next(s for s in result["storage"] if s["device"] == "scsi0")
        self.assertEqual(scsi0["config"], "local-lvm:vm-100-disk-0,size=32G")

        # Check general section
        self.assertEqual(result["general"]["name"], "test-vm")
        self.assertEqual(result["general"]["ostype"], "l26")

        # Check cloud-init section
        self.assertEqual(result["cloud_init"]["ciuser"], "ubuntu")
        self.assertIn("sshkeys", result["cloud_init"])

        # Check other section
        self.assertEqual(result["other"]["digest"], "abc123")
        self.assertEqual(result["other"]["unknown_field"], "value")

    def test_parse_ssh_keys(self):
        """Test SSH keys parsing"""
        # URL-encoded SSH keys string
        ssh_keys_encoded = "ssh-rsa%20AAAAB3NzaC1yc2EAAAADAQABAAABAQC7%2BExample%2BKey%2BData%20user%40example.com%0Assh-ed25519%20AAAAC3NzaC1lZDI1NTE5AAAAIExampleEd25519KeyData%20admin%40example.com"

        result = parse_ssh_keys(ssh_keys_encoded)

        self.assertEqual(len(result), 2)

        # Check first key (RSA)
        rsa_key = result[0]
        self.assertEqual(rsa_key["type"], "ssh-rsa")
        self.assertEqual(rsa_key["comment"], "user@example.com")
        self.assertIn("AAAAB3NzaC", rsa_key["preview"])  # Check beginning part

        # Check second key (Ed25519)
        ed25519_key = result[1]
        self.assertEqual(ed25519_key["type"], "ssh-ed25519")
        self.assertEqual(ed25519_key["comment"], "admin@example.com")

    def test_parse_ssh_keys_malformed(self):
        """Test parsing malformed SSH keys"""
        malformed_keys = "invalid-key-format%0Aanother-malformed-entry"

        result = parse_ssh_keys(malformed_keys)

        self.assertEqual(len(result), 2)
        for key in result:
            self.assertEqual(key["type"], "unknown")
            self.assertEqual(key["comment"], "malformed key")

    def test_parse_ssh_keys_empty(self):
        """Test parsing empty SSH keys"""
        result = parse_ssh_keys("")
        self.assertEqual(result, [])

        result = parse_ssh_keys(None)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
