"""Tests for the cluster / node / guest firewall endpoints."""

import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


class FirewallTestCase(unittest.TestCase):
    def setUp(self):
        self.flask_app = app.app
        self.flask_app.config["TESTING"] = True
        self.client = self.flask_app.test_client()

        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.connection_metadata.clear()
        app.all_clusters.clear()

        app.all_clusters["cluster-a"] = {"id": "cluster-a", "name": "A", "nodes": []}
        app.current_cluster_id = "cluster-a"
        # check_config() redirects every request to /connect without these.
        self._had_config = app.config_file_exists
        app.config_file_exists = True

        self.proxmox = Mock()
        app.proxmox_nodes["pve-a"] = self.proxmox
        app.cluster_nodes.append(
            {"name": "pve-a", "status": "online", "connection": self.proxmox}
        )

        self.patcher = patch("app.get_proxmox_connection", return_value=self.proxmox)
        self.patcher.start()

        self.cluster_fw = self.proxmox.cluster.firewall
        self.node_fw = self.proxmox.nodes.return_value.firewall
        self.qemu = self.proxmox.nodes.return_value.qemu.return_value
        self.lxc = self.proxmox.nodes.return_value.lxc.return_value
        self.cluster_fw.options.get.return_value = {"enable": 1, "digest": "c1"}

    def tearDown(self):
        self.patcher.stop()
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.connection_metadata.clear()
        app.all_clusters.clear()
        app.current_cluster_id = None
        app.config_file_exists = self._had_config


class TestFirewallOptions(FirewallTestCase):
    def test_cluster_options_return_defaults_for_unset_keys(self):
        self.cluster_fw.options.get.return_value = {"digest": "d0"}
        r = self.client.get("/api/cluster/firewall/options")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["scope"], "cluster")
        self.assertEqual(data["options"], {})
        self.assertEqual(data["defaults"]["enable"], 0)
        self.assertEqual(data["defaults"]["policy_in"], "DROP")
        self.assertEqual(data["digest"], "d0")
        self.assertNotIn("cluster_enabled", data)

    def test_node_options_report_cluster_state(self):
        self.cluster_fw.options.get.return_value = {"digest": "x"}
        self.node_fw.options.get.return_value = {"enable": 1, "nftables": 1}
        data = self.client.get("/api/node/pve-a/firewall/options").get_json()
        self.assertEqual(data["scope"], "node")
        self.assertEqual(data["options"], {"enable": 1})
        self.assertFalse(data["cluster_enabled"])

    def test_guest_options_list_nic_firewall_flags(self):
        self.qemu.firewall.options.get.return_value = {"enable": 1}
        self.qemu.config.get.return_value = {
            "net0": "virtio=AA:BB,bridge=vmbr0,firewall=1",
            "net1": "virtio=CC:DD,bridge=vmbr1",
            "name": "vm",
        }
        data = self.client.get("/api/vm/pve-a/100/firewall/options").get_json()
        self.assertEqual(data["scope"], "guest")
        self.assertTrue(data["cluster_enabled"])
        self.assertEqual(
            data["nics"],
            [
                {"key": "net0", "firewall": True, "pending": False},
                {"key": "net1", "firewall": False, "pending": False},
            ],
        )

    def test_guest_falls_back_to_lxc(self):
        self.qemu.status.current.get.side_effect = Exception("500 no such VM")
        self.lxc.firewall.options.get.return_value = {}
        self.lxc.config.get.return_value = {}
        r = self.client.get("/api/vm/pve-a/101/firewall/options")
        self.assertEqual(r.status_code, 200)
        self.lxc.firewall.options.get.assert_called_once_with()

    def test_put_enable_and_reset_policy(self):
        r = self.client.put(
            "/api/vm/pve-a/100/firewall/options",
            json={"enable": True, "policy_in": "", "policy_out": "REJECT"},
        )
        self.assertEqual(r.status_code, 200)
        self.qemu.firewall.options.put.assert_called_once_with(
            enable=1, policy_out="REJECT", delete="policy_in"
        )

    def test_put_rejects_unknown_policy(self):
        r = self.client.put(
            "/api/cluster/firewall/options", json={"policy_in": "ALLOW"}
        )
        self.assertEqual(r.status_code, 400)
        self.cluster_fw.options.put.assert_not_called()

    def test_put_ignores_keys_outside_scope(self):
        r = self.client.put(
            "/api/node/pve-a/firewall/options",
            json={"policy_in": "DROP", "nf_conntrack_max": 1},
        )
        self.assertEqual(r.status_code, 400)
        self.node_fw.options.put.assert_not_called()

    def test_put_passes_digest(self):
        self.client.put(
            "/api/cluster/firewall/options", json={"enable": 0, "digest": "abc"}
        )
        self.cluster_fw.options.put.assert_called_once_with(enable=0, digest="abc")


