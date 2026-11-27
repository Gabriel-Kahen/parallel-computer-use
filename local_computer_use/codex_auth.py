"""Sign in with ChatGPT for a local app using the documented OAuth flow."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import http.client
import json
import os
import queue
import re
import secrets
import stat
import threading
import time
import uuid
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit


_AUTH_HOST = "auth.openai.com"
_ISSUER = f"https://{_AUTH_HOST}"
_AUTHORIZE = "/api/accounts/authorize"
_TOKEN = "/api/accounts/oauth/token"
_DISCOVERY = "/.well-known/openid-configuration"
_JWKS = "/.well-known/jwks.json"
_REVOCATION = "/api/accounts/oauth/revoke"
_CALLBACK = "/auth/callback"
_RESOURCE = "https://api.openai.com/v1"
_DYNAMIC_CLIENT = "dynamic_agent_client"
_APP_NAME = "Local Computer Use"
_REQUEST_SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
_PLAN_SCOPES = {"offline_access", "resource.invoke", "chatgpt.tokens.use.direct"}
_TOKEN_LIMIT = 16 * 1024
_BODY_LIMIT = 128 * 1024
_UNUSABLE_REFRESH = {
    "invalid_grant", "invalid_refresh_token", "token_expired",
    "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused",
}
_CREDENTIAL_FIELDS = {"access_token", "refresh_token", "id_token", "scopes", "expires_at", "refresh_at"}


class CodexAuthError(RuntimeError):
    """A sign-in, token refresh, or local credential store operation failed."""


class _OpenAIError(CodexAuthError):
    def __init__(self, status: int, detail: str = ""):
        self.status = status
        self.detail = detail
        super().__init__(f"ChatGPT authorization failed (HTTP {status}).")


class _PlanPermissionError(CodexAuthError):
    def __init__(self):
        super().__init__("Sign in with ChatGPT and enable ChatGPT plan usage.")


@dataclass(frozen=True)
class Account:
    client_id: str
    email: str
    signed_in: bool
    expires_at: float | None


def _opaque(value: object, limit: int = _TOKEN_LIMIT) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit and value.isascii() and all(
        "!" <= character <= "~" for character in value
    )


def _issued(client_id: object) -> str:
    if not isinstance(client_id, str) or client_id == _DYNAMIC_CLIENT or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._~-]{0,255}", client_id,
    ):
        raise CodexAuthError("A valid issued ChatGPT client ID is required.")
    return client_id


def _account_name(client_id: str) -> str:
    key = hashlib.sha256(_issued(client_id).encode("ascii")).hexdigest()
    return f"account-{key}.json"


def _body_object(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise CodexAuthError("ChatGPT authorization returned invalid data.") from None
    if not isinstance(data, dict):
        raise CodexAuthError("ChatGPT authorization returned invalid data.")
    return data


def _request(path: str, fields: dict[str, str] | None = None) -> dict:
    connection = http.client.HTTPSConnection(_AUTH_HOST, timeout=8)
    method = "GET" if fields is None else "POST"
    headers = {"Accept": "application/json", "Connection": "close"}
    body = None
    if fields is not None:
        body = urlencode(fields).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        connection.request(method, path, body=body, headers=headers)
        with connection.getresponse() as response:
            raw = response.read(_BODY_LIMIT + 1)
            if len(raw) > _BODY_LIMIT or response.length:
                raise CodexAuthError("ChatGPT authorization returned invalid data.")
            if response.status != 200:
                try:
                    error = _body_object(raw).get("error")
                    if isinstance(error, dict):
                        error = error.get("code")
                except CodexAuthError:
                    error = None
                detail = error if isinstance(error, str) and error in _UNUSABLE_REFRESH else ""
                raise _OpenAIError(response.status, detail)
            return _body_object(raw) if raw else {}
    except (OSError, http.client.HTTPException):
        raise CodexAuthError("Could not reach the ChatGPT authorization service.") from None
    finally:
        connection.close()


def _jwt_library():
    try:
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        raise CodexAuthError("Install the codex-auth extra to sign in with ChatGPT.") from None
    return jwt, rsa


def _validate_identity(token: str, client_id: str, nonce: str) -> tuple[str, str]:
    jwt, rsa = _jwt_library()
    try:
        header = jwt.get_unverified_header(token)
        key_id = header.get("kid")
        if header.get("alg") != "RS256" or not _opaque(key_id, 512):
            raise ValueError("Unexpected ID token key")
        keys = _request(_JWKS).get("keys")
        (key_record,) = [
            key for key in keys if isinstance(key, dict) and key.get("kid") == key_id
            and key.get("kty") == "RSA" and key.get("use", "sig") == "sig"
            and key.get("alg", "RS256") == "RS256"
        ]
        key = jwt.PyJWK.from_dict(key_record, algorithm="RS256").key
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise ValueError("Unexpected ID token key")
        claims = jwt.decode(
            token, key, algorithms=["RS256"], issuer=_ISSUER, audience=client_id, leeway=30,
            options={"require": ["iss", "aud", "exp", "iat", "sub", "nonce"]},
        )
        received_nonce = claims["nonce"]
        subject = claims["sub"]
        audience = claims["aud"]
        authorized_party = claims.get("azp")
        if (
            not _opaque(received_nonce, 512) or not secrets.compare_digest(nonce, received_nonce)
            or not _opaque(subject, 512)
            or (authorized_party is not None and authorized_party != client_id)
            or (isinstance(audience, list) and len(audience) > 1 and authorized_party != client_id)
        ):
            raise ValueError("Unexpected ID token identity")
        email = claims.get("email", "")
        if not isinstance(email, str) or len(email) > 320 or not email.isprintable():
            email = ""
        return subject, email
    except (jwt.PyJWTError, TypeError, ValueError, OverflowError):
        raise CodexAuthError("ChatGPT did not return a verifiable identity.") from None


def _credentials(response: dict, old_scopes: list[str] | None = None) -> dict:
    lifetime = response.get("expires_in")
    access, refresh, token_type = (response.get(name) for name in ("access_token", "refresh_token", "token_type"))
    scope = response.get("scope")
    if scope is not None and (not isinstance(scope, str) or len(scope) > 1024):
        raise CodexAuthError("ChatGPT returned invalid authorization scopes.")
    scopes = scope.split() if scope is not None else (old_scopes or [])
    if not _PLAN_SCOPES.issubset(scopes):
        raise _PlanPermissionError()
    if (
        type(lifetime) is not int or not 0 < lifetime <= 86400
        or not _opaque(access) or not _opaque(refresh)
        or not isinstance(token_type, str) or token_type.lower() != "bearer"
    ):
        raise CodexAuthError("ChatGPT returned incomplete authorization credentials.")
    now = time.time()
    expires = now + lifetime
    earliest_refresh = response.get("earliest_refresh_at")
    if type(earliest_refresh) not in (int, float) or not now < earliest_refresh < expires:
        earliest_refresh = now
    return {
        "access_token": access,
        "refresh_token": refresh,
        "scopes": scopes,
        "expires_at": expires,
        "refresh_at": max(earliest_refresh, expires - min(60, lifetime / 4)),
    }


class _CallbackServer(HTTPServer):
    def get_request(self):
        client, address = super().get_request()
        client.settimeout(2)
        return client, address


def _browser_callback(open_browser, authorize, state, timeout):
    received = queue.Queue(maxsize=1)
    accepted = False

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            nonlocal accepted
            if self.headers.get_all("Host") != [authority] or len(self.path) > 20 * 1024:
                self._reply(400, "Invalid ChatGPT authorization callback.")
                return
            try:
                target = urlsplit(self.path)
                if target.path != _CALLBACK or target.scheme or target.netloc or target.fragment:
                    self._reply(404, "ChatGPT authorization callback was not found.")
                    return
                fields = parse_qs(target.query, keep_blank_values=True, max_num_fields=16, errors="strict")
                if accepted or any(len(values) != 1 for values in fields.values()):
                    raise ValueError("Callback was already handled or has repeated parameters")
                actual = fields.get("state", [""])[0]
                if not _opaque(actual, 128) or not secrets.compare_digest(actual, state):
                    raise ValueError("Callback state is invalid")
            except ValueError:
                self._reply(400, "Invalid ChatGPT authorization callback.")
                return
            accepted = True
            if "error" in fields:
                outcome = CodexAuthError("ChatGPT authorization was declined or unavailable.")
                self._reply(400, "ChatGPT authorization did not finish. Return to the app.")
            else:
                outcome = {key: values[0] for key, values in fields.items()}
                self._reply(200, "Return to Local Computer Use to finish verifying your sign-in.")
            received.put_nowait(outcome)

        def _reply(self, status, message):
            body = message.encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

    try:
        with _CallbackServer(("127.0.0.1", 0), Callback) as server:
            authority = f"127.0.0.1:{server.server_port}"
            redirect = f"http://{authority}{_CALLBACK}"
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
            )
            thread.start()
            try:
                if not open_browser(authorize(redirect)):
                    raise CodexAuthError("Could not open your browser for Sign in with ChatGPT.")
                result = received.get(timeout=timeout)
            finally:
                server.shutdown()
                thread.join()
    except queue.Empty:
        raise CodexAuthError("Sign in with ChatGPT timed out.") from None
    except (OSError, webbrowser.Error):
        raise CodexAuthError("Could not start Sign in with ChatGPT on this machine.") from None
    if isinstance(result, CodexAuthError):
        raise result
    return redirect, result


class CodexAuth:
    """CLI-ready authentication store for this app's own ChatGPT registrations."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    @contextmanager
    def _locked(self):
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = os.open(self.directory, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
            try:
                _check_permissions(os.fstat(directory), stat.S_ISDIR, 0o700)
                lock = os.open(
                    ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600,
                    dir_fd=directory,
                )
                try:
                    _check_permissions(os.fstat(lock), stat.S_ISREG, 0o600)
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    yield directory
                finally:
                    os.close(lock)
            finally:
                os.close(directory)
        except OSError:
            raise CodexAuthError("Could not access the private ChatGPT credential store.") from None

    def accounts(self) -> list[Account]:
        with self._locked() as directory:
            return [
                _account(_read(directory, name))
                for name in sorted(os.listdir(directory))
                if name.startswith("account-") and name.endswith(".json")
            ]

    def sign_in(
        self, client_id: str | None = None, *,
        open_browser=webbrowser.open, timeout: float = 180,
    ) -> Account:
        _jwt_library()
        if not 0 < timeout <= 900:
            raise ValueError("Sign-in timeout must be between zero and 900 seconds.")
        previous = None
        with self._locked() as directory:
            if client_id is not None:
                previous = _require_account(directory, client_id)
            host = _read(directory, "host.json")
            if host is None:
                host = {"id": uuid.uuid4().urn}
                _write(directory, "host.json", host)
            host_id = host.get("id")
            if not _opaque(host_id, 256):
                raise CodexAuthError("The saved ChatGPT host ID is invalid.")
        registration = client_id
        for attempt in range(2):
            issued_client, nonce, exchange = self._authorize(registration, host_id, previous, open_browser, timeout)
            try:
                response = _request(_TOKEN, exchange)
            except _OpenAIError as error:
                if error.detail != "invalid_grant" or attempt == 1:
                    raise
                registration = issued_client
            else:
                break
        credentials = _credentials(response)
        token = response.get("id_token")
        if not _opaque(token):
            raise CodexAuthError("ChatGPT did not return an identity token.")
        subject, email = _validate_identity(token, issued_client, nonce)
        if previous and subject != previous["subject"]:
            raise CodexAuthError("ChatGPT sign-in returned a different account identity.")
        record = {
            "issuer": _ISSUER, "client_id": issued_client, "subject": subject, "email": email,
            "id_token": token, **credentials,
        }
        with self._locked() as directory:
            filename = _account_name(issued_client)
            if previous != _read(directory, filename):
                raise CodexAuthError("The ChatGPT registration changed during sign-in. Start sign-in again.")
            _write(directory, filename, record)
        return _account(record)

    @staticmethod
    def _authorize(registration, host_id, previous, open_browser, timeout):
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())

        def authorize(redirect):
            fields = {
                "client_id": registration or _DYNAMIC_CLIENT,
                "ext_agent_host_id": host_id,
                "response_type": "code",
                "redirect_uri": redirect,
                "scope": _REQUEST_SCOPES,
                "resource": _RESOURCE,
                "state": state,
                "nonce": nonce,
                "code_challenge_method": "S256",
                "code_challenge": challenge.decode("ascii").rstrip("="),
            }
            if registration is None:
                fields["agent_name_hint"] = _APP_NAME
            elif previous and previous.get("id_token"):
                fields["id_token_hint"] = previous["id_token"]
            return f"{_ISSUER}{_AUTHORIZE}?{urlencode(fields)}"

        redirect, fields = _browser_callback(open_browser, authorize, state, timeout)
        returned_client = fields.get("client_id", registration)
        issued_client = _issued(returned_client)
        if registration is not None and issued_client != registration:
            raise CodexAuthError("ChatGPT sign-in returned a different account registration.")
        code = fields.get("code", "")
        if not _opaque(code, 8192):
            raise CodexAuthError("ChatGPT did not return a valid authorization code.")
        return issued_client, nonce, {
            "grant_type": "authorization_code", "client_id": issued_client, "code": code,
            "code_verifier": verifier, "redirect_uri": redirect, "resource": _RESOURCE,
        }

    def access_token(self, client_id: str) -> str:
        with self._locked() as directory:
            record = _require_account(directory, client_id)
            if not _account(record).signed_in:
                raise CodexAuthError("Sign in with ChatGPT before using this account.")
            if time.time() < record["refresh_at"]:
                return record["access_token"]
            try:
                response = _request(_TOKEN, {
                    "grant_type": "refresh_token", "client_id": client_id,
                    "refresh_token": record["refresh_token"], "resource": _RESOURCE,
                })
                credentials = _credentials(response, record["scopes"])
            except _OpenAIError as error:
                if error.detail in _UNUSABLE_REFRESH:
                    _clear(directory, record)
                    raise CodexAuthError("ChatGPT sign-in expired. Sign in again.") from None
                raise
            except _PlanPermissionError:
                _clear(directory, record)
                raise
            record.update(credentials)
            _write(directory, _account_name(client_id), record)
            return record["access_token"]

    def sign_out(self, client_id: str) -> bool:
        """Clear local credentials and report whether remote revocation was confirmed."""
        with self._locked() as directory:
            record = _require_account(directory, client_id)
            if not _account(record).signed_in:
                return True
            for attempt in range(2):
                try:
                    metadata = _request(_DISCOVERY)
                    if metadata.get("issuer") != _ISSUER or metadata.get("revocation_endpoint") != _ISSUER + _REVOCATION:
                        raise CodexAuthError("ChatGPT provided an unexpected revocation endpoint.")
                    _request(_REVOCATION, {
                        "token": record["refresh_token"], "token_type_hint": "refresh_token",
                        "client_id": client_id,
                    })
                except CodexAuthError as error:
                    if isinstance(error, _OpenAIError) and error.status < 500:
                        break
                    if attempt == 0:
                        time.sleep(0.25)
                else:
                    _clear(directory, record)
                    return True
            _clear(directory, record)
            return False


