import http.client
import json
import threading
import unittest

from parallel_computer_use.server import LocalServer


class App:
    def state(self, revision):
        return 7, None if revision == 7 else {"runs": []}

    def start_run(self, body):
        return "run-for-" + body["prompt"]

    def frame(self, _job, _key):
        return b"image", 3, False


class LocalServerTest(unittest.TestCase):
    def setUp(self):
        self.server = LocalServer(App(), 0)
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.key = self.server.new_launch_url().split("#")[1]

    def request(self, method, path, body=None, **headers):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            if body is not None:
                headers.setdefault("Content-Type", "application/json")
                body = json.dumps(body)
            client.request(method, path, body, headers)
            reply = client.getresponse()
            return reply.status, dict(reply.getheaders()), reply.read()
        finally:
            client.close()

    def login(self):
        status, headers, body = self.request("POST", "/api/auth", {"token": self.key}, Origin=self.server.origin)
        self.assertEqual(status, 200)
        self.assertNotIn("Set-Cookie", headers)
        return "Bearer " + json.loads(body)["token"]

    def test_private_routes_and_single_use_launch_token(self):
        self.assertEqual(self.request("GET", "/api/state")[0], 401)
        self.assertEqual(self.request("GET", "/api/state", Host="fake.test")[0], 403)
        authorization = self.login()
        self.assertEqual(self.request("POST", "/api/auth", {"token": self.key}, Origin=self.server.origin)[0], 401)
        self.assertEqual(self.request("GET", "/api/state", Authorization=authorization)[0], 200)
        self.assertEqual(self.request("GET", "/api/state", Authorization=authorization,
                                      Origin="http://127.0.0.1:15937")[0], 403)

    def test_writes_require_exact_origin_and_json(self):
        authorization = self.login()
        payload = {"prompt": "hello"}
        self.assertEqual(self.request("POST", "/api/runs", payload, Authorization=authorization)[0], 403)
        self.assertEqual(self.request("POST", "/api/runs", payload, Origin="http://example.org",
                                      Authorization=authorization)[0], 403)
        self.assertEqual(self.request("POST", "/api/runs", payload, Origin=self.server.origin,
                                      Authorization=authorization, **{"Content-Type": "text/plain"})[0], 415)
        status, _, body = self.request("POST", "/api/runs", payload, Origin=self.server.origin,
                                       Authorization=authorization)
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body), {"id": "run-for-hello"})

    def test_conditional_state_and_frame_requests(self):
        authorization = self.login()
        for path in ("/api/state", "/api/frame/" + "a" * 32):
            status, headers, body = self.request("GET", path, Authorization=authorization)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            status, _, body = self.request("GET", path, Authorization=authorization,
                                           **{"If-None-Match": headers["ETag"]})
            self.assertEqual((status, body), (304, b""))


if __name__ == "__main__":
    unittest.main()