class TestFirewallRules(FirewallTestCase):
    def test_list_sorted_by_pos(self):
        self.node_fw.rules.get.return_value = [
            {"pos": 1, "type": "in", "action": "DROP"},
            {"pos": 0, "type": "in", "action": "ACCEPT"},
        ]
        data = self.client.get("/api/node/pve-a/firewall/rules").get_json()
        self.assertEqual([r["pos"] for r in data["rules"]], [0, 1])

    def test_add_rule_drops_empty_fields(self):
        r = self.client.post(
            "/api/cluster/firewall/rules",
            json={
                "type": "in",
                "action": "ACCEPT",
                "macro": "SSH",
                "source": " 10.0.0.0/24 ",
                "dport": "",
                "enable": True,
            },
        )
        self.assertEqual(r.status_code, 200)
        self.cluster_fw.rules.post.assert_called_once_with(
            type="in", action="ACCEPT", macro="SSH", source="10.0.0.0/24", enable=1
        )

    def test_add_rule_validates_type_and_action(self):
        for body in (
            {"type": "sideways", "action": "ACCEPT"},
            {"type": "in", "action": "ALLOW"},
            {"type": "group", "action": "bad name!"},
            {"type": "in", "action": "ACCEPT", "comment": "a\nb"},
            {"type": "in", "action": "ACCEPT", "log": "loud"},
        ):
            r = self.client.post("/api/node/pve-a/firewall/rules", json=body)
            self.assertEqual(r.status_code, 400, body)
        self.node_fw.rules.post.assert_not_called()

    def test_add_group_rule(self):
        r = self.client.post(
            "/api/vm/pve-a/100/firewall/rules",
            json={"type": "group", "action": "webservers", "enable": 1},
        )
        self.assertEqual(r.status_code, 200)
        self.qemu.firewall.rules.post.assert_called_once_with(
            type="group", action="webservers", enable=1
        )

    def test_edit_clears_emptied_fields(self):
        r = self.client.put(
            "/api/node/pve-a/firewall/rules/3",
            json={
                "type": "in",
                "action": "DROP",
                "proto": "tcp",
                "dport": "22",
                "enable": 0,
                "digest": "d1",
            },
        )
        self.assertEqual(r.status_code, 200)
        self.node_fw.rules.assert_called_with(3)
        kwargs = self.node_fw.rules.return_value.put.call_args.kwargs
        self.assertEqual(kwargs["proto"], "tcp")
        self.assertEqual(kwargs["dport"], "22")
        self.assertEqual(kwargs["enable"], 0)
        self.assertEqual(kwargs["digest"], "d1")
        deleted = set(kwargs["delete"].split(","))
        self.assertIn("source", deleted)
        self.assertIn("icmp-type", deleted)
        self.assertIn("log", deleted)
        self.assertNotIn("dport", deleted)

    def test_toggle_enable_only(self):
        self.client.put(
            "/api/node/pve-a/firewall/rules/2", json={"enable": False, "digest": "d"}
        )
        self.node_fw.rules.return_value.put.assert_called_once_with(
            enable=0, digest="d"
        )

    def test_move_up_and_down(self):
        # PVE inserts before the rule at `moveto` in the original list.
        self.client.put("/api/node/pve-a/firewall/rules/2", json={"move": "up"})
        self.node_fw.rules.return_value.put.assert_called_with(moveto=1)
        self.client.put("/api/node/pve-a/firewall/rules/2", json={"move": "down"})
        self.node_fw.rules.return_value.put.assert_called_with(moveto=4)

    def test_move_up_from_top_rejected(self):
        r = self.client.put("/api/node/pve-a/firewall/rules/0", json={"move": "up"})
        self.assertEqual(r.status_code, 400)

    def test_delete_with_digest(self):
        r = self.client.delete("/api/vm/pve-a/100/firewall/rules/1?digest=zz")
        self.assertEqual(r.status_code, 200)
        self.qemu.firewall.rules.assert_called_with(1)
        self.qemu.firewall.rules.return_value.delete.assert_called_once_with(
            digest="zz"
        )

    def test_proxmox_error_is_forwarded(self):
        self.node_fw.rules.post.side_effect = Exception("400 Bad Request: bad dport")
        r = self.client.post(
            "/api/node/pve-a/firewall/rules", json={"type": "in", "action": "DROP"}
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("bad dport", r.get_json()["error"])


class TestFirewallNic(FirewallTestCase):
    def test_enable_keeps_other_nic_settings(self):
        self.qemu.config.get.side_effect = [
            {"net0": "virtio=AA:BB,bridge=vmbr0,tag=5", "digest": "cfg1"},
            {"net0": "virtio=AA:BB,bridge=vmbr0,tag=5,firewall=1"},
        ]
        r = self.client.put(
            "/api/vm/pve-a/100/firewall/nics/net0", json={"firewall": True}
        )
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("pending", r.get_json())
        self.qemu.config.put.assert_called_once_with(
            net0="virtio=AA:BB,bridge=vmbr0,tag=5,firewall=1", digest="cfg1"
        )

    def test_disable_strips_flag(self):
        self.qemu.config.get.side_effect = [
            {"net1": "virtio=AA:BB,firewall=1,bridge=vmbr1"},
            {"net1": "virtio=AA:BB,bridge=vmbr1"},
        ]
        self.client.put("/api/vm/pve-a/100/firewall/nics/net1", json={"firewall": 0})
        self.qemu.config.put.assert_called_once_with(net1="virtio=AA:BB,bridge=vmbr1")

    def test_reports_pending_when_not_hotplugged(self):
        self.qemu.config.get.side_effect = [
            {"net0": "virtio=AA:BB,bridge=vmbr0"},
            {"net0": "virtio=AA:BB,bridge=vmbr0"},
        ]
        data = self.client.put(
            "/api/vm/pve-a/100/firewall/nics/net0", json={"firewall": True}
        ).get_json()
        self.assertTrue(data["pending"])

    def test_rejects_bad_key_and_missing_nic(self):
        r = self.client.put("/api/vm/pve-a/100/firewall/nics/scsi0", json={})
        self.assertEqual(r.status_code, 400)
        self.qemu.config.get.return_value = {}
        r = self.client.put("/api/vm/pve-a/100/firewall/nics/net3", json={})
        self.assertEqual(r.status_code, 404)
        self.qemu.config.put.assert_not_called()

    def test_options_mark_pending_nic_change(self):
        self.qemu.firewall.options.get.return_value = {}
        self.qemu.config.get.return_value = {"net0": "virtio=AA,bridge=vmbr0"}
        self.qemu.pending.get.return_value = [
            {
                "key": "net0",
                "value": "virtio=AA,bridge=vmbr0",
                "pending": "virtio=AA,bridge=vmbr0,firewall=1",
            },
            {"key": "memory", "value": 1024},
        ]
        data = self.client.get("/api/vm/pve-a/100/firewall/options").get_json()
        self.assertEqual(
            data["nics"], [{"key": "net0", "firewall": False, "pending": True}]
        )


class TestFirewallGroups(FirewallTestCase):
    def test_create_validates_name(self):
        r = self.client.post("/api/cluster/firewall/groups", json={"group": "1bad"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post(
            "/api/cluster/firewall/groups", json={"group": "web", "comment": "w"}
        )
        self.assertEqual(r.status_code, 200)
        self.cluster_fw.groups.post.assert_called_once_with(group="web", comment="w")

    def test_rename_posts_with_rename(self):
        self.client.put(
            "/api/cluster/firewall/groups/web",
            json={"group": "web2", "comment": "c"},
        )
        self.cluster_fw.groups.post.assert_called_once_with(
            group="web2", rename="web", comment="c"
        )

    def test_group_rules_use_group_endpoint(self):
        self.cluster_fw.groups.return_value.get.return_value = [
            {"pos": 0, "type": "in", "action": "ACCEPT"}
        ]
        data = self.client.get("/api/cluster/firewall/groups/web/rules").get_json()
        self.assertEqual(data["scope"], "group")
        self.cluster_fw.groups.assert_called_with("web")
        self.client.put(
            "/api/cluster/firewall/groups/web/rules/0", json={"move": "down"}
        )
        self.cluster_fw.groups.return_value.return_value.put.assert_called_with(
            moveto=2
        )

    def test_group_rules_reject_bad_group_name(self):
        r = self.client.get("/api/cluster/firewall/groups/bad!name/rules")
        self.assertEqual(r.status_code, 400)


class TestFirewallAliases(FirewallTestCase):
    def test_create_and_validate(self):
        r = self.client.post(
            "/api/cluster/firewall/aliases", json={"name": "lan", "cidr": ""}
        )
        self.assertEqual(r.status_code, 400)
        self.client.post(
            "/api/cluster/firewall/aliases",
            json={"name": "lan", "cidr": " 10.0.0.0/24 "},
        )
        self.cluster_fw.aliases.post.assert_called_once_with(
            name="lan", cidr="10.0.0.0/24"
        )

    def test_edit_with_rename(self):
        self.client.put(
            "/api/cluster/firewall/aliases/lan",
            json={"name": "lan2", "cidr": "10.0.0.0/8", "comment": "", "digest": "d"},
        )
        self.cluster_fw.aliases.assert_called_with("lan")
        self.cluster_fw.aliases.return_value.put.assert_called_once_with(
            cidr="10.0.0.0/8", comment="", rename="lan2", digest="d"
        )


class TestFirewallIpsets(FirewallTestCase):
    def test_list_includes_entries(self):
        self.cluster_fw.ipset.get.return_value = [{"name": "b"}, {"name": "a"}]
        self.cluster_fw.ipset.return_value.get.return_value = [{"cidr": "10.0.0.1"}]
        data = self.client.get("/api/cluster/firewall/ipsets").get_json()
        self.assertEqual([s["name"] for s in data["ipsets"]], ["a", "b"])
        self.assertEqual(data["ipsets"][0]["entries"], [{"cidr": "10.0.0.1"}])

    def test_delete_forces(self):
        self.client.delete("/api/cluster/firewall/ipsets/admins")
        self.cluster_fw.ipset.return_value.delete.assert_called_once_with(force=1)

    def test_entry_with_slash_in_path(self):
        entry = self.cluster_fw.ipset.return_value.return_value
        r = self.client.put(
            "/api/cluster/firewall/ipsets/admins/entries/10.0.0.0/24",
            json={"comment": "net", "nomatch": True},
        )
        self.assertEqual(r.status_code, 200)
        self.cluster_fw.ipset.return_value.assert_called_with("10.0.0.0/24")
        entry.put.assert_called_once_with(nomatch=1, comment="net")

    def test_add_entry(self):
        self.client.post(
            "/api/cluster/firewall/ipsets/admins/entries",
            json={"cidr": "10.0.0.5", "comment": ""},
        )
        self.cluster_fw.ipset.return_value.post.assert_called_once_with(
            cidr="10.0.0.5", nomatch=0
        )

    def test_entry_rejects_bad_cidr(self):
        r = self.client.post(
            "/api/cluster/firewall/ipsets/admins/entries",
            json={"cidr": "10.0.0.5; rm"},
        )
        self.assertEqual(r.status_code, 400)


class TestFirewallPage(FirewallTestCase):
    def test_page_renders_cluster_card(self):
        r = self.client.get("/firewall")
        self.assertEqual(r.status_code, 200)
        self.assertIn("firewallPanel('cluster'", r.get_data(as_text=True))


class TestFirewallChoices(FirewallTestCase):
    def test_macros_and_groups_sorted(self):
        self.cluster_fw.macros.get.return_value = [
            {"macro": "SSH", "descr": "Secure shell"},
            {"macro": "HTTP", "descr": "Web"},
        ]
        self.cluster_fw.groups.get.return_value = [{"group": "web"}, {"group": "db"}]
        self.cluster_fw.aliases.get.return_value = [{"name": "lan"}]
        self.cluster_fw.ipset.get.return_value = [{"name": "admins"}]
        data = self.client.get("/api/firewall/choices").get_json()
        self.assertEqual([m["name"] for m in data["macros"]], ["HTTP", "SSH"])
        self.assertEqual(data["groups"], ["db", "web"])
        self.assertEqual(data["addresses"], ["lan", "+admins"])


if __name__ == "__main__":
    unittest.main()
