"""A per-browser HTTPS proxy. The guest reaches only its own proxy socket."""

import errno
import ipaddress
import re
import selectors
import socket
import struct
import sys
import threading
import time
from urllib.parse import urlsplit


GUEST_PROXY_PORT = 3128
GUEST_PROXY_SOCKET = "/run/pcu-proxy.sock"
_MAX_CONNECTIONS = 12
_MAX_HEADERS = 8192
_MAX_HELLO = 65536
_TIMEOUT = 5


class Denied(ValueError):
    pass


def _domain(name):
    if not isinstance(name, str):
        raise Denied("Expected an exact DNS domain")
    name = name.removesuffix(".").lower()
    if len(name) > 253 or not re.fullmatch(
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
        r"(?:[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?)", name, re.ASCII
    ):
        raise Denied("Expected an exact ASCII DNS domain")
    return name


def normalize_domains(domains):
    if not hasattr(domains, "__iter__") or isinstance(domains, (str, bytes)):
        raise Denied("Pass allowed domains as a collection of DNS names")
    return frozenset(_domain(domain) for domain in domains)


def validate_network_target(url, domains):
    """Validate an HTTPS start URL against an exact domain allowlist."""
    if (not isinstance(url, str) or not 1 <= len(url) <= 2048 or
            "\\" in url or any(not char.isprintable() or char.isspace() for char in url)):
        raise Denied("Invalid HTTPS start URL")
    try:
        target = urlsplit(url)
        port = target.port
    except ValueError as error:
        raise Denied("Invalid HTTPS start URL") from error
    if (target.scheme != "https" or target.username is not None or target.password is not None or
            port not in (None, 443) or not target.hostname):
        raise Denied("Start URLs must use HTTPS on port 443 without credentials")
    domain = _domain(target.hostname)
    if domain not in normalize_domains(domains):
        raise Denied("Start URL domain is not allowed")
    return domain


def _read_headers(peer):
    buffer = bytearray()
    deadline = time.monotonic() + _TIMEOUT
    while b"\r\n\r\n" not in buffer and len(buffer) < _MAX_HEADERS:
        data = _recv_until(peer, min(_MAX_HEADERS - len(buffer), 4096), deadline)
        if not data:
            raise Denied("Missing proxy request")
        buffer.extend(data)
    header, separator, payload = buffer.partition(b"\r\n\r\n")
    if not separator:
        raise Denied("Proxy request headers are too large")
    rows = header.decode("ascii").split("\r\n")
    first = re.fullmatch(r"CONNECT ([A-Za-z0-9.-]+):443 HTTP/1\.[01]", rows[0])
    if not first:
        raise Denied("Only HTTPS CONNECT on port 443 is allowed")
    domain = _domain(first.group(1))
    seen_host = False
    for line in rows[1:]:
        match = re.fullmatch(r"([A-Za-z-]+):[ \t]*([ -~]*)", line)
        if not match:
            raise Denied("Invalid proxy header")
        name, value = match.group(1).lower(), match.group(2).strip()
        if name == "host" and not seen_host and value.lower() == first.group(1).lower() + ":443":
            seen_host = True
        elif name not in {"proxy-connection", "connection", "user-agent"}:
            raise Denied("Unexpected proxy header")
    if not seen_host:
        raise Denied("CONNECT Host does not match its destination")
    return domain, payload


