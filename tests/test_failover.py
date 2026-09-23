"""Tests for routing around a Proxmox node that stops answering."""

import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app

# The verbatim shape proxmoxer raises when a node is unreachable. The
# /api2/json/access/ticket URL matters: it contains the word "ticket", which a
# naive auth check matches on, and an earlier version of these tests used a
# trimmed message that hid exactly that bug.
TIMEOUT = Exception(
    "HTTPSConnectionPool(host='10.0.0.1', port=8006): Max retries exceeded "
    "with url: /api2/json/access/ticket "
    "(Caused by ConnectTimeoutError(<urllib3.connection.HTTPSConnection "
    "object at 0x7f00>, 'Connection to 10.0.0.1 timed out. "
    "(connect timeout=30)'))"
)

REFUSED = Exception(
    "HTTPSConnectionPool(host='10.0.0.1', port=8006): Max retries exceeded "
    "with url: /api2/json/access/ticket (Caused by NewConnectionError("
    "'<urllib3.connection.HTTPSConnection object>: Failed to establish a new "
    "connection: [Errno 111] Connection refused'))"
)


def dead_connection():
    conn = Mock()
    conn.version.get.side_effect = TIMEOUT
    return conn


def live_connection(version="8.2.2"):
    conn = Mock()
    conn.version.get.return_value = {"version": version}
    conn.cluster.status.get.return_value = []
    return conn


