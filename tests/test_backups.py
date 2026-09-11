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


class TestApplyRealSizes(unittest.TestCase):
    def test_pbs_sizes_are_scaled_by_the_datastore_ratio(self):
        storages = [{"storage": "pbs", "type": "pbs", "used": 100}]
        backups = [
            {"storage": "pbs", "size": 600},
            {"storage": "pbs", "size": 400},
        ]

        app._apply_real_sizes(storages, backups)

        self.assertEqual(storages[0]["logical"], 1000)
        self.assertEqual(storages[0]["ratio"], 0.1)
        # The datastore's own used figure is the only exact number.
        self.assertEqual(storages[0]["backup_used"], 100)
        self.assertEqual([b["real"] for b in backups], [60, 40])
        self.assertTrue(all(b["estimated"] for b in backups))

    def test_file_storage_sizes_are_used_as_reported(self):
        storages = [{"storage": "nfs", "type": "nfs", "used": 999}]
        backups = [{"storage": "nfs", "size": 600}]

        app._apply_real_sizes(storages, backups)

        self.assertIsNone(storages[0]["ratio"])
        # A dir/NFS target holds nothing but these archives, so they add up.
        self.assertEqual(storages[0]["backup_used"], 600)
        self.assertEqual(backups[0]["real"], 600)
        self.assertFalse(backups[0]["estimated"])


class TestJobPayload(unittest.TestCase):
    def test_explicit_guest_list_is_normalised(self):
        params = app._job_payload(
            {"schedule": "sun 01:00", "storage": "pbs", "vmid": ["103", "101"]}
        )

        self.assertEqual(params["vmid"], "101,103")
        self.assertEqual(params["enabled"], 1)
        self.assertEqual(params["mode"], "snapshot")
        self.assertNotIn("all", params)

    def test_all_selection_carries_exclusions_only(self):
        params = app._job_payload(
            {
                "schedule": "sun 01:00",
                "storage": "pbs",
                "selection": "all",
                "exclude": "102, 101",
                "vmid": ["999"],
            }
        )

        self.assertEqual(params["all"], 1)
        self.assertEqual(params["exclude"], "101,102")
        self.assertNotIn("vmid", params)

    def test_pool_selection_requires_a_pool(self):
        with self.assertRaises(ValueError):
            app._job_payload(
                {"schedule": "sun 01:00", "storage": "pbs", "selection": "pool"}
            )

    def test_keep_last_becomes_a_prune_property_string(self):
        params = app._job_payload(
            {"schedule": "sun 01:00", "storage": "pbs", "vmid": "101", "keep-last": "7"}
        )

        self.assertEqual(params["prune-backups"], "keep-last=7")

    def test_schedule_and_storage_are_required(self):
        with self.assertRaises(ValueError):
            app._job_payload({"storage": "pbs", "vmid": "101"})
        with self.assertRaises(ValueError):
            app._job_payload({"schedule": "sun 01:00", "vmid": "101"})

    def test_empty_guest_list_is_rejected(self):
        with self.assertRaises(ValueError):
            app._job_payload({"schedule": "sun 01:00", "storage": "pbs", "vmid": ""})


