import tempfile
import threading
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from local_computer_use.orchestrator import Job, Orchestrator, RequestError, Run, TERMINAL
from local_computer_use.store import Store


class FakeDesktop:
    width, height = 960, 640

    def __init__(self, template, workspace):
        self.template = template
        self.workspace = workspace
        self.actions = []
        self.closed = False
        self.releases = 0
        self.focus = 27

    def observation(self):
        return {"width": self.width, "height": self.height, "focus": self.focus,
                "active_window": self.focus}

    def png(self):
        return b"frame:" + str(len(self.actions)).encode()

    def apply(self, action):
        self.actions.append(action)
        if action.get("key") == "ctrl+s":
            (self.workspace / "notes.txt").write_text("export")

    def close(self):
        self.closed = True

    def release_held(self):
        self.releases += 1


class GateProvider:
    supports_images = True
    is_cloud = False

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.prompts = []
        self.images = []

    def decide(self, prompt, image, cancel):
        self.prompts.append(prompt)
        self.images.append(image)
        self.entered.set()
        while not self.release.is_set():
            if cancel.wait(0.02):
                self.cancelled.set()
                return {"type": "click", "x": 7, "y": 9}
        return {"type": "done", "result": "fixture finished"}


class PlannerProvider:
    supports_images = True
    is_cloud = False

    def __init__(self):
        self.calls = []

    def decide(self, prompt, image, cancel):
        self.calls.append((prompt, image))
        if image is None:
            return {"type": "plan", "jobs": [
                {"title": "Gather", "prompt": "collect results"},
                {"title": "Compare", "prompt": "compare gathered results", "depends_on": [0]},
            ]}
        return {"type": "done", "result": "verified outcome"}


class OrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.desktops = []
        self.app = None

    def build(self, providers, **kwargs):
        def desktop(template, workspace, **options):
            item = FakeDesktop(template, workspace)
            item.options = options
            self.desktops.append(item)
            return item

        self.app = Orchestrator(Path(self.temp.name), desktop, providers, memory_floor=0, **kwargs)
        self.addCleanup(self.app.close)
        return self.app

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if result := condition():
                return result
            time.sleep(0.015)
        self.fail("Condition did not happen before the deadline")

    def test_session_and_inference_limits_are_independent(self):
        provider = GateProvider()
        app = self.build({"local": provider}, sessions=2, inference=1)
        run = app.start_run({"prompt": "fixture", "count": 3, "mode": "manual"})
        self.assertTrue(provider.entered.wait(3))
        self.wait_for(lambda: len(self.desktops) == 2)
        metrics = app.state()[1]["metrics"]
        self.assertEqual(metrics, {"active_sessions": 2, "active_inferences": 1, "queued": 1})
        provider.release.set()
        self.wait_for(lambda: all(item.status in TERMINAL for item in app.runs[run].jobs))
        self.assertEqual(len(self.desktops), 3)
        self.assertTrue(all(desktop.closed for desktop in self.desktops))
        self.assertIn("3 of 3", app.runs[run].summary)

    def test_manual_handoff_rejects_late_model_and_user_input(self):
        provider = GateProvider()
        app = self.build({"local": provider})
        run = app.start_run({"prompt": "fixture"})
        job = app.runs[run].jobs[0]
        self.assertTrue(provider.entered.wait(3))
        initial = job.lease
        takeover = app.command(job.id, {"command": "takeover"})
        self.assertTrue(provider.cancelled.wait(2))
        self.assertEqual(takeover["owner"], "user")
        with self.assertRaises(RequestError) as error:
            app.command(job.id, {"command": "input", "lease": initial,
                                 "action": {"type": "click", "x": 7, "y": 9}})
        self.assertEqual(error.exception.status, 409)
        app.command(job.id, {"command": "input", "lease": takeover["lease"],
                             "action": {"type": "key", "key": "Return"}})
        app.command(job.id, {"command": "return"})
        provider.release.set()
        self.wait_for(lambda: job.status == "completed")
        self.assertEqual(self.desktops[0].actions, [{"type": "key", "key": "Return"}])
        self.assertGreaterEqual(self.desktops[0].releases, 3)

    def test_stale_user_cleanup_cannot_pause_new_owner(self):
        provider = GateProvider()
        app = self.build({"local": provider})
        run = app.start_run({"prompt": "fixture"})
        self.assertTrue(provider.entered.wait(3))
        job = app.runs[run].jobs[0]
        first = app.command(job.id, {"command": "takeover"})
        second = app.command(job.id, {"command": "takeover", "lease": first["lease"]})
        with self.assertRaises(RequestError):
            app.command(job.id, {"command": "pause", "lease": first["lease"]})
        self.assertEqual(job.lease, second["lease"])
        self.assertEqual(job.owner, "user")

    def test_expired_manual_lease_pauses_worker(self):
        provider = GateProvider()
        app = self.build({"local": provider})
        run = app.start_run({"prompt": "fixture"})
        self.assertTrue(provider.entered.wait(3))
        job = app.runs[run].jobs[0]
        with patch("local_computer_use.orchestrator.MANUAL_TIMEOUT", 0.02):
            takeover = app.command(job.id, {"command": "takeover"})
        self.wait_for(lambda: job.owner is None and job.status == "paused")
        self.assertGreater(job.lease, takeover["lease"])
        self.assertIn("expired", job.detail)

    def test_dependencies_receive_previous_result(self):
        provider = PlannerProvider()
        app = self.build({"local": provider}, sessions=2)
        run = app.start_run({"prompt": "catalog", "count": 2})
        self.wait_for(lambda: app.runs[run].status == "completed")
        jobs = app.runs[run].jobs
        self.assertEqual(jobs[1].depends_on, [jobs[0].id])
        self.assertIn("Gather [completed]: verified outcome", provider.calls[-1][0])

    def test_cloud_selection_requires_explicit_consent(self):
        local = GateProvider()
        cloud = GateProvider()
        cloud.is_cloud = True
        app = self.build({"local": local, "gemini": cloud})
        with self.assertRaises(RequestError):
            app.start_run({"prompt": "private fixture", "provider": "gemini"})
        run = app.start_run({"prompt": "local fixture"})
        self.assertTrue(local.entered.wait(3))
        self.assertFalse(cloud.entered.is_set())
        app.send(run, "coordinator", "Follow-up")
        self.assertEqual(app.store.inbox(run, app.runs[run].jobs[0].id, 0), [])
        app.send(run, app.runs[run].jobs[0].id, "Direct note")
        messages = app.store.inbox(run, app.runs[run].jobs[0].id, 0)
        self.assertEqual(messages[0]["text"], "Direct note")
        app.cancel_run(run)
        self.wait_for(lambda: app.runs[run].status == "cancelled")

    def test_named_models_share_local_inference_limit(self):
        first, second = GateProvider(), GateProvider()
        app = self.build({"local": first, "local-secondary": second}, sessions=2, inference=1)
        run = app.start_run({"prompt": "fixture", "assignments": [
            {"prompt": "first"}, {"prompt": "second", "provider": "local-secondary"},
        ]})
        self.wait_for(lambda: first.entered.is_set() or second.entered.is_set())
        self.wait_for(lambda: len(self.desktops) == 2)
        self.assertNotEqual(first.entered.is_set(), second.entered.is_set())
        self.assertEqual(app.state()[1]["metrics"]["active_inferences"], 1)
        first.release.set()
        second.release.set()
        self.wait_for(lambda: app.runs[run].status == "completed")

    def test_private_preview_requires_current_manual_owner(self):
        provider = GateProvider()
        app = self.build({"local": provider})
        run = app.start_run({"prompt": "fixture"})
        self.assertTrue(provider.entered.wait(3))
        job = app.runs[run].jobs[0]
        original, _, private = app.frame(job.id)
        self.assertFalse(private)
        control = app.command(job.id, {"command": "takeover"})
        privacy = app.command(job.id, {"command": "privacy-on", "lease": control["lease"]})
        self.assertEqual(app.frame(job.id), (None, 0, True))
        self.assertEqual(app.frame(job.id, privacy["privacy_token"])[0], original)
        self.assertNotIn("privacy_token", str(app.state()[1]))
        with self.assertRaises(RequestError):
            app.command(job.id, {"command": "return", "lease": control["lease"]})
        app.command(job.id, {"command": "pause", "lease": control["lease"]})
        self.assertEqual(app.frame(job.id, privacy["privacy_token"]), (None, 0, True))
        next_owner = app.command(job.id, {"command": "takeover"})
        self.assertNotEqual(privacy["privacy_token"], next_owner["privacy_token"])
        app.command(job.id, {"command": "privacy-off", "lease": next_owner["lease"]})
        app.command(job.id, {"command": "return", "lease": next_owner["lease"]})
        provider.release.set()
        self.wait_for(lambda: job.status == "completed")

    def test_scoped_copies_and_profile_queue(self):
        provider = GateProvider()
        app = self.build({"local": provider}, sessions=2, browser_profiles=("reusable",))
        invalid = {"prompt": "bad", "input_files": [{"name": "../x", "data": "bG9jYWw="}]}
        with self.assertRaises(RequestError):
            app.start_run(invalid)
        run = app.start_run({"prompt": "fixture", "mode": "manual", "count": 2,
                             "browser_profile": "reusable", "input_files": [
                                 {"name": "Fixture.txt", "data": "bG9jYWw="},
                             ]})
        self.assertTrue(provider.entered.wait(3))
        self.assertEqual(len(self.desktops), 1)
        self.assertEqual(self.desktops[0].template, "browser")
        self.assertEqual(self.desktops[0].options, {"browser_profile": "reusable"})
        before = self.desktops[0].workspace / "imports" / "Fixture.txt"
        self.assertEqual(before.read_bytes(), b"local")
        before.write_bytes(b"only the first copy")
        provider.release.set()
        self.wait_for(lambda: app.runs[run].status == "completed")
        self.assertEqual((self.desktops[1].workspace / "imports" / "Fixture.txt").read_bytes(), b"local")

    def test_cross_provider_dependencies_require_a_second_opt_in(self):
        local = GateProvider()
        cloud = GateProvider()
        cloud.is_cloud = True
        app = self.build({"local": local, "gemini": cloud}, sessions=1)
        for sharing in (False, True):
            run = app.start_run({"prompt": "private", "cloud_consent": True, "share_results": sharing,
                                 "assignments": [{"prompt": "local task"},
                                                 {"prompt": "cloud task", "provider": "gemini", "depends_on": [0]}]})
            local.release.set()
            self.wait_for(lambda: cloud.entered.is_set())
            self.assertEqual("fixture finished" in cloud.prompts[-1], sharing)
            app.cancel_run(run)
            self.wait_for(lambda: app.runs[run].status == "partial")
            cloud.entered.clear()

    def test_cancelling_queued_jobs_releases_pending_entries_with_full_slots(self):
        provider = GateProvider()
        app = self.build({"local": provider}, sessions=1)
        app.start_run({"prompt": "worker holds the slot"})
        self.assertTrue(provider.entered.wait(3))
        for _ in range(12):
            run = app.start_run({"prompt": "queued and cancelled"})
            app.cancel_run(run)
        self.assertFalse(app.pending)

    def test_motion_from_a_stale_focus_observation_is_never_applied(self):
        class MoveThenFinish(GateProvider):
            def decide(self, prompt, image, cancel):
                self.prompts.append(prompt)
                if len(self.prompts) == 1:
                    self.entered.set()
                    self.release.wait(3)
                    return {"type": "click", "x": 3, "y": 8}
                return {"type": "done", "result": "observed again"}

        provider = MoveThenFinish()
        app = self.build({"local": provider})
        run = app.start_run({"prompt": "fixture"})
        self.assertTrue(provider.entered.wait(3))
        self.desktops[0].focus = 40
        provider.release.set()
        self.wait_for(lambda: app.runs[run].status == "completed")
        self.assertEqual(self.desktops[0].actions, [])
        self.assertIn("Desktop focus changed", provider.prompts[-1])

    def test_observe_only_rejects_agent_input_but_allows_manual_input(self):
        class ObserveProvider(GateProvider):
            def decide(self, prompt, image, cancel):
                self.prompts.append(prompt)
                if len(self.prompts) == 1:
                    return {"type": "click", "x": 9, "y": 11}
                if len(self.prompts) == 2:
                    self.entered.set()
                    self.release.wait(3)
                return {"type": "done", "result": "Observed only"}

        provider = ObserveProvider()
        app = self.build({"local": provider})
        run = app.start_run({"prompt": "Read the page", "observe_only": True})
        self.assertTrue(provider.entered.wait(3))
        job = app.runs[run].jobs[0]
        control = app.command(job.id, {"command": "takeover"})
        app.command(job.id, {"command": "input", "lease": control["lease"], "action": {
            "type": "click", "x": 1000, "y": 0, "units": "normalized",
        }})
        app.command(job.id, {"command": "return"})
        provider.release.set()
        self.wait_for(lambda: job.status == "completed")
        self.assertIn("Input actions are disabled", provider.prompts[0])
        self.assertIn("can only observe", provider.prompts[1])
        self.assertEqual(self.desktops[0].actions, [{"type": "click", "button": 1, "x": 959, "y": 0}])

    def test_handoff_reuses_live_desktop_and_workspace(self):
        class HandoffProvider(GateProvider):
            def decide(self, prompt, image, cancel):
                self.prompts.append(prompt)
                if "Worker: Source\n" in prompt:
                    target = prompt.split("Destination (")[1].split(")")[0]
                    return {"type": "handoff", "target": target, "result": "retained state"}
                if "Pressed a key." not in prompt:
                    return {"type": "key", "key": "ctrl+s"}
                return {"type": "done", "result": "reused session"}

        app = self.build({"local": HandoffProvider()}, sessions=1)
        run = app.start_run({"prompt": "fixture", "assignments": [
            {"title": "Source", "prompt": "start"},
            {"title": "Destination", "prompt": "finish", "depends_on": [0]},
        ]})
        self.wait_for(lambda: app.runs[run].status == "completed")
        source, destination = app.runs[run].jobs
        self.assertEqual(len(self.desktops), 1)
        self.assertEqual(destination.workspace_id, source.id)
        self.assertEqual(app.artifact(destination.id, "notes.txt"), b"export")

    def test_old_active_run_is_recovered_even_outside_recent_history(self):
        store = Store(Path(self.temp.name))
        old = Run("old", "old", "000", [Job("old-job", "old", "work", "task", "local", "editor", "running")],
                  status="running")
        store.save(asdict(old))
        for index in range(101):
            later = Run(str(index), "finished", f"{index + 1:03}", [], status="completed")
            store.save(asdict(later))
        store.close()
        app = self.build({"local": GateProvider()})
        row = app.store.db.execute("SELECT status FROM runs WHERE id='old'").fetchone()
        self.assertEqual(row["status"], "interrupted")

    def test_artifact_path_is_scoped_and_symlinks_are_rejected(self):
        provider = GateProvider()
        app = self.build({"local": provider})
        run = app.start_run({"prompt": "fixture"})
        self.assertTrue(provider.entered.wait(3))
        job = app.runs[run].jobs[0]
        workspace = self.desktops[0].workspace
        (workspace / "notes.txt").write_bytes(b"verified")
        (workspace / "shortlist.csv").symlink_to(Path(self.temp.name) / "runs.sqlite3")
        provider.release.set()
        self.wait_for(lambda: job.status == "completed")
        self.assertEqual(job.artifacts, ["notes.txt"])
        self.assertEqual(app.artifact(job.id, "notes.txt"), b"verified")
        with self.assertRaises(RequestError):
            app.artifact(job.id, "shortlist.csv")
        app.close()
        app = self.build({"local": provider})
        self.assertEqual(app.artifact(job.id, "notes.txt"), b"verified")

    def test_frame_sequence_changes_only_when_pixels_change(self):
        provider = GateProvider()
        app = self.build({"local": provider})
        run = app.start_run({"prompt": "fixture"})
        self.assertTrue(provider.entered.wait(3))
        job = app.runs[run].jobs[0]
        _, original, _ = app.frame(job.id)
        _, unchanged, _ = app.frame(job.id)
        self.assertEqual(original, unchanged)
        control = app.command(job.id, {"command": "takeover"})
        app.command(job.id, {"command": "input", "lease": control["lease"],
                             "action": {"type": "click", "x": 100, "y": 120}})
        _, updated, _ = app.frame(job.id)
        self.assertEqual(updated, original + 1)


if __name__ == "__main__":
    unittest.main()
