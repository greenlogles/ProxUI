"""Tests for the browser terminal (termproxy) endpoints.

The websocket relay itself (/shell-ws/<session_id>) is not covered here — it
needs a live PVE node and a real websocket upgrade. These tests cover the
ticket endpoints: their response shape, the API-token rejection path, the
QEMU no-serial-port path, and that PVE tickets never leave the server.
"""

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app

SECRET_TICKET = "PVEVNC:SECRETTICKETVALUE"


def _termproxy_response():
    resp = Mock()
    resp.status_code = 200
    resp.json.return_value = {
        "data": {
            "ticket": SECRET_TICKET,
            "port": 5900,
            "user": "root@pam",
            "upid": "UPID:pve-a:0000:termproxy::root@pam:",
        }
    }
    resp.raise_for_status.return_value = None
    return resp


class ShellTestCase(unittest.TestCase):
    def setUp(self):
        self.flask_app = app.app
        self.flask_app.config["TESTING"] = True
        self.client = self.flask_app.test_client()

        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        with app.shell_sessions_lock:
            app.shell_sessions.clear()

        app.all_clusters["cluster-a"] = {"id": "cluster-a", "name": "A", "nodes": []}
        app.current_cluster_id = "cluster-a"

        self.proxmox = Mock()
        app.proxmox_nodes["pve-a"] = self.proxmox
        app.cluster_nodes.append({"name": "pve-a", "status": "online"})
        app.connection_metadata["pve-a"] = {
            "host": "10.0.0.10",
            "user": "root@pam",
            "password": "hunter2",
            "verify_ssl": False,
        }

    def tearDown(self):
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()
        app.all_clusters.clear()
        app.connection_metadata.clear()
        app.current_cluster_id = None
        with app.shell_sessions_lock:
            app.shell_sessions.clear()

    def _use_token_auth(self):
        app.connection_metadata["pve-a"] = {
            "host": "10.0.0.10",
            "user": "svc@pve",
            "token_name": "proxui",
            "token_value": "0000-1111",
            "verify_ssl": False,
        }


class TestNodeShellTicket(ShellTestCase):
    @patch("app._pve_login_ticket")
    @patch("app.requests.post")
    def test_returns_session_id_and_websocket_url(self, mock_post, mock_login):
        mock_login.return_value = ("10.0.0.10", "LOGINTICKET", "CSRF", False)
        mock_post.return_value = _termproxy_response()

        r = self.client.post("/api/node/pve-a/shell-ticket")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["websocket_url"], f"/shell-ws/{data['session_id']}")

        url = mock_post.call_args[0][0]
        self.assertEqual(url, "https://10.0.0.10:8006/api2/json/nodes/pve-a/termproxy")

    @patch("app._pve_login_ticket")
    @patch("app.requests.post")
    def test_ticket_is_stored_server_side_only(self, mock_post, mock_login):
        mock_login.return_value = ("10.0.0.10", "LOGINTICKET", "CSRF", False)
        mock_post.return_value = _termproxy_response()

        buf = io.StringIO()
        with redirect_stdout(buf):
            r = self.client.post("/api/node/pve-a/shell-ticket")

        body = r.get_data(as_text=True)
        self.assertNotIn(SECRET_TICKET, body)
        self.assertNotIn("LOGINTICKET", body)
        self.assertNotIn(SECRET_TICKET, buf.getvalue())
        self.assertNotIn("LOGINTICKET", buf.getvalue())

        session_id = r.get_json()["session_id"]
        with app.shell_sessions_lock:
            stored = app.shell_sessions[session_id]
        self.assertEqual(stored["ticket"], SECRET_TICKET)
        self.assertEqual(stored["port"], 5900)
        self.assertEqual(stored["ws_path"], "/nodes/pve-a/vncwebsocket")

    def test_no_connection_is_404(self):
        app.proxmox_nodes.clear()
        app.cluster_nodes.clear()

        r = self.client.post("/api/node/pve-a/shell-ticket")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"], "Node not found")

    @patch("app.requests.post")
    def test_node_without_connection_metadata_is_reported(self, mock_post):
        # get_proxmox_connection falls back to any live connection, so an
        # unknown node only fails once we look for its host.
        r = self.client.post("/api/node/nope/shell-ticket")
        self.assertEqual(r.status_code, 400)
        self.assertIn("No connection host known", r.get_json()["error"])
        mock_post.assert_not_called()

    @patch("app.requests.post")
    def test_api_token_auth_is_rejected_with_explanation(self, mock_post):
        self._use_token_auth()

        r = self.client.post("/api/node/pve-a/shell-ticket")
        self.assertEqual(r.status_code, 400)
        data = r.get_json()
        self.assertTrue(data["token_auth"])
        self.assertIn("API token", data["error"])
        self.assertIn("username/password", data["error"])
        mock_post.assert_not_called()
        with app.shell_sessions_lock:
            self.assertEqual(app.shell_sessions, {})

    @patch("app._pve_login_ticket")
    @patch("app.requests.post")
    def test_forbidden_becomes_privilege_message(self, mock_post, mock_login):
        mock_login.return_value = ("10.0.0.10", "LOGINTICKET", "CSRF", False)
        forbidden = Mock()
        forbidden.status_code = 403
        mock_post.return_value = forbidden

        r = self.client.post("/api/node/pve-a/shell-ticket")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Sys.Console", r.get_json()["error"])


