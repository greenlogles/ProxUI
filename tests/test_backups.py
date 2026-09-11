"""Focused tests for backup listing helpers and the backup management endpoints."""

import json
import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


def _backup(volid, vmid, ctime, storage="pbs", size=100, **extra):
    item = {
        "volid": volid,
        "vmid": vmid,
        "ctime": ctime,
        "size": size,
        "content": "backup",
        "format": "pbs-vm",
    }
    item.update(extra)
    return item


class BackupsTestCase(unittest.TestCase):
    def setUp(self):
        self.flask_app = app.app
        self.flask_app.config["TESTING"] = True
        self.client = self.flask_app.test_client()

        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()

        app.all_clusters["cluster-a"] = {
            "id": "cluster-a",
            "name": "Cluster A",
            "nodes": [],
        }
        app.current_cluster_id = "cluster-a"

        self.proxmox = Mock()
        self.proxmox.version.get.return_value = {"version": "8.4"}
        self.proxmox.cluster.status.get.return_value = [
            {"type": "node", "name": "pve-a"},
            {"type": "node", "name": "pve-b"},
        ]
        app.proxmox_nodes["pve-a"] = self.proxmox
        app.proxmox_nodes["pve-b"] = self.proxmox
        app.cluster_nodes.append(
            {"name": "pve-a", "status": "online", "connection": self.proxmox}
        )
        app.cluster_nodes.append(
            {"name": "pve-b", "status": "online", "connection": self.proxmox}
        )

    def tearDown(self):
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        app.current_cluster_id = None


class TestBackupStorages(BackupsTestCase):
    def test_skips_inactive_storage_and_lists_shared_once(self):
        self.proxmox.nodes.return_value.storage.get.return_value = [
            {"storage": "pbs", "content": "backup", "active": 1, "shared": 1},
            {"storage": "iso", "content": "iso,vztmpl", "active": 1, "shared": 0},
            {"storage": "old-nfs", "content": "backup", "active": 0, "shared": 1},
        ]

        storages = app._backup_storages(self.proxmox)

        self.assertEqual([s["storage"] for s in storages], ["pbs"])
        self.assertTrue(storages[0]["shared"])

    def test_local_storage_listed_per_node(self):
        self.proxmox.nodes.return_value.storage.get.return_value = [
            {"storage": "local", "content": "backup", "active": 1, "shared": 0},
        ]

        storages = app._backup_storages(self.proxmox)

        self.assertEqual(
            sorted((s["storage"], s["node"]) for s in storages),
            [("local", "pve-a"), ("local", "pve-b")],
        )


class TestCollectBackups(BackupsTestCase):
    def test_one_failing_storage_does_not_hide_the_others(self):
        good = self.proxmox.nodes.return_value.storage.return_value.content.get
        good.side_effect = [
            [_backup("pbs:backup/vm/101/a", 101, 100)],
            Exception("storage 'broken' is disabled"),
        ]
        storages = [
            {"storage": "pbs", "node": "pve-a", "error": None},
            {"storage": "broken", "node": "pve-a", "error": None},
        ]

        backups = app._collect_backups(storages)

        self.assertEqual([b["volid"] for b in backups], ["pbs:backup/vm/101/a"])
        self.assertIsNone(storages[0]["error"])
        self.assertIn("disabled", storages[1]["error"])

    def test_backups_are_sorted_newest_first(self):
        self.proxmox.nodes.return_value.storage.return_value.content.get.return_value = [
            _backup("pbs:old", 101, 100),
            _backup("pbs:new", 101, 300),
            _backup("pbs:mid", 101, 200),
        ]

        backups = app._collect_backups([{"storage": "pbs", "node": "pve-a"}])

        self.assertEqual(
            [b["volid"] for b in backups], ["pbs:new", "pbs:mid", "pbs:old"]
        )


class TestGroupBackupsByGuest(unittest.TestCase):
    def test_guests_without_backups_are_kept_with_zero_counts(self):
        guests = [{"vmid": 101, "name": "web", "type": "qemu", "node": "pve-a"}]

        rows = app._group_backups_by_guest([], guests)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 0)
        self.assertEqual(rows[0]["size"], 0)
        self.assertEqual(rows[0]["latest"], 0)
        self.assertFalse(rows[0]["orphan"])

    def test_backups_are_totalled_onto_their_guest(self):
        guests = [{"vmid": 101, "name": "web", "type": "qemu", "node": "pve-a"}]
        backups = [
            {"vmid": 101, "size": 10, "ctime": 300, "volid": "a"},
            {"vmid": 101, "size": 25, "ctime": 100, "volid": "b"},
        ]

        row = app._group_backups_by_guest(backups, guests)[0]

        self.assertEqual(row["count"], 2)
        self.assertEqual(row["size"], 35)
        self.assertEqual(row["latest"], 300)

    def test_backups_for_a_deleted_guest_become_orphan_rows_sorted_last(self):
        guests = [{"vmid": 101, "name": "web", "type": "qemu", "node": "pve-a"}]
        backups = [
            {"vmid": 101, "size": 10, "ctime": 300, "volid": "a"},
            {"vmid": 999, "size": 10, "ctime": 100, "volid": "b", "subtype": "lxc"},
        ]

        rows = app._group_backups_by_guest(backups, guests)

        self.assertEqual([r["vmid"] for r in rows], [101, 999])
        self.assertFalse(rows[0]["orphan"])
        self.assertTrue(rows[1]["orphan"])
        self.assertEqual(rows[1]["type"], "lxc")


