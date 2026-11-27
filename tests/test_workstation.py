"""Run opt-in integration checks with actual applications and no physical input."""

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from parallel_computer_use.desktop import DesktopSession
from parallel_computer_use.orchestrator import Orchestrator, TERMINAL


class CatalogFixture:
    is_cloud = False
    supports_images = True

    def decide(self, prompt, image, cancel):
        if not image or not image.startswith(b"\x89PNG"):
            raise AssertionError("A real desktop frame is required")
        if cancel.is_set():
            return {"type": "wait"}
        if "Worker: Notebook search\n" in prompt:
            query, result = "Notebook", "Elm Notebook, $8; Cedar Notebook, $11"
        elif "Worker: Mug search\n" in prompt:
            query, result = "mug", "Cedar Mug, $16"
        else:
            # The last worker has both results available before editing.
            self.assert_inputs(prompt)
            if "Entered text." not in prompt:
                return {"type": "type", "text": "Elm Notebook | $8\nCedar Mug | $16\n"}
            if prompt.count("Pressed a key.") == 0:
                return {"type": "key", "key": "ctrl+o"}
            if prompt.count("Pressed a key.") == 1:
                return {"type": "key", "key": "Return"}
            return {"type": "done", "result": "Saved both recommendations to notes.txt."}
        if "Entered text." not in prompt:
            return {"type": "type", "text": query}
        return {"type": "done", "result": result}

    @staticmethod
    def assert_inputs(prompt):
        if "Elm Notebook, $8" not in prompt or "Cedar Mug, $16" not in prompt:
            raise AssertionError("Coordinator dependency results did not reach the editor")


@unittest.skipUnless(os.environ.get("PCU_TEST_DESKTOP") == "1", "opt in with PCU_TEST_DESKTOP=1")
class WorkstationTest(unittest.TestCase):
    def wait_until(self, check, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if result := check():
                return result
            time.sleep(0.075)
        self.fail("Desktop operation failed to reach the expected state")

    def test_independent_native_sessions_and_typing(self):
        with DesktopSession(width=640, height=480) as first:
            with DesktopSession(width=640, height=480) as second:
                baseline_second = hashlib.sha256(second.png()).digest()
                before = second.observation()
                self.assertNotEqual(first.display, second.display)
                first.apply({"type": "click", "x": 24, "y": 30})
                first.apply({"type": "type", "text": "isolated native fixture"})
                first.apply({"type": "key", "key": "ctrl+o"})
                first.apply({"type": "key", "key": "Return"})
                self.wait_until(lambda: (first.workspace / "notes.txt").stat().st_size)
                self.assertIn("isolated native fixture", (first.workspace / "notes.txt").read_text())
                self.assertEqual((second.workspace / "notes.txt").read_bytes(), b"")
                self.assertEqual(second.observation(), before)
                self.assertEqual(hashlib.sha256(second.png()).digest(), baseline_second)

    def test_three_worker_catalog_and_native_export(self):
        with tempfile.TemporaryDirectory() as state:
            app = Orchestrator(Path(state),
                               lambda template, workspace: DesktopSession(template, workspace=workspace,
                                                                          width=800, height=600),
                               {"local": CatalogFixture()}, sessions=3, inference=2, memory_floor=0)
            try:
                run_id = app.start_run({
                    "prompt": "Build a catalog shortlist.",
                    "assignments": [
                        {"title": "Notebook search", "prompt": "Filter notebook listings.", "template": "browser"},
                        {"title": "Mug search", "prompt": "Filter mug listings.", "template": "browser"},
                        {"title": "Shortlist", "prompt": "Save the shortlist.", "template": "editor",
                         "depends_on": [0, 1]},
                    ],
                })
                jobs = app.runs[run_id].jobs
                self.wait_until(lambda: all(job.status in TERMINAL for job in jobs))
                state = app.state()[1]
                self.assertEqual([job.status for job in jobs], ["completed"] * 3, json.dumps(state))
                exported = app.artifact(jobs[2].id, "notes.txt").decode()
                self.assertIn("Elm Notebook | $8", exported)
                self.assertIn("Cedar Mug | $16", exported)
                self.assertEqual(app.state()[1]["metrics"]["active_sessions"], 0)
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
