"""HTTPS boundary tests use an ephemeral local TLS fixture and make no public requests."""

import base64
import hashlib
import os
import queue
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_computer_use._network import Denied, NetworkProxy, _resolve, validate_network_target
from local_computer_use.desktop import DesktopError, DesktopSession


def tunnel(proxy, domain):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(4)
    client.connect(str(proxy))
    client.sendall(
        f"CONNECT {domain}:443 HTTP/1.1\r\nHost: {domain}:443\r\n\r\n".encode("ascii")
    )
    response = b""
    while b"\r\n\r\n" not in response:
        part = client.recv(512)
        if not part:
            break
        response += part
    return client, response


class NetworkPolicyTest(unittest.TestCase):
    def test_exact_https_targets(self):
        for url in ("https://catalog.example/", "https://CATALOG.example:443/search"):
            self.assertEqual(validate_network_target(url, ("catalog.example",)), "catalog.example")
        for url in (
            "http://catalog.example", "https://catalog.example:8443",
            "https://catalog.example.evil.example", "https://shop.catalog.example",
            "https://catalog.example@other.example", "https://user@catalog.example",
            "https://127.0.0.1", "https://[::1]", "https://catalog.example\\outside.example",
            "https://catalog.example/\r\n", "file:///work/notes.txt",
        ):
            with self.subTest(url=url), self.assertRaises(Denied):
                validate_network_target(url, ("catalog.example",))

    def test_dns_rejects_host_private_link_local_and_mixed_answers(self):
        for address in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "100.64.0.1",
                        "192.168.0.1", "224.0.0.1", "198.18.1.1"):
            records = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)),
            ]
            with self.subTest(address=address), patch("socket.getaddrinfo", return_value=records):
                with self.assertRaises(Denied):
                    _resolve("catalog.example")

    def test_proxy_denies_unlisted_domain_before_dns_and_mismatched_tls_name(self):
        with tempfile.TemporaryDirectory() as directory:
            proxy = NetworkProxy(Path(directory) / "proxy", ("catalog.example",))
            try:
                with patch("local_computer_use._network._resolve") as resolver:
                    client, response = tunnel(proxy.path, "another.example")
                    with client:
                        self.assertTrue(response.startswith(b"HTTP/1.1 403"))
                    client, response = tunnel(proxy.path, "catalog.example")
                    with client:
                        self.assertTrue(response.startswith(b"HTTP/1.1 200"))
                        context = ssl.create_default_context()
                        with self.assertRaises((OSError, ssl.SSLError)):
                            context.wrap_socket(client, server_hostname="another.example")
                    resolver.assert_not_called()
            finally:
                proxy.close()


class TLSFixture:
    def __init__(self, directory):
        cert = directory / "cert.pem"
        key = directory / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=catalog.example", "-addext", "subjectAltName=DNS:catalog.example",
            "-keyout", str(key), "-out", str(cert),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=10)
        self.cert = cert
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self.connected = threading.Event()
        self.stopped = threading.Event()
        self.requests = queue.Queue()
        self.pending = {}
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stopped.is_set():
            try:
                client, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connected.set()
            threading.Thread(target=self.serve, args=(client,), daemon=True).start()

    def serve(self, client):
        try:
            client.settimeout(4)
            with client, self.context.wrap_socket(client, server_side=True) as tls:
                message = tls.recv(4096)
                head = message.split(b"\r\n", 1)[0]
                if head.startswith(b"GET /catalog"):
                    path = head.split(b" ")[1]
                    self.requests.put((path, message.lower()))
                    cookie = {
                        b"/catalog?first=alpha": b"Set-Cookie: lcu_alpha=1; Max-Age=3600; Secure; Path=/\r\n",
                        b"/catalog?first=beta": b"Set-Cookie: lcu_beta=1; Max-Age=3600; Secure; Path=/\r\n",
                    }.get(path, b"")
                    if cookie:
                        label = path.rsplit(b"=", 1)[-1]
                        tls.sendall(
                            b"HTTP/1.1 302 Found\r\nContent-Length: 0\r\nConnection: close\r\n"
                            b"Location: /catalog?check=" + label + b"\r\n" + cookie + b"\r\n"
                        )
                    else:
                        tls.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Length: 15\r\nConnection: close\r\n"
                            b"\r\nfixture catalog"
                        )
        except OSError:
            pass

    def close(self):
        self.stopped.set()
        self.listener.close()
        self.thread.join(timeout=0.5)

    def spki_pin(self):
        key = subprocess.run(["openssl", "x509", "-pubkey", "-noout", "-in", str(self.cert)],
                             check=True, capture_output=True, timeout=3).stdout
        der = subprocess.run(["openssl", "pkey", "-pubin", "-outform", "DER"],
                             input=key, check=True, capture_output=True, timeout=3).stdout
        return base64.b64encode(hashlib.sha256(der).digest()).decode("ascii")

    def wait_request(self, expected):
        deadline = time.monotonic() + 5
        while expected not in self.pending:
            path, request = self.requests.get(timeout=max(0, deadline - time.monotonic()))
            self.pending[path] = request
        return self.pending.pop(expected)