def _recv_until(peer, length, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Proxy handshake timed out")
    peer.settimeout(remaining)
    return peer.recv(length)


def _take(data, offset, prefix):
    if offset + prefix > len(data):
        raise Denied("Truncated TLS ClientHello")
    size = int.from_bytes(data[offset:offset + prefix], "big")
    offset += prefix
    if offset + size > len(data):
        raise Denied("Truncated TLS ClientHello")
    return data[offset:offset + size], offset + size


def _client_hostname(hello):
    offset = 34  # legacy version and random
    _, offset = _take(hello, offset, 1)  # session ID
    _, offset = _take(hello, offset, 2)  # cipher suites
    _, offset = _take(hello, offset, 1)  # compression methods
    extensions, offset = _take(hello, offset, 2)
    if offset != len(hello):
        raise Denied("Unexpected TLS ClientHello payload")
    offset, hostname = 0, None
    while offset < len(extensions):
        if offset + 4 > len(extensions):
            raise Denied("Truncated TLS extension")
        kind = int.from_bytes(extensions[offset:offset + 2], "big")
        content, offset = _take(extensions, offset + 2, 2)
        if kind == 0xfe0d:
            raise Denied("Encrypted ClientHello is not allowed")
        if kind != 0:
            continue
        names, end = _take(content, 0, 2)
        if hostname is not None or end != len(content) or names[:1] != b"\x00":
            raise Denied("Invalid TLS server name")
        raw, end = _take(names, 1, 2)
        if end != len(names):
            raise Denied("Invalid TLS server name")
        hostname = _domain(raw.decode("ascii"))
    return hostname


def _read_client_hello(peer, initial, domain):
    wire, handshake = bytearray(initial), bytearray()
    offset = 0
    deadline = time.monotonic() + _TIMEOUT
    while True:
        need = offset + 5
        while len(wire) < need:
            data = _recv_until(peer, need - len(wire), deadline)
            if not data:
                raise Denied("Missing TLS ClientHello")
            wire.extend(data)
        if wire[offset:offset + 2] != b"\x16\x03":
            raise Denied("Expected a TLS handshake record")
        record_size = int.from_bytes(wire[offset + 3:offset + 5], "big")
        need = offset + 5 + record_size
        if not 1 <= record_size <= 16384 or need > _MAX_HELLO:
            raise Denied("TLS ClientHello is too large")
        while len(wire) < need:
            data = _recv_until(peer, need - len(wire), deadline)
            if not data:
                raise Denied("Truncated TLS handshake")
            wire.extend(data)
        handshake.extend(wire[offset + 5:need])
        offset = need
        if len(handshake) >= 4:
            size = int.from_bytes(handshake[1:4], "big")
            if handshake[0] != 1 or size > _MAX_HELLO:
                raise Denied("Expected a TLS ClientHello")
            if len(handshake) >= 4 + size:
                if _client_hostname(handshake[4:4 + size]) != domain:
                    raise Denied("TLS server name does not match CONNECT destination")
                return wire


def _public_ipv4(address):
    ip = ipaddress.IPv4Address(address)
    if not ip.is_global or ip.is_multicast or ip.is_reserved:
        raise Denied("Resolved address is outside the public IPv4 policy")
    # Publicly numbered addresses belonging to this workstation are still local.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((str(ip), 0))
        except OSError as error:
            if error.errno != errno.EADDRNOTAVAIL:
                raise Denied("Could not rule out a local destination") from error
        else:
            raise Denied("Resolved address belongs to this workstation")
    return str(ip)


def _resolve(domain):
    records = socket.getaddrinfo(domain, 443, socket.AF_INET, socket.SOCK_STREAM)
    addresses = tuple(dict.fromkeys(_public_ipv4(record[4][0]) for record in records))
    if not addresses:
        raise Denied("No public IPv4 address found")
    return addresses


def _open_upstream(address):
    return socket.create_connection((address, 443), timeout=_TIMEOUT)


def _relay(left, right, stopped):
    remaining = 64 * 1024 * 1024
    until = time.monotonic() + 120
    with selectors.DefaultSelector() as selector:
        selector.register(left, selectors.EVENT_READ, right)
        selector.register(right, selectors.EVENT_READ, left)
        while remaining > 0 and not stopped.is_set() and time.monotonic() < until:
            ready = selector.select(5)
            if not ready:
                continue
            for key, _ in ready:
                data = key.fileobj.recv(min(65536, remaining))
                if not data:
                    return
                key.data.sendall(data)
                remaining -= len(data)


def _serve(listener, stopped, handler):
    client_slots = threading.BoundedSemaphore(_MAX_CONNECTIONS)

    def handle(peer):
        try:
            with peer:
                peer.settimeout(_TIMEOUT)
                handler(peer)
        except (OSError, ValueError):
            pass
        finally:
            client_slots.release()

    listener.settimeout(0.25)
    while not stopped.is_set():
        try:
            peer, _ = listener.accept()
        except TimeoutError:
            continue
        except OSError:
            return
        if client_slots.acquire(blocking=False):
            threading.Thread(target=handle, args=(peer,), daemon=True).start()
        else:
            peer.close()


class NetworkProxy:
    def __init__(self, path, domains):
        self.path = path
        self.domains = normalize_domains(domains)
        self.stopped = threading.Event()
        self._sockets = set()
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._listener.bind(str(path))
            path.chmod(0o600)
            self._listener.listen(_MAX_CONNECTIONS)
            self._thread = threading.Thread(
                target=_serve, args=(self._listener, self.stopped, self._connect), daemon=True
            )
            self._thread.start()
        except BaseException:
            self._listener.close()
            raise

    def _connect(self, peer):
        try:
            domain, initial = _read_headers(peer)
            if domain not in self.domains:
                raise Denied("CONNECT domain is not allowed")
        except ValueError:
            peer.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return
        peer.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        hello = _read_client_hello(peer, initial, domain)
        peer.settimeout(_TIMEOUT)
        addresses = _resolve(domain)
        for address in addresses[:4]:
            if self.stopped.is_set():
                return
            try:
                upstream = _open_upstream(_public_ipv4(address))
            except OSError:
                continue
            with upstream:
                with self._lock:
                    if self.stopped.is_set():
                        return
                    self._sockets.update((peer, upstream))
                try:
                    upstream.sendall(hello)
                    _relay(peer, upstream, self.stopped)
                finally:
                    with self._lock:
                        self._sockets.difference_update((peer, upstream))
            return

    def close(self):
        self.stopped.set()
        self._listener.close()
        with self._lock:
            for active in self._sockets:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self._thread.join(timeout=0.5)
        self.path.unlink(missing_ok=True)


def _bridge(path):
    stopped = threading.Event()

    def forward(peer):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
            upstream.settimeout(_TIMEOUT)
            upstream.connect(path)
            _relay(peer, upstream, stopped)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", GUEST_PROXY_PORT))
        listener.listen(_MAX_CONNECTIONS)
        _serve(listener, stopped, forward)


if __name__ == "__main__":
    if sys.argv[1:] == ["bridge"]:
        _bridge(GUEST_PROXY_SOCKET)
