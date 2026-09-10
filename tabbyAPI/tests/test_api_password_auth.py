import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from common import auth as api_auth
from ui import auth, users


class ApiPasswordAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        users.set_users_path(Path(self.tmp.name) / "ui_users.json")
        auth.clear_sessions()
        auth.set_authenticator(None)
        api_auth.clear_password_cache()
        self._prev_keys = api_auth.AUTH_KEYS
        self._prev_disable = api_auth.DISABLE_AUTH
        api_auth.AUTH_KEYS = api_auth.AuthKeys(api_key="yaml-api", admin_key="yaml-admin")
        api_auth.DISABLE_AUTH = False
        self._stack_patch = mock.patch.object(auth, "stack_username", return_value="tabby")
        self._stack_patch.start()
        self._pam_patch = mock.patch.object(auth, "_pam_authenticate", return_value=False)
        self._pam_patch.start()

    def tearDown(self):
        self._pam_patch.stop()
        self._stack_patch.stop()
        api_auth.AUTH_KEYS = self._prev_keys
        api_auth.DISABLE_AUTH = self._prev_disable
        api_auth.clear_password_cache()
        auth.set_authenticator(None)
        auth.clear_sessions()
        users.set_users_path(None)
        self.tmp.cleanup()

    def test_yaml_keys_still_work(self):
        self.assertEqual(api_auth.permission_for_token("yaml-admin"), "admin")
        self.assertEqual(api_auth.permission_for_token("yaml-api"), "api")
        self.assertIsNone(api_auth.permission_for_token("nope"))

    def test_verify_key_uses_constant_time_compare(self):
        keys = api_auth.AuthKeys(api_key=["k\u00e9y-one", "key-two"], admin_key="adm\u00efn")
        with mock.patch.object(
            api_auth.secrets, "compare_digest", wraps=api_auth.secrets.compare_digest
        ) as cmp:
            self.assertTrue(keys.verify_key("adm\u00efn", "admin_key"))
            self.assertTrue(keys.verify_key("adm\u00efn", "api_key"))
            self.assertTrue(keys.verify_key("k\u00e9y-one", "api_key"))
            self.assertTrue(keys.verify_key("key-two", "api_key"))
            self.assertFalse(keys.verify_key("key-two", "admin_key"))
            self.assertFalse(keys.verify_key("key-tw", "api_key"))
            self.assertFalse(keys.verify_key("", "api_key"))
            self.assertFalse(keys.verify_key("adm\u00efn", "other"))
            self.assertTrue(cmp.called)
            for call in cmp.call_args_list:
                self.assertIsInstance(call.args[0], bytes)
                self.assertIsInstance(call.args[1], bytes)

    def test_extra_user_password_is_api_key(self):
        with mock.patch.object(auth, "stack_username", return_value="tabby"):
            users.create_user("alice", "secret123")
            with mock.patch.object(auth, "_pam_authenticate", return_value=False) as pam:
                self.assertEqual(api_auth.permission_for_token("secret123"), "api")
                pam.assert_not_called()
                self.assertIsNone(api_auth.permission_for_token("wrongpass"))
                pam.assert_called()

    def test_linux_password_is_admin_key(self):
        auth.set_authenticator(lambda user, password: user == "tabby" and password == "pbp")
        with mock.patch.object(auth, "stack_username", return_value="tabby"):
            self.assertEqual(api_auth.permission_for_token("pbp"), "admin")
            self.assertIsNone(api_auth.permission_for_token("other"))

    def test_extra_user_password_is_not_admin(self):
        with mock.patch.object(auth, "stack_username", return_value="tabby"):
            users.create_user("alice", "secret123")
            self.assertEqual(api_auth.permission_for_token("secret123"), "api")

        async def _check():
            with self.assertRaises(HTTPException) as raised:
                await api_auth.check_admin_key(authorization="Bearer secret123")
            self.assertEqual(raised.exception.status_code, 401)
            self.assertEqual(
                await api_auth.check_api_key(authorization="Bearer secret123"),
                "Bearer secret123",
            )

        asyncio.run(_check())

    def test_linux_password_change_invalidates_cache(self):
        stamp = ["a"]
        auth.set_authenticator(lambda user, password: user == "tabby" and password == "pbp")
        with mock.patch.object(api_auth, "_linux_auth_stamp", side_effect=lambda: stamp[0]):
            with mock.patch.object(auth, "stack_username", return_value="tabby"):
                self.assertEqual(api_auth.permission_for_token("pbp"), "admin")
                stamp[0] = "b"
                auth.set_authenticator(lambda user, password: False)
                self.assertIsNone(api_auth.permission_for_token("pbp"))

    def test_yaml_keys_are_not_logged(self):
        keys = api_auth.AuthKeys(api_key="super-secret-api", admin_key="super-secret-admin")
        formatted = api_auth._format_api_keys(keys)
        self.assertNotIn("super-secret-api", formatted)
        self.assertNotIn("super-secret-admin", formatted)
        self.assertIn("1 extra API key", formatted)
        with mock.patch.object(auth, "stack_username", return_value="tabby"):
            users.create_user("alice", "secret123")
            self.assertEqual(api_auth.permission_for_token("secret123"), "api")
            users.set_password("alice", "newsecret")
            self.assertIsNone(api_auth.permission_for_token("secret123"))
            self.assertEqual(api_auth.permission_for_token("newsecret"), "api")
