import unittest
from unittest.mock import Mock, patch

from parallel_computer_use._browser import Chrome


class GuestBrowserTest(unittest.TestCase):
    def setUp(self):
        self.url = "https://catalog.example/search?q=fixture"
        with patch("parallel_computer_use._browser.subprocess.Popen", return_value=Mock(poll=lambda: 0)):
            self.browser = Chrome(["chrome"], self.url, ["catalog.example"])
        self.addCleanup(self.browser.close)
        self.pages, self.frames, self.visible = {}, {}, set()
        self.browser.send = Mock(side_effect=self.protocol)

    def page(self, target, url, *, loader="loader", **frame):
        self.pages[target] = {"type": "page", "targetId": target, "url": url}
        base, marker, fragment = url.partition("#")
        self.frames[target] = {"id": target, "loaderId": loader, "url": base,
                               "urlFragment": marker + fragment, **frame}

    def protocol(self, method, **options):
        if method == "Target.getTargets":
            return {"targetInfos": list(self.pages.values())}
        if method == "Target.attachToTarget":
            return {"sessionId": options["targetId"]}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": self.frames[self.browser.session]}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": len(self.browser.send.mock_calls)}
        if method == "Runtime.evaluate":
            if "document.hasFocus() && document.visibilityState" in options["expression"] and "createRange" not in options["expression"]:
                value = self.browser.session in self.visible
            else:
                value = {"text": "h1 Search", "count": 0, "visual": False, "status": "", "ready": "complete"}
            return {"result": {"value": value}}
        self.fail(f"Unexpected protocol method {method}")

    def test_skips_ineligible_targets_frames_and_browser_error_pages(self):
        for bad in ("http://catalog.example/", "https://sub.catalog.example/", "file:///work/x",
                    "https://catalog.example:444/", "https://author@catalog.example/"):
            with self.subTest(url=bad):
                self.page("one", bad)
                self.assertIsNone(self.browser.observe())
                self.assertEqual([call.args[0] for call in self.browser.send.mock_calls], ["Target.getTargets"])
                self.browser.send.reset_mock()
        self.page("one", self.url, unreachableUrl=self.url)
        self.assertIsNone(self.browser.observe())
        self.page("one", self.url)
        self.frames["one"]["url"] = "https://outside.example/search"
        self.assertIsNone(self.browser.observe())
        self.assertNotIn("Runtime.evaluate", [call.args[0] for call in self.browser.send.mock_calls])

    def test_selects_one_focused_tab_and_fails_closed_on_ambiguity(self):
        self.page("one", self.url)
        self.page("two", "https://catalog.example/result")
        self.page("three", "https://outside.example/private")
        self.visible = {"two"}
        self.assertEqual(self.browser.observe()["page"], "https://catalog.example/result")
        self.assertEqual(self.browser.document[0], "two")
        self.assertNotIn("three", self.browser.targets)
        for visible in ({"one", "two"}, {"three"}):
            self.visible = visible
            self.assertIsNone(self.browser.observe())
            self.assertIsNone(self.browser.document)

    def test_rejects_old_revision_after_same_document_url_change(self):
        self.page("one", self.url)
        first = self.browser.observe()
        world = self.browser.world
        self.assertEqual(first["page"], "https://catalog.example/search")
        second = self.browser.observe()
        self.assertEqual(second["document"], first["document"])
        self.page("one", "https://catalog.example/result?filter=new#section")
        self.assertFalse(self.browser.click(second["revision"], "e1"))
        self.assertEqual(self.browser.world, world)
        self.assertEqual(self.browser.document[-1], "https://catalog.example/result?filter=new#section")
        current = self.browser.observe()
        self.assertEqual(current["page"], "https://catalog.example/result")
        self.assertNotEqual(current["document"], first["document"])
