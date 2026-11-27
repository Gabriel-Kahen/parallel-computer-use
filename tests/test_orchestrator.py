import tempfile
import threading
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from parallel_computer_use._network import validate_network_target
from parallel_computer_use.orchestrator import Job, Orchestrator, RequestError, Run, TERMINAL
from parallel_computer_use.providers import DecisionUsage, ProviderResponseError
from parallel_computer_use.store import Store


class FakeDesktop:
    width, height = 960, 640

    def __init__(self, template, workspace):
        self.template = template
        self.workspace = workspace
        self.actions = []
        self.closed = False
        self.releases = 0
        self.focus = 27
        self.crops = []
        self.captures = 0

    def observation(self):
        return {"width": self.width, "height": self.height, "focus": self.focus,
                "active_window": self.focus}

    def png(self):
        self.captures += 1
        return b"frame:" + str(len(self.actions)).encode()

    def png_region(self, x, y, width, height):
        self.crops.append((x, y, width, height))
        return b"crop"

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

    def test_pending_job_captures_after_a_model_slot_is_available(self):
        provider = GateProvider()
        app = self.build({"local": provider}, sessions=2, inference=1)
        run = app.start_run({"prompt": "fixture", "count": 2, "mode": "manual"})
        self.assertTrue(provider.entered.wait(3))
        self.wait_for(lambda: len(self.desktops) == 2)
        self.assertEqual(sum(desktop.captures for desktop in self.desktops), 1)
        provider.release.set()
        self.wait_for(lambda: app.runs[run].status == "completed")
        self.assertEqual([desktop.captures for desktop in self.desktops], [1, 1])

    def test_verification_waits_for_application_to_paint_after_input(self):
        class PaintProvider(GateProvider):
            def decide(self, prompt, image, cancel):
                self.images.append(image)
                if len(self.images) == 1:
                    self.entered.set()
                    self.release.wait(3)
                    return {"type": "key", "key": "ctrl+s"}
                return {"type": "done", "result": "Save verified"}

        provider = PaintProvider()
        app = self.build({"local": provider})
        run = app.start_run({"prompt": "Save the document"})
        self.assertTrue(provider.entered.wait(3))
        desktop = self.desktops[0]
        apply = desktop.apply
        paint_at = float("inf")

        def delayed_apply(action):
            nonlocal paint_at
            apply(action)
            paint_at = time.monotonic() + 0.07

        desktop.apply = delayed_apply
        desktop.png = lambda: b"new" if time.monotonic() >= paint_at else b"frame:0"
        provider.release.set()
        self.wait_for(lambda: app.runs[run].status == "completed")
        self.assertEqual(provider.images, [b"frame:0", b"new"])

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

    def test_turn_limit_keeps_a_desktop_during_manual_takeover(self):
        provider = GateProvider()
        app = self.build({"local": provider})
        with patch("parallel_computer_use.orchestrator.MAX_STEPS", 1):
            run = app.start_run({"prompt": "fixture"})
            job = app.runs[run].jobs[0]
            self.assertTrue(provider.entered.wait(3))
            claim = app.command(job.id, {"command": "takeover"})
            self.assertTrue(provider.cancelled.wait(2))
            app.command(job.id, {"command": "input", "lease": claim["lease"],
                                 "action": {"type": "key", "key": "Return"}})
            self.assertFalse(self.desktops[0].closed)
            app.command(job.id, {"command": "return", "lease": claim["lease"]})
            self.wait_for(lambda: job.status == "failed")
            self.assertIn("1-turn budget", job.detail)

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
        with patch("parallel_computer_use.orchestrator.MANUAL_TIMEOUT", 0.02):
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

    def test_crop_is_single_turn_and_clicks_map_to_the_crop(self):
        class ZoomProvider(GateProvider):
            def decide_with_usage(self, prompt, screenshot, cancel, *, directives):
                self.images.append(screenshot)
                if len(self.images) == 1:
                    self.directives = directives
                    return {"type": "zoom", "region": [250, 250, 750, 750]}, DecisionUsage(700, 27, 30)
                if len(self.images) == 2:
                    self.cropped_directives = directives
                    return {"type": "click", "x": 1000, "y": 0, "units": "normalized"}, DecisionUsage(200, 25, 101)
                return {"type": "done", "result": "Read and verified"}, DecisionUsage(700, 13, 66)

        provider = ZoomProvider()
        app = self.build({"local": provider})
        run = app.start_run({"prompt": "Inspect a small detail"})
        self.wait_for(lambda: app.runs[run].status == "completed")
        job = app.runs[run].jobs[0]
        self.assertEqual(self.desktops[0].crops, [(240, 160, 480, 480 - 160)])
        self.assertEqual(self.desktops[0].actions, [{"type": "click", "x": 719, "y": 160, "button": 1}])
        self.assertEqual(provider.images, [b"frame:0", b"crop", b"frame:1"])
        self.assertIn("zoom", provider.directives)
        self.assertIn("overview", provider.cropped_directives)
        self.assertNotIn("zoom", provider.cropped_directives)
        self.assertEqual(job.model_usage["input_tokens"], 1600)
        self.assertEqual(job.model_usage["output_tokens"], 65)
        self.assertEqual(job.model_usage["cached_tokens"], 197)

    def test_semantic_worker_switches_to_pixels_when_it_needs_visual_evidence(self):
        class MissingVisual(GateProvider):
            def decide_with_usage(self, prompt, image, cancel, *, directives):
                self.images.append(image)
                self.prompts.append(prompt)
                if len(self.images) == 1:
                    self.directives = directives
                    return {"type": "visual"}, DecisionUsage()
                if len(self.images) == 2:
                    raise ProviderResponseError("Invalid first image response")
                return {"type": "done", "result": "Read the visual result"}, DecisionUsage()

        state = {"text": "h1 Canvas example", "visual": True, "count": 0, "revision": 19}
        provider = MissingVisual()
        with patch.object(FakeDesktop, "browser_state", return_value=state, create=True):
            app = self.build({"local": provider}, observation_mode="semantic")
            run = app.start_run({"prompt": "Read the drawn canvas", "template": "browser", "observe_only": True})
            job = app.runs[run].jobs[0]
            self.wait_for(lambda: job.status == "completed")
        self.assertEqual(provider.images, [None, b"frame:0", b"frame:0"])
        self.assertIn("visual", provider.directives)
        self.assertNotIn("click_ref", provider.directives)
        self.assertIn("Canvas example", provider.prompts[0])
        self.assertEqual(job.model_usage["visual_fallbacks"], 2)

    def test_browser_ref_cannot_dispatch_after_focus_changes(self):
        class DelayedRef(GateProvider):
            def decide(self, prompt, image, cancel):
                self.prompts.append(prompt)
                if len(self.prompts) == 1:
                    self.entered.set()
                    self.release.wait(3)
                    return {"type": "click_ref", "target": "e1"}
                return {"type": "done", "result": "Observed again"}

        state = {"text": "button Apply [e1]", "visual": False, "count": 1, "revision": 19}
        provider = DelayedRef()
        with (patch.object(FakeDesktop, "browser_state", return_value=state, create=True),
              patch.object(FakeDesktop, "browser_click", return_value=True, create=True) as click):
            app = self.build({"local": provider}, observation_mode="hybrid")
            run = app.start_run({"prompt": "Click Apply", "template": "browser"})
            self.assertTrue(provider.entered.wait(3))
            self.desktops[0].focus = 40
            provider.release.set()
            self.wait_for(lambda: app.runs[run].status == "completed")
            click.assert_not_called()
        self.assertIn("Browser element changed", provider.prompts[-1])

    def test_browser_ref_cannot_dispatch_after_cancel_during_the_focus_check(self):
        class ReferenceProvider(GateProvider):
            def decide(self, prompt, image, cancel):
                return {"type": "click_ref", "target": "e1"}

        entered, release = threading.Event(), threading.Event()
        state = {"text": "button Apply [e1]", "visual": False, "count": 1, "revision": 19}
        normal_observation = FakeDesktop.observation

        def hold_observation(desktop):
            if not hasattr(desktop, "observed"):
                desktop.observed = True
            else:
                entered.set()
                release.wait(3)
            return normal_observation(desktop)

        with (patch.object(FakeDesktop, "observation", hold_observation),
              patch.object(FakeDesktop, "browser_state", return_value=state, create=True),
              patch.object(FakeDesktop, "browser_click", return_value=True, create=True) as click):
            app = self.build({"local": ReferenceProvider()}, observation_mode="semantic")
            run = app.start_run({"prompt": "Click Apply", "template": "browser"})
            try:
                self.assertTrue(entered.wait(3))
                app.cancel_run(run)
            finally:
                release.set()
            self.wait_for(lambda: app.runs[run].jobs[0].status == "cancelled")
            click.assert_not_called()

    def test_changed_status_requires_verification_before_more_input(self):
        class ReferenceProvider(GateProvider):
            def decide_with_usage(self, prompt, image, cancel, *, directives):
                self.prompts.append(prompt)
                if len(self.prompts) == 1:
                    return {"type": "click_ref", "target": "e1"}, DecisionUsage()
                self.verify_directives = directives
                return {"type": "done", "result": "Applied"}, DecisionUsage()

        def state(desktop):
            status = "Applied" if desktop.actions else "Ready"
            return {"text": f"button Apply [e1]\np {status}", "count": 1, "revision": 19,
                    "visual": False, "status": status}

        def click(desktop, revision, target):
            desktop.actions.append(target)
            return True

        provider = ReferenceProvider()
        with (patch.object(FakeDesktop, "browser_state", state, create=True),
              patch.object(FakeDesktop, "browser_click", click, create=True)):
            app = self.build({"local": provider}, observation_mode="hybrid")
            run = app.start_run({"prompt": "Click Apply once", "template": "browser"})
            self.wait_for(lambda: app.runs[run].jobs[0].status == "completed")
        self.assertEqual(self.desktops[0].actions, ["e1"])
        self.assertIn("continue", provider.verify_directives)
        self.assertNotIn("click_ref", provider.verify_directives)
        self.assertIn("After input, the application shows this status: Applied", provider.prompts[1])

    def test_navigation_to_a_new_page_uses_text_verification_without_another_click(self):
        class ReferenceProvider(GateProvider):
            def decide_with_usage(self, prompt, image, cancel, *, directives):
                self.prompts.append(prompt)
                self.images.append(image)
                if len(self.prompts) == 1:
                    return {"type": "click_ref", "target": "e1"}, DecisionUsage()
                self.verify_directives.append(directives)
                if len(self.prompts) == 2:
                    return {"type": "wait", "seconds": 0}, DecisionUsage()
                return {"type": "done", "result": "Read chapter"}, DecisionUsage()

        def state(desktop):
            visited = bool(desktop.actions) and len(provider.prompts) >= 2
            return {"text": "h1 Chapter" if visited else "a Reference [e1]", "count": 1, "revision": 19,
                    "visual": visited, "status": "", "page": "https://docs.example/" + ("chapter" if visited else "search")}

        def click(desktop, revision, target):
            desktop.actions.append(target)
            return True

        provider = ReferenceProvider()
        provider.verify_directives = []
        with (patch.object(FakeDesktop, "browser_state", state, create=True),
              patch.object(FakeDesktop, "browser_click", click, create=True)):
            app = self.build({"local": provider}, observation_mode="targeted")
            run = app.start_run({"prompt": "Open and check the chapter", "template": "browser"})
            self.wait_for(lambda: app.runs[run].jobs[0].status == "completed")
        self.assertEqual(self.desktops[0].actions, ["e1"])
        self.assertEqual(provider.images, [None, None, None])
        for directives in provider.verify_directives:
            self.assertNotIn("click_ref", directives)
            self.assertNotIn("click_link", directives)
        self.assertIn("Wait if it is still loading.", provider.prompts[1])
        self.assertIn("https://docs.example/chapter", provider.prompts[2])
        self.assertIn("previous action opened a new browser page", provider.prompts[2])
        self.assertIn("Previous visible link (page data): Reference", provider.prompts[2])

    def test_online_pixel_worker_rechecks_a_changed_page_before_reporting_a_blocker(self):
        class LoadingProvider(GateProvider):
            def decide(self, prompt, image, cancel):
                self.prompts.append(prompt)
                self.images.append(image)
                if len(self.images) == 1:
                    self.entered.set()
                    self.release.wait(3)
                    return {"type": "blocked", "reason": "Search has not rendered"}
                return {"type": "done", "result": "Found the listing"}

        provider = LoadingProvider()
        app = self.build({"local": provider}, network_domains=("docs.example",),
                         validate_browser_url=validate_network_target)
        run = app.start_run({"prompt": "Find the visible result", "start_url": "https://docs.example/search",
                             "template": "browser", "observe_only": True})
        self.assertTrue(provider.entered.wait(3))
        self.desktops[0].actions.append("Page rendered")
        provider.release.set()
        self.wait_for(lambda: app.runs[run].jobs[0].status == "completed")
        self.assertEqual(provider.images, [b"frame:0", b"frame:1"])
        self.assertIn("Inspect the latest frame before reporting a blocker", provider.prompts[1])

    def test_unacknowledged_browser_click_requires_verification_without_status(self):
        class ReferenceProvider(GateProvider):
            def decide(self, prompt, image, cancel):
                self.prompts.append(prompt)
                if len(self.prompts) <= 2:
                    return {"type": "click_ref", "target": "e1"}
                return {"type": "done", "result": "Inspected"}

        state = {"text": "button Apply [e1]", "visual": False, "count": 1, "revision": 19}
        provider = ReferenceProvider()
        with (patch.object(FakeDesktop, "browser_state", return_value=state, create=True),
              patch.object(FakeDesktop, "browser_click", return_value=None, create=True) as click):
            app = self.build({"local": provider}, observation_mode="semantic")
            run = app.start_run({"prompt": "Click Apply once", "template": "browser"})
            self.wait_for(lambda: app.runs[run].status == "completed")
            click.assert_called_once()
        self.assertIn("previous click may have taken effect", provider.prompts[1])
        self.assertIn("Verify the new application status", provider.prompts[-1])

    def test_targeted_click_reobserves_the_full_screen_when_canvas_pixels_change(self):
        class PointProvider(GateProvider):
            def decide(self, prompt, image, cancel):
                self.images.append(image)
                self.prompts.append(prompt)
                if len(self.images) < 3:
                    return {"type": "click", "x": 10, "y": 20}
                return {"type": "done", "result": "Inspected"}

        state = {"text": "h1 Drawing", "visual": True, "count": 0, "revision": 19}
        provider = PointProvider()
        with (patch.object(FakeDesktop, "browser_state", return_value=state, create=True),
              patch.object(FakeDesktop, "browser_crop_region", return_value=(100, 100, 300, 200), create=True),
              patch.object(FakeDesktop, "png_region", side_effect=[b"old crop", b"changed crop"])):
            app = self.build({"local": provider}, observation_mode="targeted")
            run = app.start_run({"prompt": "Click in the canvas", "template": "browser"})
            self.wait_for(lambda: app.runs[run].jobs[0].status == "completed")
        self.assertEqual(provider.images, [b"old crop", b"frame:0", b"frame:1"])
        self.assertEqual(self.desktops[0].actions, [{"type": "click", "x": 10, "y": 20, "button": 1}])
        self.assertIn("canvas changed", provider.prompts[1])

    def test_targeted_click_requires_verification_if_status_changes_outside_the_crop(self):
        class PointProvider(GateProvider):
            def decide(self, prompt, image, cancel):
                self.images.append(image)
                self.prompts.append(prompt)
                if len(self.images) == 1:
                    return {"type": "click", "x": 10, "y": 20}
                return {"type": "done", "result": "Already applied"}

        ready = {"text": "p Ready", "status": "Ready", "visual": True, "count": 0, "revision": 19}
        applied = {**ready, "text": "p Already applied", "status": "Already applied", "revision": 20}
        provider = PointProvider()
        with (patch.object(FakeDesktop, "browser_state", side_effect=[ready, applied, applied], create=True),
              patch.object(FakeDesktop, "browser_crop_region", return_value=(100, 100, 300, 200), create=True)):
            app = self.build({"local": provider}, observation_mode="targeted")
            run = app.start_run({"prompt": "Apply once", "template": "browser"})
            self.wait_for(lambda: app.runs[run].jobs[0].status == "completed")
        self.assertEqual(provider.images, [b"crop", None])
        self.assertEqual(self.desktops[0].actions, [])
        self.assertIn("After input, the application shows this status: Already applied", provider.prompts[-1])

    def test_targeted_click_cannot_reach_a_replaced_document_with_the_same_visible_path(self):
        class PointProvider(GateProvider):
            def decide(self, prompt, image, cancel):
                self.images.append(image)
                self.prompts.append(prompt)
                if len(self.images) == 1:
                    return {"type": "click", "x": 10, "y": 20}
                return {"type": "done", "result": "Inspected"}

        original = {"text": "h1 Drawing", "visual": True, "count": 0, "revision": 19,
                    "document": 1, "page": "https://docs.example/drawing"}
        replacement = {**original, "document": 2, "revision": 20}
        provider = PointProvider()
        with (patch.object(FakeDesktop, "browser_state", side_effect=[original, replacement, replacement], create=True),
              patch.object(FakeDesktop, "browser_crop_region", return_value=(100, 100, 300, 200), create=True),
              patch.object(FakeDesktop, "browser_canvas_contains", return_value=True, create=True)):
            app = self.build({"local": provider}, observation_mode="targeted")
            run = app.start_run({"prompt": "Inspect the drawing", "template": "browser"})
            self.wait_for(lambda: app.runs[run].jobs[0].status == "completed")
        self.assertEqual(provider.images, [b"crop", None])
        self.assertEqual(self.desktops[0].actions, [])
        self.assertIn("The browser document changed.", provider.prompts[-1])

    def test_nested_zoom_is_rejected_and_reobserves_overview(self):
        class RepeatZoomProvider(GateProvider):
            def decide(self, prompt, image, cancel):
                self.images.append(image)
                if len(self.images) <= 2:
                    return {"type": "zoom", "region": [250, 250, 750, 750]}
                self.final_prompt = prompt
                return {"type": "done", "result": "Read the complete screen"}

        provider = RepeatZoomProvider()
        app = self.build({"local": provider})
        run = app.start_run({"prompt": "Compare two visible rows", "observe_only": True})
        self.wait_for(lambda: app.runs[run].status == "completed")
        self.assertEqual(provider.images, [b"frame:0", b"crop", b"frame:0"])
        self.assertIn("Return to the full screen", provider.final_prompt)

    def test_overview_rejects_a_duplicate_crop_on_the_same_frame(self):
        class RepeatZoomProvider(GateProvider):
            def decide(self, prompt, image, cancel):
                self.images.append(image)
                if len(self.images) in (1, 3):
                    return {"type": "zoom", "region": [250, 250, 750, 750]}
                if len(self.images) == 2:
                    return {"type": "overview"}
                self.final_prompt = prompt
                return {"type": "done", "result": "Read the complete screen"}

        provider = RepeatZoomProvider()
        app = self.build({"local": provider})
        run = app.start_run({"prompt": "Compare two visible rows", "observe_only": True})
        self.wait_for(lambda: app.runs[run].status == "completed")
        self.assertEqual(provider.images, [b"frame:0", b"crop", b"frame:0", b"frame:0"])
        self.assertIn("This region was already shown", provider.final_prompt)

    def test_zoom_requested_from_a_stale_focus_falls_back_to_the_full_frame(self):
        class ZoomProvider(GateProvider):
            def decide(self, prompt, image, cancel):
                self.images.append(image)
                if len(self.images) == 1:
                    self.entered.set()
                    self.release.wait(3)
                    return {"type": "zoom", "region": [100, 100, 900, 900]}
                return {"type": "done", "result": "Fresh view"}

        provider = ZoomProvider()
        app = self.build({"local": provider})
        run = app.start_run({"prompt": "Inspect an offline fixture", "observe_only": True})
        self.assertTrue(provider.entered.wait(3))
        self.desktops[0].focus += 1
        provider.release.set()
        self.wait_for(lambda: app.runs[run].status == "completed")
        self.assertEqual(provider.images, [b"frame:0", b"frame:0"])
        self.assertEqual(self.desktops[0].crops, [])

    def test_batch_collapses_two_input_turns_and_keeps_final_observation(self):
        class BatchProvider(GateProvider):
            def decide(self, prompt, image, cancel):
                self.images.append(image)
                if len(self.images) == 1:
                    return {"type": "batch", "actions": [
                        {"type": "type", "text": "fixture"},
                        {"type": "key", "key": "ctrl+s"},
                    ]}
                self.finish_prompt = prompt
                return {"type": "done", "result": "Saved"}

        provider = BatchProvider()
        app = self.build({"local": provider})
        run = app.start_run({"prompt": "Write a fixture note and save it"})
        self.wait_for(lambda: app.runs[run].status == "completed")
        job = app.runs[run].jobs[0]
        self.assertEqual(app.artifact(job.id, "notes.txt"), b"export")
        self.assertEqual(job.model_usage["requests"], 2)
        self.assertEqual(provider.images, [b"frame:0", b"frame:2"])
        self.assertIn("Applied 2 of 2 batch steps", provider.finish_prompt)

    def test_batch_stops_when_typing_changes_focus(self):
        class BatchProvider(GateProvider):
            def decide(self, prompt, image, cancel):
                self.prompts.append(prompt)
                if len(self.prompts) == 1:
                    self.entered.set()
                    self.release.wait(3)
                    return {"type": "batch", "actions": [
                        {"type": "type", "text": "protected"},
                        {"type": "key", "key": "ctrl+s"},
                    ]}
                return {"type": "done", "result": "Focus changed"}

        provider = BatchProvider()
        app = self.build({"local": provider})
        run = app.start_run({"prompt": "Batch fixture"})
        self.assertTrue(provider.entered.wait(3))
        desktop = self.desktops[0]
        first = desktop.apply

        def change_focus(action):
            first(action)
            desktop.focus += 1

        desktop.apply = change_focus
        provider.release.set()
        self.wait_for(lambda: app.runs[run].status == "completed")
        self.assertEqual(desktop.actions, [{"type": "type", "text": "protected"}])
        self.assertIn("Batch stopped after 1 step(s)", provider.prompts[-1])

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
