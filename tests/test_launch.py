import argparse
import contextlib
import http.client
import io
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from local_computer_use.__main__ import prune_history, read_config
from local_computer_use.model_process import ManagedModel
from local_computer_use.orchestrator import Job, Run
from local_computer_use.store import Store


PROBE = """#!/usr/bin/env python3
import http.server
import sys
from pathlib import Path

def flag(name):
    return sys.argv[sys.argv.index(name) + 1]

key = Path(flag("--api-key-file")).read_text().strip().encode()
alias = flag("--alias")

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Authorization", "").encode() != b"Bearer " + key:
            self.send_error(401)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(('{"data":[{"id":"' + alias + '"}]}').encode()
                         if self.path == "/v1/models" else b'{"status":"ok"}')

http.server.HTTPServer(("127.0.0.1", int(flag("--port"))), Handler).serve_forever()
"""


class LauncherTest(unittest.TestCase):
    def test_managed_process_is_authenticated_private_and_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "fixture-server"
            fake.write_text(PROBE)
            fake.chmod(0o700)
            artifact = root / "fixture.gguf"
            artifact.write_bytes(b"GGUF" + b"\x00" * 16)
            with ManagedModel(str(fake), artifact, artifact, slots=2, context_per_slot=4096,
                              gpu_layers=0, threads=2) as managed:
                connection = http.client.HTTPConnection("127.0.0.1", managed.port, timeout=3)
                try:
                    connection.request("GET", "/v1/models")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 401)
                    response.read()
                    self.assertEqual(managed._get("/v1/models")["data"][0]["id"], managed.alias)
                finally:
                    connection.close()
            self.assertIsNotNone(managed.process.poll())

    def test_configuration_checks_shape_and_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.json"
            config.write_text('{"applications":{"calc":{"label":"Calculator","argv":["/usr/bin/gnome-calculator"]}}}')
            self.assertEqual(read_config(config)["applications"]["calc"]["label"], "Calculator")
            config.write_text('{"local_models":{"missing-identifier":{"url":"http://127.0.0.1:3"}}}')
            with self.assertRaises(ValueError):
                read_config(config)

    def test_prune_only_removes_old_finished_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_id, job_id = uuid4().hex, uuid4().hex
            old = Run(old_id, "Old", "2000-01-01T00:00:00+00:00",
                      [Job(job_id, old_id, "A", "A", "local", "editor", "completed")], status="completed")
            active_id = uuid4().hex
            active = Run(active_id, "Active", old.created_at, [], status="running")
            store = Store(root)
            store.save(asdict(old))
            store.save(asdict(active))
            store.close()
            workspace = root / "work" / job_id
            workspace.mkdir(parents=True)
            (workspace / "artifact.txt").write_text("fixture")
            profile = root / "browser-profiles" / "known"
            profile.mkdir(parents=True)
            args = argparse.Namespace(state_dir=root, older_than_days=30, dry_run=True)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                prune_history(args)
            self.assertEqual(json.loads(output.getvalue()), {"matched": 1})
            self.assertTrue(workspace.exists())
            with contextlib.redirect_stdout(io.StringIO()) as output:
                args.dry_run = False
                prune_history(args)
            self.assertEqual(json.loads(output.getvalue()), {"removed": 1})
            self.assertFalse(workspace.exists())
            self.assertTrue(profile.exists())
            store = Store(root)
            try:
                self.assertEqual([run["id"] for run in store.load()], [active_id])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