class FailoverTestCase(unittest.TestCase):
    def setUp(self):
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.connection_metadata.clear()
        app.node_cluster_ips.clear()
        app.all_clusters.clear()
        app._last_failover_attempt = 0.0
        self._saved_cluster = app.current_cluster_id
        app.current_cluster_id = "c1"
        app.all_clusters["c1"] = {
            "id": "c1",
            "name": "C1",
            "nodes": [
                {"host": "10.0.0.1", "user": "root@pam", "password": "pw"},
                {"host": "10.0.0.2", "user": "root@pam", "password": "pw"},
            ],
        }

    def tearDown(self):
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.connection_metadata.clear()
        app.node_cluster_ips.clear()
        app.all_clusters.clear()
        app._last_failover_attempt = 0.0
        app.current_cluster_id = self._saved_cluster

    def _seed(self, conn, hosts=("kilo", "node1", "node2"), host="10.0.0.1"):
        meta = {"host": host, "user": "root@pam", "password": "pw"}
        for name in hosts:
            app.proxmox_nodes[name] = conn
            app.connection_metadata[name] = meta
            app.cluster_nodes.append(
                {"name": name, "status": "online", "connection": conn}
            )
        app.connection_metadata[host] = meta

    def test_falls_over_to_another_configured_host(self):
        dead, live = dead_connection(), live_connection()
        self._seed(dead)
        with patch.object(app, "create_proxmox_connection", return_value=live) as make:
            got = app.get_proxmox_connection("node1")
        self.assertIs(got, live)
        self.assertEqual(make.call_args.args[0]["host"], "10.0.0.2")

    def test_every_node_moves_off_the_dead_connection(self):
        dead, live = dead_connection(), live_connection()
        self._seed(dead)
        with patch.object(app, "create_proxmox_connection", return_value=live):
            app.get_proxmox_connection("node1")
        # The whole cluster was reached through the dead host, so all of it moves.
        for name in ("kilo", "node1", "node2"):
            self.assertIs(app.proxmox_nodes[name], live, name)
        for node_info in app.cluster_nodes:
            self.assertIs(node_info["connection"], live, node_info["name"])

    def test_prefers_a_live_connection_already_in_the_pool(self):
        dead, live = dead_connection(), live_connection()
        self._seed(dead, hosts=("kilo", "node1"))
        app.proxmox_nodes["node2"] = live
        with patch.object(app, "create_proxmox_connection") as make:
            got = app.get_proxmox_connection("node1")
        self.assertIs(got, live)
        make.assert_not_called()

    def test_uses_a_discovered_node_address_when_configured_hosts_are_down(self):
        dead, live = dead_connection(), live_connection()
        app.all_clusters["c1"]["nodes"] = [
            {"host": "10.0.0.1", "user": "root@pam", "password": "pw"}
        ]
        self._seed(dead)
        app.node_cluster_ips.update({"kilo": "10.0.0.1", "node1": "10.0.0.9"})

        def connect(cfg, timeout=None):
            if cfg["host"] == "10.0.0.9":
                return live
            raise TIMEOUT

        with patch.object(app, "create_proxmox_connection", side_effect=connect):
            got = app.get_proxmox_connection("node1")
        self.assertIs(got, live)

    def test_credentials_are_reused_for_a_sibling_address(self):
        dead, live = dead_connection(), live_connection()
        app.all_clusters["c1"]["nodes"] = [
            {"host": "10.0.0.1", "user": "root@pam", "password": "pw"}
        ]
        self._seed(dead)
        app.node_cluster_ips.update({"node1": "10.0.0.9"})
        with patch.object(app, "create_proxmox_connection", return_value=live) as make:
            app.get_proxmox_connection("node1")
        cfg = make.call_args.args[0]
        self.assertEqual(cfg["host"], "10.0.0.9")
        self.assertEqual(cfg["user"], "root@pam")
        self.assertEqual(cfg["password"], "pw")

    def test_returns_the_original_when_nothing_is_reachable(self):
        dead = dead_connection()
        self._seed(dead)
        with patch.object(app, "create_proxmox_connection", side_effect=TIMEOUT):
            got = app.get_proxmox_connection("node1")
        # Handing back the dead handle lets the caller surface the transport
        # error instead of a misleading "node not found".
        self.assertIs(got, dead)

    def test_a_failed_sweep_is_not_repeated_for_every_request(self):
        dead = dead_connection()
        self._seed(dead)
        with patch.object(
            app, "create_proxmox_connection", side_effect=TIMEOUT
        ) as make:
            for _ in range(4):
                app.get_proxmox_connection("node1")
        # One probe round for the window, not one per request.
        self.assertEqual(make.call_count, 1)

    def test_a_successful_failover_does_not_leave_the_throttle_armed(self):
        dead, live = dead_connection(), live_connection()
        self._seed(dead)
        with patch.object(app, "create_proxmox_connection", return_value=live):
            app.get_proxmox_connection("node1")
        self.assertEqual(app._last_failover_attempt, 0.0)

    def test_auth_errors_still_renew_against_the_same_host(self):
        conn = Mock()
        conn.version.get.side_effect = Exception("401 Unauthorized")
        self._seed(conn)
        renewed = live_connection()
        with patch.object(app, "renew_proxmox_connection", return_value=renewed) as ren:
            with patch.object(app, "create_proxmox_connection") as make:
                got = app.get_proxmox_connection("node1")
        self.assertIs(got, renewed)
        ren.assert_called_once_with("node1")
        make.assert_not_called()

    def test_unreachable_node_is_not_mistaken_for_an_auth_failure(self):
        # proxmoxer re-authenticates against /api2/json/access/ticket, so an
        # unreachable node's error mentions "ticket". Treating that as an auth
        # failure renews against the same dead host and never fails over.
        for err in (TIMEOUT, REFUSED):
            with self.subTest(err=str(err)[:40]):
                self.assertTrue(app.is_connection_error(err))
                self.assertFalse(app.is_authentication_error(err))

    def test_real_outage_message_still_fails_over(self):
        dead, live = dead_connection(), live_connection()
        self._seed(dead)
        with patch.object(app, "renew_proxmox_connection") as renew:
            with patch.object(app, "create_proxmox_connection", return_value=live):
                got = app.get_proxmox_connection("node1")
        self.assertIs(got, live)
        renew.assert_not_called()

    def test_connection_refused_also_fails_over(self):
        dead, live = dead_connection(), live_connection()
        dead.version.get.side_effect = REFUSED
        self._seed(dead)
        with patch.object(app, "create_proxmox_connection", return_value=live):
            got = app.get_proxmox_connection("node1")
        self.assertIs(got, live)

    def test_failed_renewal_falls_back_to_failover(self):
        conn = Mock()
        conn.version.get.side_effect = Exception("401 Unauthorized")
        self._seed(conn)
        live = live_connection()
        with patch.object(app, "renew_proxmox_connection", return_value=None):
            with patch.object(app, "create_proxmox_connection", return_value=live):
                got = app.get_proxmox_connection("node1")
        self.assertIs(got, live)

    def test_genuine_auth_errors_are_still_auth_errors(self):
        for msg in (
            "401 Unauthorized",
            "authentication failed",
            "Couldn't authenticate user: root@pam",
            "invalid ticket",
        ):
            with self.subTest(msg=msg):
                self.assertTrue(app.is_authentication_error(Exception(msg)))
                self.assertFalse(app.is_connection_error(Exception(msg)))

    def test_healthy_connection_is_returned_untouched(self):
        live = live_connection()
        self._seed(live)
        with patch.object(app, "create_proxmox_connection") as make:
            got = app.get_proxmox_connection("node1")
        self.assertIs(got, live)
        make.assert_not_called()


if __name__ == "__main__":
    unittest.main()