def _check_permissions(info, check_type, mode):
    if info.st_uid != os.getuid() or not check_type(info.st_mode) or stat.S_IMODE(info.st_mode) != mode:
        raise CodexAuthError("The ChatGPT credential store must be private and owned by this user.")


def _read(directory, filename):
    try:
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as file:
        _check_permissions(os.fstat(file.fileno()), stat.S_ISREG, 0o600)
        raw = file.read(_BODY_LIMIT + 1)
    if len(raw) > _BODY_LIMIT:
        raise CodexAuthError("The saved ChatGPT credential is invalid.")
    return _body_object(raw)


def _write(directory, filename, record):
    temporary = f".credential-{secrets.token_hex(12)}"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory,
    )
    try:
        with os.fdopen(descriptor, "wb") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(json.dumps(record, ensure_ascii=True, allow_nan=False).encode("ascii"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _account(record):
    if not isinstance(record, dict) or record.get("issuer") != _ISSUER or not _opaque(record.get("subject"), 512):
        raise CodexAuthError("The saved ChatGPT credential is invalid.")
    email = record.get("email", "")
    if not isinstance(email, str) or len(email) > 320 or (email and not email.isprintable()):
        raise CodexAuthError("The saved ChatGPT credential is invalid.")
    expires = record.get("expires_at")
    signed_in = bool(_CREDENTIAL_FIELDS.intersection(record))
    if signed_in and (
        not all(_opaque(record.get(field)) for field in ("access_token", "refresh_token", "id_token"))
        or type(expires) not in (int, float) or not 0 < expires < 2 ** 63
        or type(record.get("refresh_at")) not in (int, float) or not 0 < record["refresh_at"] <= expires
        or not isinstance(record.get("scopes"), list)
        or not all(isinstance(scope, str) for scope in record["scopes"])
        or not _PLAN_SCOPES.issubset(record["scopes"])
    ):
        raise CodexAuthError("The saved ChatGPT credential is invalid.")
    return Account(_issued(record.get("client_id")), email, signed_in, expires)


def _require_account(directory, client_id):
    record = _read(directory, _account_name(client_id))
    if record is None or _account(record).client_id != client_id:
        raise CodexAuthError("No saved ChatGPT registration matches that client ID.")
    return record


def _clear(directory, record):
    account_only = {key: record[key] for key in ("issuer", "subject", "client_id")}
    account_only["email"] = record.get("email", "")
    _write(directory, _account_name(record["client_id"]), account_only)
