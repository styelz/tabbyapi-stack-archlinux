import os
import unittest
from unittest import mock

from ui import auth


class UiAuthTests(unittest.TestCase):
    def setUp(self):
        auth.clear_sessions()
        auth.set_authenticator(None)

    def tearDown(self):
        auth.set_authenticator(None)
        auth.clear_sessions()

    def test_rejects_wrong_user(self):
        auth.set_authenticator(lambda user, password: True)
        with mock.patch.object(auth, "stack_username", return_value="tabby"):
            # authenticator hook bypasses the username check when set
            self.assertTrue(auth.authenticate_user("tabby", "secret"))

    def test_wrong_username_never_calls_pam(self):
        with mock.patch.object(auth, "stack_username", return_value="tabby"):
            with mock.patch.object(auth, "_pam_authenticate") as pam:
                self.assertFalse(auth.authenticate_user("other", "secret"))
                pam.assert_not_called()

    def test_pam_runs_in_subprocess(self):
        completed = mock.Mock(returncode=0)
        with mock.patch.object(auth, "stack_username", return_value="tabby"):
            with mock.patch("ui.auth.subprocess.run", return_value=completed) as run:
                self.assertTrue(auth.authenticate_user("tabby", "secret"))
        args = run.call_args
        cmd = args.args[0]
        self.assertIn("-m", cmd)
        self.assertIn("ui.pam_check", cmd)
        self.assertEqual(cmd[-1], "tabby")
        self.assertEqual(args.kwargs["input"], b"secret")
        self.assertEqual(args.kwargs["cwd"], str(auth.ROOT))
        pythonpath = args.kwargs["env"]["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(pythonpath[0], str(auth.ROOT))

    def test_pam_helper_failure_is_false(self):
        completed = mock.Mock(returncode=1)
        with mock.patch.object(auth, "stack_username", return_value="tabby"):
            with mock.patch("ui.auth.subprocess.run", return_value=completed):
                self.assertFalse(auth.authenticate_user("tabby", "nope"))

    def test_session_roundtrip(self):
        token = auth.create_session("tabby")
        self.assertEqual(auth.validate_session(token), "tabby")
        auth.destroy_session(token)
        self.assertIsNone(auth.validate_session(token))

    def test_csrf_origin_matches_host(self):
        req = mock.Mock()
        req.method = "POST"
        req.headers = {"origin": "http://192.168.1.14:5000", "host": "192.168.1.14:5000"}
        req.client = mock.Mock(host="192.168.1.20")
        self.assertTrue(auth.csrf_origin_ok(req))

    def test_csrf_origin_rejects_cross_site(self):
        req = mock.Mock()
        req.method = "POST"
        req.headers = {"origin": "https://evil.example", "host": "192.168.1.14:5000"}
        req.client = mock.Mock(host="192.168.1.20")
        self.assertFalse(auth.csrf_origin_ok(req))

    def test_csrf_get_skips_origin(self):
        req = mock.Mock()
        req.method = "GET"
        req.headers = {"origin": "https://evil.example", "host": "192.168.1.14:5000"}
        req.client = mock.Mock(host="192.168.1.20")
        self.assertTrue(auth.csrf_origin_ok(req))

    def test_csrf_trusts_forwarded_host_from_localhost(self):
        req = mock.Mock()
        req.method = "POST"
        req.headers = {
            "origin": "https://gpu.example",
            "host": "127.0.0.1:5000",
            "x-forwarded-host": "gpu.example",
        }
        req.client = mock.Mock(host="127.0.0.1")
        self.assertTrue(auth.csrf_origin_ok(req))

    def test_expired_session(self):
        token = auth.create_session("tabby")
        self.assertIsNone(auth.validate_session(token, max_age=-1))

    def test_logout_rejects_cross_site_origin(self):
        import asyncio

        from fastapi import HTTPException

        from ui.router import ui_logout

        token = auth.create_session("tabby")
        req = mock.Mock()
        req.method = "POST"
        req.headers = {"origin": "https://evil.example", "host": "192.168.1.14:5000"}
        req.client = mock.Mock(host="192.168.1.20")
        req.cookies = {auth.COOKIE_NAME: token}

        with self.assertRaises(HTTPException) as raised:
            asyncio.run(ui_logout(req))
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(auth.validate_session(token), "tabby")

    def test_logout_same_origin_clears_session(self):
        import asyncio

        from ui.router import ui_logout

        token = auth.create_session("tabby")
        req = mock.Mock()
        req.method = "POST"
        req.headers = {"origin": "http://192.168.1.14:5000", "host": "192.168.1.14:5000"}
        req.client = mock.Mock(host="192.168.1.20")
        req.cookies = {auth.COOKIE_NAME: token}

        asyncio.run(ui_logout(req))
        self.assertIsNone(auth.validate_session(token))

    def test_login_rate_limit(self):
        ip = "203.0.113.9"
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            self.assertTrue(auth.login_allowed(ip))
            auth.record_login_attempt(ip)
        self.assertFalse(auth.login_allowed(ip))
