import unittest

from ui.router import UI_PREFIX, legacy_router, router


class UiRoutePrefixTests(unittest.TestCase):
    def test_ui_lives_under_v1(self):
        self.assertEqual(UI_PREFIX, "/v1/ui")
        self.assertEqual(router.prefix, "/v1/ui")

    def test_routes_include_login_and_assets(self):
        paths = {route.path for route in router.routes}
        self.assertIn("/v1/ui/login", paths)
        self.assertIn("/v1/ui/", paths)
        self.assertIn("/v1/ui/assets/{name}", paths)
        self.assertIn("/v1/ui/auth/login", paths)
        self.assertIn("/v1/ui/gallery/file/{name}", paths)
        self.assertIn("/v1/ui/gallery/upload", paths)
        self.assertIn("/v1/ui/metrics", paths)
        self.assertIn("/v1/ui/saver/state", paths)
        self.assertIn("/v1/ui/users", paths)
        self.assertIn("/v1/ui/settings", paths)
        self.assertIn("/v1/ui/chats", paths)
        self.assertIn("/v1/ui/prefs", paths)
        self.assertIn("/v1/ui/backup", paths)
        self.assertIn("/v1/ui/backup.zip", paths)
        self.assertIn("/v1/ui/backup/restore", paths)
        self.assertIn("/v1/ui/stack-backup/plan", paths)
        self.assertIn("/v1/ui/stack-backup", paths)
        self.assertIn("/v1/ui/stack-backup/restore", paths)
        self.assertIn("/v1/ui/workspace/{chat_id}/folder", paths)
        self.assertIn("/v1/ui/workspace/{chat_id}/drafts", paths)
        self.assertIn("/v1/ui/workspace/{chat_id}/shell", paths)
        self.assertIn("/v1/ui/workspace/{chat_id}/lsp", paths)
        self.assertIn("/v1/ui/workspace/{chat_id}/grep", paths)
        self.assertIn("/v1/ui/workspace/{chat_id}/replace", paths)
        self.assertIn("/v1/ui/workspace/{chat_id}/clone", paths)
        self.assertIn("/v1/ui/workspace/{chat_id}/git", paths)
        self.assertIn("/v1/ui/workspace/{chat_id}/git/diff", paths)
        self.assertIn("/v1/ui/workspace/{chat_id}/git/log", paths)
        self.assertIn("/v1/ui/workspace/{chat_id}/history/restore-run", paths)
        self.assertIn("/v1/ui/workspace/{chat_id}/crop", paths)
        self.assertIn("/v1/ui/workspace/{chat_id}/punch", paths)
        self.assertIn("/v1/ui/workspace/{chat_id}/resize", paths)

    def test_legacy_ui_redirect_routes_exist(self):
        paths = {route.path for route in legacy_router.routes}
        self.assertIn("/ui", paths)
        self.assertIn("/ui/", paths)
        self.assertIn("/ui/{rest:path}", paths)

    def _dep_names(self, path):
        for route in router.routes:
            if route.path == path:
                return [dep.call.__name__ for dep in route.dependant.dependencies if dep.call]
        self.fail(f"missing route {path}")

    def test_restart_and_update_require_admin(self):
        self.assertIn("require_ui_admin", self._dep_names("/v1/ui/restart"))
        self.assertIn("require_ui_admin", self._dep_names("/v1/ui/update"))
        self.assertIn("require_ui_admin", self._dep_names("/v1/ui/update/log"))
        self.assertIn("require_ui_admin", self._dep_names("/v1/ui/settings"))
        self.assertNotIn("require_ui_admin", self._dep_names("/v1/ui/gpu"))
        self.assertIn("require_ui_user", self._dep_names("/v1/ui/gpu"))
        self.assertIn("require_ui_user", self._dep_names("/v1/ui/backup"))
        self.assertIn("require_ui_user", self._dep_names("/v1/ui/backup.zip"))
        self.assertIn("require_ui_user", self._dep_names("/v1/ui/backup/restore"))
        self.assertNotIn("require_ui_admin", self._dep_names("/v1/ui/backup"))
        self.assertNotIn("require_ui_admin", self._dep_names("/v1/ui/backup.zip"))
        self.assertNotIn("require_ui_admin", self._dep_names("/v1/ui/backup/restore"))
        self.assertIn("require_ui_admin", self._dep_names("/v1/ui/stack-backup/plan"))
        self.assertIn("require_ui_admin", self._dep_names("/v1/ui/stack-backup"))
        self.assertIn("require_ui_admin", self._dep_names("/v1/ui/stack-backup/restore"))

    def test_saver_state_is_not_session_gated(self):
        deps = self._dep_names("/v1/ui/saver/state")
        self.assertNotIn("require_ui_user", deps)
        self.assertNotIn("require_ui_admin", deps)


if __name__ == "__main__":
    unittest.main()