class TestJobVmids(BackupsTestCase):
    def test_explicit_vmid_list_honours_exclusions(self):
        job = {"vmid": "101,102,103", "exclude": "102"}

        self.assertEqual(app._job_vmids(self.proxmox, job), [101, 103])

    def test_pool_selection_resolves_members(self):
        self.proxmox.pools.return_value.get.return_value = {
            "members": [{"vmid": 201}, {"vmid": 202}]
        }

        self.assertEqual(app._job_vmids(self.proxmox, {"pool": "prod"}), [201, 202])

    def test_all_selection_skips_templates_and_exclusions(self):
        self.proxmox.cluster.resources.get.return_value = [
            {"vmid": 101},
            {"vmid": 102, "template": 1},
            {"vmid": 103},
        ]

        self.assertEqual(
            app._job_vmids(self.proxmox, {"all": 1, "exclude": "103"}), [101]
        )


class TestBackupEndpoints(BackupsTestCase):
    def test_run_job_issues_one_vzdump_per_owning_node(self):
        self.proxmox.cluster.backup.get.return_value = [
            {
                "id": "job-1",
                "vmid": "101,102,103",
                "storage": "pbs",
                "mode": "snapshot",
                "compress": "zstd",
            }
        ]
        self.proxmox.cluster.resources.get.return_value = [
            {"vmid": 101, "node": "pve-a"},
            {"vmid": 102, "node": "pve-b"},
            {"vmid": 103, "node": "pve-a"},
        ]
        vzdump = self.proxmox.nodes.return_value.vzdump.post
        vzdump.return_value = "UPID:test"

        response = self.client.post("/api/backups/job/job-1/run")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(vzdump.call_count, 2)
        sent = sorted(c.kwargs["vmid"] for c in vzdump.call_args_list)
        self.assertEqual(sent, ["101,103", "102"])
        for call in vzdump.call_args_list:
            self.assertEqual(call.kwargs["storage"], "pbs")
            self.assertEqual(call.kwargs["mode"], "snapshot")

    def test_run_job_rejects_a_selection_with_no_guests(self):
        self.proxmox.cluster.backup.get.return_value = [
            {"id": "job-1", "vmid": "", "storage": "pbs"}
        ]

        response = self.client.post("/api/backups/job/job-1/run")

        self.assertEqual(response.status_code, 400)
        self.assertIn("no guests", response.get_json()["error"])

    def test_run_job_404s_for_an_unknown_job(self):
        self.proxmox.cluster.backup.get.return_value = []

        response = self.client.post("/api/backups/job/nope/run")

        self.assertEqual(response.status_code, 404)

    def test_guest_backup_runs_on_the_node_that_owns_it(self):
        self.proxmox.cluster.resources.get.return_value = [
            {"vmid": 101, "node": "pve-b"}
        ]
        vzdump = self.proxmox.nodes.return_value.vzdump.post
        vzdump.return_value = "UPID:test"

        response = self.client.post(
            "/api/backups/guest/101/run",
            data=json.dumps({"storage": "pbs", "mode": "stop", "compress": "zstd"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["node"], "pve-b")
        self.proxmox.nodes.assert_called_with("pve-b")
        # remove=0 keeps a one-off backup from triggering the storage's prune rules.
        self.assertEqual(
            vzdump.call_args.kwargs,
            {
                "storage": "pbs",
                "mode": "stop",
                "compress": "zstd",
                "vmid": 101,
                "remove": 0,
            },
        )

    def test_guest_backup_requires_a_storage(self):
        response = self.client.post(
            "/api/backups/guest/101/run",
            data=json.dumps({}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("storage is required", response.get_json()["error"])

    def test_toggle_job_sends_the_requested_state(self):
        response = self.client.post(
            "/api/backups/job/job-1/toggle",
            data=json.dumps({"enabled": False}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["enabled"], 0)
        self.proxmox.cluster.backup.assert_called_with("job-1")
        self.proxmox.cluster.backup.return_value.put.assert_called_with(enabled=0)

    def test_delete_backup_targets_the_storage_named_in_the_volid(self):
        response = self.client.delete(
            "/api/backups/content",
            data=json.dumps({"volid": "pbs:backup/vm/101/a", "node": "pve-a"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.proxmox.nodes.return_value.storage.assert_called_with("pbs")
        self.proxmox.nodes.return_value.storage.return_value.content.assert_called_with(
            "pbs:backup/vm/101/a"
        )

    def test_delete_backup_requires_volid_and_node(self):
        response = self.client.delete(
            "/api/backups/content",
            data=json.dumps({"volid": "pbs:backup/vm/101/a"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)


class TestDemoModeGating(BackupsTestCase):
    def setUp(self):
        super().setUp()
        self._demo = app.DEMO_MODE
        app.DEMO_MODE = True

    def tearDown(self):
        app.DEMO_MODE = self._demo
        super().tearDown()

    def test_write_endpoints_are_blocked(self):
        calls = [
            self.client.post("/api/backups/job/job-1/run"),
            self.client.post("/api/backups/job/job-1/toggle", json={"enabled": True}),
            self.client.post("/api/backups/guest/101/run", json={"storage": "pbs"}),
            self.client.delete(
                "/api/backups/content", json={"volid": "pbs:x", "node": "pve-a"}
            ),
        ]

        for response in calls:
            self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
