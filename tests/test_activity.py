#!/usr/bin/env python3
"""
Tests for the activity feed that merges native Proxmox tasks and ProxUI jobs.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
from app import app as flask_app

MIGRATE_UPID = "UPID:pve1:0000ABCD:0001ABCD:6730A000:qmigrate:100:root@pam:"
CLONE_UPID = "UPID:pve1:0000ABCE:0001ABCE:6730A100:qmclone:9000:root@pam!proxui:"


class TestParseTaskProgress(unittest.TestCase):
    def test_mirror_line(self):
        self.assertEqual(
            app.parse_task_progress(
                "mirror-scsi0: transferred 1.4 GiB of 60.0 GiB (2.41%) in 5s"
            ),
            {"percent": 2.41, "text": "mirror-scsi0: 1.4 GiB of 60.0 GiB (2.41%)"},
        )

    def test_clone_line_without_drive(self):
        self.assertEqual(
            app.parse_task_progress("transferred 8.0 GiB of 32.0 GiB (25.00%)"),
            {"percent": 25.0, "text": "8.0 GiB of 32.0 GiB (25.00%)"},
        )

    def test_vzdump_line(self):
        p = app.parse_task_progress(
            "INFO:  45% (18.0 GiB of 40.0 GiB) in 2m 3s, read: 150 MiB/s"
        )
        self.assertEqual(p["percent"], 45.0)
        self.assertEqual(p["text"], "18.0 GiB of 40.0 GiB (45.00%)")

    def test_ram_state_line_is_not_progress(self):
        self.assertIsNone(
            app.parse_task_progress(
                "migration active, transferred 351.2 MiB of 4.0 GiB VM-state, 110.6 MiB/s"
            )
        )

    def test_plain_line(self):
        self.assertIsNone(app.parse_task_progress("starting migration of VM 100"))


class TestParseUpid(unittest.TestCase):
    def test_valid(self):
        p = app._parse_upid(CLONE_UPID)
        self.assertEqual(p["node"], "pve1")
        self.assertEqual(p["type"], "qmclone")
        self.assertEqual(p["id"], "9000")
        self.assertEqual(p["user"], "root@pam!proxui")
        self.assertEqual(p["starttime"], 0x6730A100)

    def test_rejects_path_tricks(self):
        self.assertIsNone(app._parse_upid("UPID:pve1/../x:1:1:1:qmstart:100:root@pam:"))
        self.assertIsNone(app._parse_upid("not-a-upid"))


class ActivityTestBase(unittest.TestCase):
    def setUp(self):
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()
        app.proxmox_nodes.clear()
        app._task_progress_cache.clear()
        self.conn = MagicMock()
        app.proxmox_nodes["pve1"] = self.conn
        self.job_ids = []
        for patcher in (
            patch("app.get_proxmox_connection", return_value=self.conn),
            patch("app.config_file_exists", True),
            patch.dict(
                "app.all_clusters", {"c": {"id": "c", "name": "C", "nodes": []}}
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        for job_id in self.job_ids:
            app.job_queue.delete_job(job_id)
        app.proxmox_nodes.clear()
        app._task_progress_cache.clear()

    def make_job(self, upids=()):
        job_id = app.job_queue.create_job(
            "clone", "Clone VM 100 → 9000", {"target_node": "pve1"}
        )
        self.job_ids.append(job_id)
        for upid in upids:
            app.job_queue.add_upid(job_id, "pve1", upid)
        return job_id

    def task(self):
        return self.conn.nodes.return_value.tasks.return_value


class TestActivityFeed(ActivityTestBase):
    def test_merges_and_nests_job_tasks(self):
        job_id = self.make_job([CLONE_UPID])
        self.conn.cluster.tasks.get.return_value = [
            {
                "upid": MIGRATE_UPID,
                "node": "pve1",
                "type": "qmigrate",
                "id": "100",
                "user": "root@pam",
                "starttime": 0x6730A000,
            },
            {
                "upid": CLONE_UPID,
                "node": "pve1",
                "type": "qmclone",
                "id": "9000",
                "starttime": 0x6730A100,
                "endtime": 0x6730A200,
                "status": "OK",
            },
        ]
        self.task().log.get.return_value = [
            {"n": 1, "t": "starting migration of VM 100 to node 'pve2'"},
            {
                "n": 2,
                "t": "mirror-scsi0: transferred 2.6 GiB of 60.0 GiB (4.27%) in 9s",
            },
        ]

        data = self.client.get("/api/activity").get_json()
        self.assertIsNone(data["error"])
        ids = [i["id"] for i in data["items"]]
        self.assertIn(job_id, ids)
        self.assertIn(MIGRATE_UPID, ids)
        self.assertNotIn(CLONE_UPID, ids, "job child must not be listed twice")

        job = next(i for i in data["items"] if i["id"] == job_id)
        self.assertEqual(job["source"], "proxui")
        self.assertEqual([c["id"] for c in job["children"]], [CLONE_UPID])
        self.assertEqual(job["children"][0]["status"], "ok")

        mig = next(i for i in data["items"] if i["id"] == MIGRATE_UPID)
        self.assertEqual(mig["status"], "running")
        self.assertEqual(mig["vmid"], "100")
        self.assertEqual(mig["progress"], 4)
        self.assertEqual(
            mig["current_step"], "mirror-scsi0: 2.6 GiB of 60.0 GiB (4.27%)"
        )

    def test_log_is_read_incrementally(self):
        running = {
            "upid": MIGRATE_UPID,
            "node": "pve1",
            "type": "qmigrate",
            "id": "100",
            "starttime": 1,
        }
        self.conn.cluster.tasks.get.return_value = [running]
        self.task().log.get.side_effect = [
            [{"n": 1, "t": "mirror-scsi0: transferred 1 GiB of 10 GiB (10.00%) in 1s"}],
            [],
        ]
        self.client.get("/api/activity")
        data = self.client.get("/api/activity").get_json()
        calls = self.task().log.get.call_args_list
        self.assertEqual(calls[1].kwargs["start"], 1)
        # No new lines: the last known progress is kept.
        self.assertEqual(data["items"][0]["progress"], 10)

        # Once finished, the cached offset is dropped.
        running["endtime"] = 2
        running["status"] = "OK"
        self.client.get("/api/activity")
        self.assertNotIn(MIGRATE_UPID, app._task_progress_cache)

    def test_console_tasks_are_not_tailed(self):
        self.conn.cluster.tasks.get.return_value = [
            {
                "upid": "UPID:pve1:1:1:6730A000:vncproxy:100:root@pam:",
                "node": "pve1",
                "type": "vncproxy",
                "starttime": 1,
            }
        ]
        self.client.get("/api/activity")
        self.task().log.get.assert_not_called()

    def test_jobs_still_listed_when_proxmox_fails(self):
        job_id = self.make_job()
        self.conn.cluster.tasks.get.side_effect = Exception("boom")
        data = self.client.get("/api/activity").get_json()
        self.assertIn("boom", data["error"])
        self.assertEqual([i["id"] for i in data["items"]], [job_id])


class TestActivityItem(ActivityTestBase):
    def test_task_detail_with_log(self):
        self.task().status.get.return_value = {
            "status": "stopped",
            "exitstatus": "OK",
            "type": "qmigrate",
            "id": "100",
        }
        self.conn.cluster.tasks.get.return_value = [
            {
                "upid": MIGRATE_UPID,
                "node": "pve1",
                "type": "qmigrate",
                "id": "100",
                "starttime": 10,
                "endtime": 70,
                "status": "OK",
            }
        ]
        self.task().log.get.return_value = [
            {"n": 1, "t": "starting migration"},
            {"n": 2, "t": "migration finished successfully"},
        ]
        data = self.client.get(f"/api/activity/{MIGRATE_UPID}").get_json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["ended_at"], 70)
        self.assertEqual(
            data["log"], ["starting migration", "migration finished successfully"]
        )
        self.assertFalse(data["log_truncated"])

    def test_running_task_detail_shows_progress(self):
        self.task().status.get.return_value = {"status": "running", "type": "qmigrate"}
        self.conn.cluster.tasks.get.return_value = []
        self.task().log.get.return_value = [
            {
                "n": 1,
                "t": "mirror-scsi0: transferred 1.4 GiB of 60.0 GiB (2.41%) in 5s",
            },
            {
                "n": 2,
                "t": "mirror-scsi0: transferred 1.7 GiB of 60.0 GiB (2.87%) in 6s",
            },
        ]
        data = self.client.get(f"/api/activity/{MIGRATE_UPID}").get_json()
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["progress"], 3)

    def test_finished_task_missing_from_cluster_list(self):
        self.task().status.get.return_value = {
            "status": "stopped",
            "exitstatus": "migration problems",
        }
        self.conn.cluster.tasks.get.return_value = []
        self.task().log.get.return_value = []
        data = self.client.get(f"/api/activity/{MIGRATE_UPID}").get_json()
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["status_text"], "migration problems")

    def test_invalid_upid(self):
        r = self.client.get("/api/activity/UPID:bad")
        self.assertEqual(r.status_code, 400)

    def test_job_detail(self):
        job_id = self.make_job([CLONE_UPID])
        app.job_queue.add_step(job_id, "Cloning disk…")
        self.conn.cluster.tasks.get.return_value = []
        data = self.client.get(f"/api/activity/{job_id}").get_json()
        self.assertEqual(data["source"], "proxui")
        self.assertEqual(len(data["steps"]), 1)
        self.assertEqual(data["children"][0]["id"], CLONE_UPID)
        self.assertEqual(data["children"][0]["status"], "unknown")
        self.assertEqual(data["children"][0]["type"], "qmclone")

    def test_unknown_job(self):
        self.assertEqual(self.client.get("/api/activity/nope").status_code, 404)

    def test_detail_page_renders(self):
        r = self.client.get(f"/tasks/{MIGRATE_UPID}")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"taskDetail", r.data)


class TestJobUpids(unittest.TestCase):
    def setUp(self):
        self.job_id = app.job_queue.create_job("t", "t", {})

    def tearDown(self):
        app.job_queue.delete_job(self.job_id)

    @patch("app.time.sleep")
    def test_wait_for_task_links_upid(self, _sleep):
        proxmox = MagicMock()
        proxmox.nodes.return_value.tasks.return_value.status.get.return_value = {
            "status": "stopped",
            "exitstatus": "OK",
        }
        app.wait_for_task(proxmox, "pve1", CLONE_UPID, self.job_id)
        app.wait_for_task(proxmox, "pve1", CLONE_UPID, self.job_id)
        self.assertEqual(
            app.job_queue.get_job(self.job_id)["upids"],
            [{"node": "pve1", "upid": CLONE_UPID}],
        )


if __name__ == "__main__":
    unittest.main()