class TestJobCrudEndpoints(BackupsTestCase):
    def test_create_posts_the_translated_payload(self):
        response = self.client.post(
            "/api/backups/job",
            json={
                "schedule": "sun 01:00",
                "storage": "pbs",
                "vmid": ["101", "102"],
                "keep-last": "5",
            },
        )

        self.assertEqual(response.status_code, 200)
        kwargs = self.proxmox.cluster.backup.post.call_args.kwargs
        self.assertEqual(kwargs["vmid"], "101,102")
        self.assertEqual(kwargs["prune-backups"], "keep-last=5")

    def test_create_reports_a_validation_error_as_400(self):
        response = self.client.post("/api/backups/job", json={"storage": "pbs"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("schedule is required", response.get_json()["error"])

    def test_update_deletes_properties_the_form_no_longer_sets(self):
        self.proxmox.cluster.backup.get.return_value = [
            {
                "id": "job-1",
                "vmid": "101",
                "compress": "zstd",
                "notes-template": "x",
                "storage": "pbs",
            }
        ]

        response = self.client.put(
            "/api/backups/job/job-1",
            json={
                "schedule": "sun 02:00",
                "storage": "pbs",
                "selection": "all",
                "compress": "zstd",
            },
        )

        self.assertEqual(response.status_code, 200)
        kwargs = self.proxmox.cluster.backup.return_value.put.call_args.kwargs
        self.assertEqual(kwargs["all"], 1)
        # vmid and notes-template were on the job but are not in the new payload.
        self.assertEqual(
            sorted(kwargs["delete"].split(",")), ["notes-template", "vmid"]
        )

    def test_update_404s_for_an_unknown_job(self):
        self.proxmox.cluster.backup.get.return_value = []

        response = self.client.put(
            "/api/backups/job/nope",
            json={"schedule": "sun 01:00", "storage": "pbs", "vmid": "101"},
        )

        self.assertEqual(response.status_code, 404)

    def test_delete_removes_the_job(self):
        response = self.client.delete("/api/backups/job/job-1")

        self.assertEqual(response.status_code, 200)
        self.proxmox.cluster.backup.assert_called_with("job-1")
        self.proxmox.cluster.backup.return_value.delete.assert_called_once()


class TestBackupConfigEndpoint(BackupsTestCase):
    def test_returns_the_config_stored_in_the_archive(self):
        extract = self.proxmox.nodes.return_value.vzdump.extractconfig.get
        extract.return_value = "cores: 2\nname: web\n"

        response = self.client.get(
            "/api/backups/content/config",
            query_string={"volid": "pbs:backup/vm/101/a", "node": "pve-a"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("cores: 2", response.get_json()["config"])
        extract.assert_called_with(volume="pbs:backup/vm/101/a")

    def test_requires_volid_and_node(self):
        response = self.client.get(
            "/api/backups/content/config", query_string={"volid": "pbs:x"}
        )

        self.assertEqual(response.status_code, 400)


class TestGuestBackupSummary(BackupsTestCase):
    def test_rolls_backups_up_per_storage(self):
        self.proxmox.nodes.return_value.storage.get.return_value = [
            {"storage": "pbs", "content": "backup", "active": 1, "shared": 1},
        ]
        content = self.proxmox.nodes.return_value.storage.return_value.content
        content.get.return_value = [
            _backup("pbs:backup/vm/101/a", 101, 100, size=10),
            _backup("pbs:backup/vm/101/b", 101, 300, size=20),
        ]

        response = self.client.get("/api/backups/guest/101")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["latest"], 300)
        self.assertEqual(
            body["by_storage"],
            [{"storage": "pbs", "count": 2, "size": 30, "latest": 300}],
        )
        # Only this guest's backups are fetched, not the whole storage.
        self.assertEqual(
            content.get.call_args.kwargs, {"content": "backup", "vmid": 101}
        )

    def test_reports_no_backups_without_failing(self):
        self.proxmox.nodes.return_value.storage.get.return_value = [
            {"storage": "pbs", "content": "backup", "active": 1, "shared": 1},
        ]
        self.proxmox.nodes.return_value.storage.return_value.content.get.return_value = (
            []
        )

        body = self.client.get("/api/backups/guest/101").get_json()

        self.assertEqual(body["count"], 0)
        self.assertEqual(body["latest"], 0)
        self.assertEqual(body["by_storage"], [])
        self.assertEqual([s["storage"] for s in body["storages"]], ["pbs"])


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
            self.client.post(
                "/api/backups/job",
                json={"schedule": "sun 01:00", "storage": "pbs", "vmid": "101"},
            ),
            self.client.put(
                "/api/backups/job/job-1",
                json={"schedule": "sun 01:00", "storage": "pbs", "vmid": "101"},
            ),
            self.client.delete("/api/backups/job/job-1"),
        ]

        for response in calls:
            self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
