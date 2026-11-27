import base64
import hashlib
import http.client
import json
import stat
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from parallel_computer_use import codex_auth
from parallel_computer_use.codex_auth import CodexAuth, CodexAuthError

try:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:
    jwt = None


ISSUER = "https://auth.openai.com"
CLIENT_ID = "oaiapp_fixture"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"


def token_set(**overrides):
    return {
        "access_token": "fixture-access", "refresh_token": "fixture-refresh",
        "token_type": "Bearer", "expires_in": 3600, "scope": SCOPES, **overrides,
    }


@unittest.skipUnless(jwt, "Install the codex-auth extra to exercise signed authorization fixtures")
class CodexAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwks = {
            "keys": [
                {
                    **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.signing_key.public_key())),
                    "kid": "fixture-key", "alg": "RS256", "use": "sig",
                },
            ],
        }

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "codex-auth"
        self.auth = CodexAuth(self.directory)
        self.authorizations = []
        self.exchanges = []
        self.claim_overrides = {}
        self.token_overrides = {}
        self.signer = self.signing_key
        self.key_id = "fixture-key"
        self.refresh_attempts = []
        self.unusable_refresh = False
        self.reject_first_exchange = False
        self.revocations = []
        self.revocation_status = 200
        self.request = patch.object(codex_auth, "_request", side_effect=self.openai_fixture)
        self.request.start()
        self.addCleanup(self.request.stop)

    def callback(self, redirect, fields, *, host=None):
        target = urlsplit(redirect)
        self.assertEqual((target.scheme, target.hostname, target.path), ("http", "127.0.0.1", "/auth/callback"))
        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=2)
        try:
            headers = {} if host is None else {"Host": host}
            connection.request("GET", f"{target.path}?{urlencode(fields, doseq=True)}", headers=headers)
            with connection.getresponse() as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                self.assertNotIn(b"fixture-code", response.read(4096))
                return response.status
        finally:
            connection.close()

    def browser(self, authorization_url, *, client_id=CLIENT_ID, declined=False, adversarial=False):
        target = urlsplit(authorization_url)
        self.assertEqual(f"{target.scheme}://{target.netloc}{target.path}", ISSUER + "/api/accounts/authorize")
        query = {key: values[0] for key, values in parse_qs(target.query).items()}
        self.authorizations.append(query)
        redirect = query["redirect_uri"]
        if declined:
            self.assertEqual(self.callback(redirect, {"state": query["state"], "error": "access_denied"}), 400)
            return True
        answer = {"state": query["state"], "code": f"fixture-code-{len(self.authorizations)}"}
        if client_id is not None:
            answer["client_id"] = client_id
        if adversarial:
            self.assertEqual(self.callback(redirect, {**answer, "state": "incorrect"}), 400)
            self.assertEqual(self.callback(redirect, {**answer, "state": ["incorrect", query["state"]]}), 400)
            self.assertEqual(self.callback(redirect, answer, host="example.test"), 400)
        self.assertEqual(self.callback(redirect, answer), 200)
        return True

    def openai_fixture(self, path, fields=None):
        if path == "/.well-known/jwks.json":
            self.assertIsNone(fields)
            return self.jwks
        if path == "/.well-known/openid-configuration":
            self.assertIsNone(fields)
            return {"issuer": ISSUER, "revocation_endpoint": ISSUER + "/api/accounts/oauth/revoke"}
        if path == "/api/accounts/oauth/revoke":
            self.revocations.append(fields)
            if self.revocation_status != 200:
                raise codex_auth._OpenAIError(self.revocation_status)
            return {}
        self.assertEqual(path, "/api/accounts/oauth/token")
        if fields["grant_type"] == "refresh_token":
            self.refresh_attempts.append(fields)
            if self.unusable_refresh:
                raise codex_auth._OpenAIError(400, "refresh_token_reused")
            time.sleep(0.02)
            rotated = token_set(access_token="rotated-access", refresh_token="rotated-refresh")
            del rotated["scope"]
            return rotated
        self.exchanges.append(fields)
        if self.reject_first_exchange and len(self.exchanges) == 1:
            raise codex_auth._OpenAIError(400, "invalid_grant")
        request = self.authorizations[-1]
        now = int(time.time())
        claims = {
            "iss": ISSUER, "aud": fields["client_id"], "nonce": request["nonce"],
            "sub": "fixture-subject", "email": "fixture@example.test", "iat": now, "exp": now + 300,
            **self.claim_overrides,
        }
        token = jwt.encode(claims, self.signer, algorithm="RS256", headers={"kid": self.key_id})
        return token_set(id_token=token, **self.token_overrides)

    def sign_in(self, client_id=None):
        return self.auth.sign_in(client_id, open_browser=self.browser, timeout=2)

    def expire(self):
        with self.auth._locked() as directory:
            filename = codex_auth._account_name(CLIENT_ID)
            record = codex_auth._read(directory, filename)
            record["expires_at"] = time.time() - 1
            record["refresh_at"] = record["expires_at"] - 60
            codex_auth._write(directory, filename, record)

    def test_registered_client_pkce_state_nonce_and_private_storage(self):
        account = self.auth.sign_in(
            open_browser=lambda url: self.browser(url, adversarial=True), timeout=2,
        )
        self.assertEqual((account.email, account.client_id, account.signed_in), ("fixture@example.test", CLIENT_ID, True))
        self.assertEqual(codex_auth.CodexAuth(self.directory).accounts(), [account])
        self.assertEqual(self.auth.access_token(CLIENT_ID), "fixture-access")
        authorization, = self.authorizations
        exchange, = self.exchanges
        self.assertEqual(authorization["client_id"], "dynamic_agent_client")
        self.assertEqual(authorization["agent_name_hint"], "Parallel Computer Use")
        self.assertTrue(authorization["ext_agent_host_id"].startswith("urn:uuid:"))
        self.assertEqual(authorization["scope"], SCOPES)
        self.assertEqual(authorization["response_type"], "code")
        self.assertEqual(authorization["code_challenge_method"], "S256")
        self.assertNotEqual(authorization["state"], authorization["nonce"])
        self.assertEqual(exchange["client_id"], CLIENT_ID)
        self.assertEqual(exchange["resource"], "https://api.openai.com/v1")
        self.assertEqual(exchange["redirect_uri"], authorization["redirect_uri"])
        self.assertNotIn("client_secret", exchange)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(exchange["code_verifier"].encode()).digest())
        self.assertEqual(authorization["code_challenge"], challenge.decode().rstrip("="))
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        for record in self.directory.iterdir():
            self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
        self.assertNotIn("fixture-access", repr(account))

    def test_reauthorization_preserves_identity_and_host(self):
        first = self.sign_in()
        signed_in = self.sign_in(CLIENT_ID)
        self.assertEqual(first.client_id, signed_in.client_id)
        initial, second = self.authorizations
        self.assertEqual(initial["ext_agent_host_id"], second["ext_agent_host_id"])
        self.assertNotEqual(initial["state"], second["state"])
        self.assertEqual(second["client_id"], CLIENT_ID)
        self.assertIn("id_token_hint", second)
        self.assertNotIn("agent_name_hint", second)
        self.claim_overrides["sub"] = "different-verified-subject"
        with self.assertRaisesRegex(CodexAuthError, "different account identity"):
            self.sign_in(CLIENT_ID)
        self.assertEqual(self.auth.accounts(), [signed_in])

    def test_durable_state_uses_registration_id_when_emails_match(self):
        first = self.sign_in()
        second = self.auth.sign_in(open_browser=lambda url: self.browser(url, client_id="oaiapp_second"), timeout=2)
        self.assertEqual(first.email, second.email)
        self.assertCountEqual(self.auth.accounts(), [first, second])

    def test_invalid_grant_restarts_once_with_issued_client_and_fresh_state(self):
        self.reject_first_exchange = True
        self.sign_in()
        first, second = self.authorizations
        self.assertEqual(first["client_id"], "dynamic_agent_client")
        self.assertEqual(second["client_id"], CLIENT_ID)
        self.assertNotIn("agent_name_hint", second)
        self.assertNotEqual(first["state"], second["state"])
        self.assertNotEqual(first["code_challenge"], second["code_challenge"])
        self.assertNotEqual(self.exchanges[0]["code"], self.exchanges[1]["code"])

    def test_rejects_invalid_id_token_claims_or_signatures(self):
        cases = (
            ({"nonce": "wrong-nonce"}, self.signing_key, "fixture-key"),
            ({"aud": "other-client"}, self.signing_key, "fixture-key"),
            ({"aud": [CLIENT_ID, "other-client"]}, self.signing_key, "fixture-key"),
            ({"iss": "https://example.test"}, self.signing_key, "fixture-key"),
            ({"exp": int(time.time()) - 100}, self.signing_key, "fixture-key"),
            ({}, self.other_key, "fixture-key"),
            ({}, self.signing_key, "unknown-key"),
        )
        for claims, self.signer, self.key_id in cases:
            with self.subTest(claims=claims, kid=self.key_id):
                self.claim_overrides = claims
                with self.assertRaisesRegex(CodexAuthError, "verifiable identity"):
                    self.sign_in()
        self.assertEqual(self.auth.accounts(), [])

    def test_declined_scope_failure_and_wrong_registration_leave_credentials_empty(self):
        with self.assertRaisesRegex(CodexAuthError, "declined"):
            self.auth.sign_in(open_browser=lambda url: self.browser(url, declined=True), timeout=2)
        self.assertEqual(self.exchanges, [])
        self.token_overrides["scope"] = "openid profile email"
        with self.assertRaisesRegex(CodexAuthError, "enable ChatGPT plan usage"):
            self.sign_in()
        self.assertEqual(self.auth.accounts(), [])
        self.token_overrides.clear()
        first = self.sign_in()
        with self.assertRaisesRegex(CodexAuthError, "different account registration"):
            self.auth.sign_in(
                CLIENT_ID, open_browser=lambda url: self.browser(url, client_id="oaiapp_other"), timeout=2,
            )
        self.assertEqual(self.auth.accounts(), [first])

    def test_renewal_rotates_the_token_once_and_keeps_granted_scopes(self):
        self.sign_in()
        self.expire()
        barrier = threading.Barrier(2)

        def get_token():
            barrier.wait(timeout=2)
            return CodexAuth(self.directory).access_token(CLIENT_ID)

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(lambda _: get_token(), range(2))), ["rotated-access"] * 2)
        self.assertEqual(self.refresh_attempts, [{
            "grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": "fixture-refresh",
            "resource": "https://api.openai.com/v1",
        }])
        account = self.auth.accounts()[0]
        self.assertTrue(account.signed_in)
        self.assertGreater(account.expires_at, time.time())
        self.assertTrue(self.auth.sign_out(CLIENT_ID))
        self.assertEqual(self.revocations, [{
            "token": "rotated-refresh", "token_type_hint": "refresh_token", "client_id": CLIENT_ID,
        }])

    def test_unusable_refresh_and_unconfirmed_revocation_clear_local_credentials(self):
        self.sign_in()
        self.expire()
        self.unusable_refresh = True
        with self.assertRaisesRegex(CodexAuthError, "Sign in again"):
            self.auth.access_token(CLIENT_ID)
        self.assertFalse(self.auth.accounts()[0].signed_in)
        self.sign_in(CLIENT_ID)
        self.revocation_status = 403
        self.assertFalse(self.auth.sign_out(CLIENT_ID))
        self.assertFalse(self.auth.accounts()[0].signed_in)
        with self.assertRaisesRegex(CodexAuthError, "Sign in with ChatGPT"):
            self.auth.access_token(CLIENT_ID)
        stored = next(self.directory.glob("account-*.json")).read_text()
        self.assertNotIn("fixture-refresh", stored)
        self.assertNotIn("fixture-access", stored)
        self.assertNotIn("id_token", stored)


class CredentialStorageTests(unittest.TestCase):
    def test_rejects_nonprivate_and_symlinked_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "public"
            directory.mkdir()
            directory.chmod(0o755)
            with self.assertRaisesRegex(CodexAuthError, "must be private"):
                CodexAuth(directory).accounts()
            directory.chmod(0o700)
            link = Path(temporary) / "alias"
            link.symlink_to(directory, target_is_directory=True)
            with self.assertRaises(CodexAuthError):
                CodexAuth(link).accounts()
            existing = directory / "account-fixture.json"
            existing.write_text("{}")
            existing.chmod(0o644)
            with self.assertRaisesRegex(CodexAuthError, "must be private"):
                CodexAuth(directory).accounts()


if __name__ == "__main__":
    unittest.main()