class TestGuestShellTicket(ShellTestCase):
    @patch("app._pve_login_ticket")
    @patch("app.requests.post")
    def test_qemu_with_serial_port_passes_serial(self, mock_post, mock_login):
        mock_login.return_value = ("10.0.0.10", "LOGINTICKET", "CSRF", False)
        mock_post.return_value = _termproxy_response()
        self.proxmox.nodes.return_value.qemu.return_value.config.get.return_value = {
            "name": "vm1",
            "serial0": "socket",
        }

        r = self.client.post("/api/vm/pve-a/101/shell-ticket")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            mock_post.call_args[0][0],
            "https://10.0.0.10:8006/api2/json/nodes/pve-a/qemu/101/termproxy",
        )
        self.assertEqual(mock_post.call_args.kwargs["data"], {"serial": "serial0"})

        session_id = r.get_json()["session_id"]
        with app.shell_sessions_lock:
            self.assertEqual(
                app.shell_sessions[session_id]["ws_path"],
                "/nodes/pve-a/qemu/101/vncwebsocket",
            )

    @patch("app.requests.post")
    def test_qemu_without_serial_port_is_rejected(self, mock_post):
        self.proxmox.nodes.return_value.qemu.return_value.config.get.return_value = {
            "name": "vm1"
        }

        r = self.client.post("/api/vm/pve-a/101/shell-ticket")
        self.assertEqual(r.status_code, 400)
        data = r.get_json()
        self.assertTrue(data["no_serial"])
        self.assertIn("serial", data["error"].lower())
        mock_post.assert_not_called()

    @patch("app._pve_login_ticket")
    @patch("app.requests.post")
    def test_lxc_uses_the_lxc_termproxy_path(self, mock_post, mock_login):
        mock_login.return_value = ("10.0.0.10", "LOGINTICKET", "CSRF", False)
        mock_post.return_value = _termproxy_response()
        self.proxmox.nodes.return_value.qemu.return_value.status.current.get.side_effect = Exception(
            "not a VM"
        )

        r = self.client.post("/api/vm/pve-a/200/shell-ticket")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            mock_post.call_args[0][0],
            "https://10.0.0.10:8006/api2/json/nodes/pve-a/lxc/200/termproxy",
        )
        self.assertEqual(mock_post.call_args.kwargs["data"], {})


class TestShellPages(ShellTestCase):
    def test_node_shell_page_renders_without_ticket(self):
        r = self.client.get("/node/pve-a/shell")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("shellToolbar", body)
        self.assertIn("/api/node/pve-a/shell-ticket", body)
        self.assertIn("xterm", body)
        self.assertNotIn(SECRET_TICKET, body)
        self.assertNotIn("hunter2", body)

    def test_node_shell_page_flags_token_auth(self):
        self._use_token_auth()
        r = self.client.get("/node/pve-a/shell")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("const tokenAuth = true;", body)
        self.assertNotIn("0000-1111", body)

    def test_guest_shell_page_renders(self):
        self.proxmox.nodes.return_value.qemu.return_value.config.get.return_value = {
            "name": "vm1",
            "serial0": "socket",
        }
        r = self.client.get("/vm/pve-a/101/shell")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("/api/vm/pve-a/101/shell-ticket", body)
        self.assertIn("Serial console", body)


class TestShellSessionCleanup(ShellTestCase):
    def test_expired_sessions_are_dropped(self):
        with app.shell_sessions_lock:
            app.shell_sessions["old"] = {"created_at": 0}
            app.shell_sessions["new"] = {"created_at": app.time.time()}

        app.cleanup_expired_shell_sessions()

        with app.shell_sessions_lock:
            self.assertNotIn("old", app.shell_sessions)
            self.assertIn("new", app.shell_sessions)


if __name__ == "__main__":
    unittest.main()