@unittest.skipUnless(shutil.which("openssl"), "openssl is needed for local TLS fixtures")
class TLSProxyTest(unittest.TestCase):
    def test_pinned_connect_delivers_a_synthetic_https_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = TLSFixture(root)
            proxy = NetworkProxy(root / "proxy", ("catalog.example",))
            try:
                with (patch("local_computer_use._network._resolve", return_value=("1.1.1.1",)) as resolve,
                      patch("local_computer_use._network._open_upstream",
                            side_effect=lambda _: socket.create_connection(("127.0.0.1", fixture.port), 4)) as dial):
                    client, response = tunnel(proxy.path, "catalog.example")
                    with client:
                        self.assertTrue(response.startswith(b"HTTP/1.1 200"))
                        context = ssl.create_default_context(cafile=str(fixture.cert))
                        with context.wrap_socket(client, server_hostname="catalog.example") as tls:
                            tls.sendall(b"GET /catalog HTTP/1.1\r\nHost: catalog.example\r\n\r\n")
                            self.assertIn(b"fixture catalog", tls.recv(4096))
                    resolve.assert_called_once_with("catalog.example")
                    dial.assert_called_once_with("1.1.1.1")
            finally:
                proxy.close()
                fixture.close()

    @unittest.skipUnless(os.environ.get("LCU_TEST_DESKTOP") == "1", "opt in with LCU_TEST_DESKTOP=1")
    def test_browser_reaches_only_qualified_tls_fixture_through_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = TLSFixture(Path(directory))
            try:
                with (patch("local_computer_use._network._resolve", return_value=("1.1.1.1",)) as resolve,
                      patch("local_computer_use._network._open_upstream",
                            side_effect=lambda _: socket.create_connection(("127.0.0.1", fixture.port), 4))):
                    with DesktopSession("browser", width=640, height=480,
                                        allowed_domains=("catalog.example",),
                                        start_url="https://catalog.example/catalog") as desktop:
                        self.assertTrue(desktop.png().startswith(b"\x89PNG"))
                        self.assertTrue(fixture.connected.wait(timeout=4))
                    self.assertTrue(resolve.call_args_list)
                    self.assertEqual({call.args[0] for call in resolve.call_args_list}, {"catalog.example"})
            finally:
                fixture.close()

    @unittest.skipUnless(os.environ.get("LCU_TEST_DESKTOP") == "1", "opt in with LCU_TEST_DESKTOP=1")
    def test_simultaneous_browser_profiles_have_distinct_cookies_and_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = TLSFixture(root)
            pin = fixture.spki_pin()

            class ChromiumFixture(DesktopSession):
                def _app_command(self, home, workspace):
                    command, files = super()._app_command(home, workspace)
                    if not any(str(arg).startswith("--user-data-dir=") for arg in command):
                        raise unittest.SkipTest("Requires a Chromium browser for the test TLS pin")
                    return [*command[:-1], f"--ignore-certificate-errors-spki-list={pin}", command[-1]], files

            def launch(profile, query):
                return ChromiumFixture("browser", width=640, height=480, browser_profile=root / profile,
                                       allowed_domains=("catalog.example",),
                                       start_url=f"https://catalog.example/catalog?{query}")

            try:
                with (patch("local_computer_use._network._resolve", return_value=("1.1.1.1",)),
                      patch("local_computer_use._network._open_upstream",
                            side_effect=lambda _: socket.create_connection(("127.0.0.1", fixture.port), 4))):
                    with launch("alpha", "first=alpha") as alpha, launch("beta", "first=beta") as beta:
                        with self.assertRaisesRegex(DesktopError, "already in use"):
                            launch("alpha", "busy")
                        for label in ("alpha", "beta"):
                            request = fixture.wait_request(f"/catalog?check={label}".encode("ascii"))
                            self.assertIn(f"\r\ncookie: lcu_{label}=1\r\n".encode("ascii"), request)
                    with launch("alpha", "persist"):
                        request = fixture.wait_request(b"/catalog?persist")
                        self.assertIn(b"\r\ncookie: lcu_alpha=1\r\n", request)
            finally:
                fixture.close()
