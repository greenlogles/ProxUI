#!/usr/bin/env python3
"""
Tests for application-level security hardening.
"""

import base64
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


class TestSecurityHardening(unittest.TestCase):
    def restore_config(self, originals):
        for key, (present, value) in originals.items():
            if present:
                app.app.config[key] = value
            else:
                app.app.config.pop(key, None)

    def test_download_url_and_filename_validation(self):
        self.assertTrue(app.is_allowed_download_url("https://example.com/image.iso"))
        self.assertFalse(app.is_allowed_download_url("file:///etc/passwd"))
        self.assertFalse(
            app.is_allowed_download_url("https://user:pass@example.com/image.iso")
        )
        self.assertFalse(app.is_allowed_download_url("javascript:alert(1)"))

        self.assertTrue(app.is_safe_download_filename("ubuntu-24.04.iso", (".iso",)))
        self.assertTrue(
            app.is_safe_download_filename("debian-12.tar.zst", (".tar.zst",))
        )
        self.assertFalse(app.is_safe_download_filename("../secret.iso", (".iso",)))
        self.assertFalse(app.is_safe_download_filename("nested/path.iso", (".iso",)))
        self.assertFalse(app.is_safe_download_filename("image.raw", (".iso",)))

    def test_security_headers_are_set(self):
        client = app.app.test_client()

        response = client.get("/connect")

        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "same-origin")

    def test_csrf_rejects_mutating_requests_when_enabled(self):
        originals = {
            key: (key in app.app.config, app.app.config.get(key))
            for key in ("TESTING", "WTF_CSRF_ENABLED", "ENABLE_CSRF_IN_TESTS")
        }

        app.app.config["TESTING"] = True
        app.app.config["WTF_CSRF_ENABLED"] = True
        app.app.config["ENABLE_CSRF_IN_TESTS"] = True

        client = app.app.test_client()

        try:
            response = client.post("/api/switch-cluster/missing")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["error"], "Missing CSRF token")
        finally:
            self.restore_config(originals)

    def test_csrf_accepts_header_token_when_enabled(self):
        originals = {
            key: (key in app.app.config, app.app.config.get(key))
            for key in ("TESTING", "WTF_CSRF_ENABLED", "ENABLE_CSRF_IN_TESTS")
        }
        original_clusters = app.all_clusters.copy()
        original_cluster_id = app.current_cluster_id

        app.app.config["TESTING"] = True
        app.app.config["WTF_CSRF_ENABLED"] = True
        app.app.config["ENABLE_CSRF_IN_TESTS"] = True
        app.all_clusters["test"] = {"id": "test", "name": "Test", "nodes": []}
        app.current_cluster_id = "test"

        client = app.app.test_client()

        try:
            with client.session_transaction() as sess:
                sess["_csrf_token"] = "known-token"

            response = client.post(
                "/api/switch-cluster/missing",
                headers={"X-CSRF-Token": "known-token"},
            )
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.get_json()["error"], "Cluster not found")
        finally:
            self.restore_config(originals)
            app.all_clusters.clear()
            app.all_clusters.update(original_clusters)
            app.current_cluster_id = original_cluster_id

    def test_optional_basic_auth(self):
        original_clusters = app.all_clusters.copy()
        original_cluster_id = app.current_cluster_id

        app.all_clusters["test"] = {"id": "test", "name": "Test", "nodes": []}
        app.current_cluster_id = "test"

        env = {
            "PROXUI_AUTH_USERNAME": "admin",
            "PROXUI_AUTH_PASSWORD": "secret",
        }
        credentials = base64.b64encode(b"admin:secret").decode()
        client = app.app.test_client()

        try:
            with patch.dict(os.environ, env):
                response = client.get("/api/clusters")
                self.assertEqual(response.status_code, 401)
                self.assertIn("Basic", response.headers["WWW-Authenticate"])

                response = client.get(
                    "/api/clusters", headers={"Authorization": f"Basic {credentials}"}
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["current"], "test")
        finally:
            app.all_clusters.clear()
            app.all_clusters.update(original_clusters)
            app.current_cluster_id = original_cluster_id


if __name__ == "__main__":
    unittest.main()
