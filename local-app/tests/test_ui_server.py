"""Local UI API on a synthetic workspace: guards, intake round trip, version conflicts."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from hrshturman_local import config, ui_server


class UiServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        settings = config.Settings(Path(self.tmp.name), "synthetic-ui", "Europe/Moscow", config.DEFAULT_LIBRARY)
        self.server = ui_server.build_server(settings, port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, path, body=None, host=None, content_type="application/json"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": content_type})
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_page_served(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as res:
            self.assertIn("JobShturman", res.read().decode())

    def test_foreign_host_and_form_posts_refused(self):
        self.assertEqual(self.call("/api/status", host="evil.example")[0], 403)
        self.assertEqual(self.call("/api/pause", {"paused": True}, content_type="text/plain")[0], 415)

    def test_intake_round_trip_and_conflict(self):
        status, draft = self.call("/api/intake", {"pain_ids": [2, 7]})
        self.assertEqual(status, 200)
        self.assertEqual(self.call("/api/intake/confirm", {"expected_version": draft["version"]})[1]["status"], "confirmed")
        status, err = self.call("/api/intake", {"pain_ids": [1], "expected_version": draft["version"]})
        self.assertEqual((status, err["error"]), (409, "state_version_conflict"))
        self.assertEqual(self.call("/api/status")[1]["intake"], "confirmed")

    def test_bad_request_and_unknown_route(self):
        self.assertEqual(self.call("/api/intake/confirm", {})[0], 400)
        self.assertEqual(self.call("/api/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()
