"""Console chat store keeps Chat and Code conversations separate."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ui import chats, workspace
from ui.chats import drop_duplicate_media_replies, normalize_store


class ChatStoreNormalizeTests(unittest.TestCase):
    def test_keeps_last_chat_per_mode(self):
        store = normalize_store(
            {
                "activeId": "c1",
                "chats": [
                    {"id": "c1", "mode": "chat", "title": "Hi", "messages": []},
                    {"id": "p1", "mode": "code", "title": "Page", "messages": []},
                ],
                "lastByMode": {"chat": "c1", "code": "p1"},
            }
        )
        self.assertEqual(store["lastByMode"], {"chat": "c1", "code": "p1"})
        self.assertEqual([c["mode"] for c in store["chats"]], ["chat", "code"])

    def test_ignores_install_epoch_on_payload(self):
        store = normalize_store(
            {
                "epoch": "not-a-chat",
                "chats": [
                    {"id": "c1", "mode": "chat", "title": "Hi", "messages": []},
                ],
            }
        )
        self.assertNotIn("epoch", store)
        self.assertEqual([chat["id"] for chat in store["chats"]], ["c1"])

    def test_keeps_chat_folder(self):
        store = normalize_store(
            {
                "chats": [
                    {"id": "c1", "mode": "chat", "title": "Hi", "folder": "Work", "messages": []},
                    {"id": "c2", "mode": "code", "title": "App", "folder": "Nope", "messages": []},
                ]
            }
        )
        by_id = {chat["id"]: chat for chat in store["chats"]}
        self.assertEqual(by_id["c1"]["folder"], "Work")
        self.assertNotIn("folder", by_id["c2"])

    def test_drops_last_id_when_mode_does_not_match(self):
        store = normalize_store(
            {
                "chats": [
                    {"id": "c1", "mode": "chat", "title": "Hi", "messages": []},
                ],
                "lastByMode": {"chat": "missing", "code": "c1"},
            }
        )
        self.assertEqual(store["lastByMode"], {"chat": "", "code": ""})

    def test_keeps_nested_parent_id(self):
        store = normalize_store(
            {
                "chats": [
                    {"id": "w1", "mode": "code", "title": "App", "messages": []},
                    {"id": "t1", "mode": "code", "parentId": "w1", "title": "Fix", "messages": []},
                ]
            }
        )
        by_id = {chat["id"]: chat for chat in store["chats"]}
        self.assertEqual(by_id["w1"]["parentId"], "")
        self.assertEqual(by_id["t1"]["parentId"], "w1")

    def test_drops_orphan_nested_chats(self):
        store = normalize_store(
            {
                "chats": [
                    {
                        "id": "t1",
                        "mode": "code",
                        "parentId": "missing",
                        "title": "Fix",
                        "messages": [],
                    }
                ]
            }
        )
        self.assertEqual(store["chats"], [])

    def test_strips_parent_id_on_chat_mode(self):
        store = normalize_store(
            {
                "chats": [
                    {"id": "c1", "mode": "chat", "parentId": "w1", "title": "Hi", "messages": []}
                ]
            }
        )
        self.assertEqual(store["chats"][0]["parentId"], "")

    def test_keeps_context_usage(self):
        store = normalize_store(
            {
                "chats": [
                    {
                        "id": "c1",
                        "mode": "chat",
                        "title": "Hi",
                        "messages": [],
                        "usage": {
                            "prompt_tokens": 1200,
                            "completion_tokens": 80,
                            "total_tokens": 1280,
                            "estimated": True,
                        },
                    }
                ]
            }
        )
        self.assertEqual(
            store["chats"][0]["usage"],
            {
                "prompt_tokens": 1200,
                "completion_tokens": 80,
                "total_tokens": 1280,
                "estimated": True,
            },
        )

    def test_drops_second_level_nesting(self):
        store = normalize_store(
            {
                "chats": [
                    {"id": "w1", "mode": "code", "messages": []},
                    {"id": "t1", "mode": "code", "parentId": "w1", "messages": []},
                    {"id": "t2", "mode": "code", "parentId": "t1", "messages": []},
                ]
            }
        )
        self.assertEqual({chat["id"] for chat in store["chats"]}, {"w1", "t1"})

    def test_drops_persist_picture_echo_of_the_same_video(self):
        name = "generated-20260919-042559-1585157.mp4"
        store = normalize_store(
            {
                "chats": [
                    {
                        "id": "c1",
                        "mode": "chat",
                        "messages": [
                            {"role": "user", "content": "generate a video of a bicycle"},
                            {
                                "role": "assistant",
                                "content": f"Here's the video.\n\n![](/v1/images/{name})",
                            },
                            {
                                "role": "assistant",
                                "content": (
                                    f"tabby-image-job: job-1\n\nHere's the picture.\n\n"
                                    f"![](/v1/images/{name})"
                                ),
                            },
                        ],
                    }
                ]
            }
        )
        texts = [item["content"] for item in store["chats"][0]["messages"]]
        self.assertEqual(len(texts), 2)
        self.assertIn("Here's the video.", texts[1])
        self.assertNotIn("Here's the picture.", "\n".join(texts))

    def test_keeps_console_video_when_persist_echo_came_first(self):
        name = "generated-20260919-042559-1585157.mp4"
        kept = drop_duplicate_media_replies(
            [
                {
                    "role": "assistant",
                    "content": f"tabby-image-job: job-1\n\nHere's the picture.\n\n![](/v1/images/{name})",
                },
                {
                    "role": "assistant",
                    "content": f"Here's the video.\n\n![](/v1/images/{name})",
                },
            ]
        )
        self.assertEqual(len(kept), 1)
        self.assertIn("Here's the video.", kept[0]["content"])
        self.assertNotIn("tabby-image-job:", kept[0]["content"])


class ChatStoreSaveWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        folder = Path(self.tmp.name)
        chats.set_chats_dir(folder / "chats")
        workspace.set_workspaces_dir(folder / "ws")

    def tearDown(self):
        chats.set_chats_dir(None)
        workspace.set_workspaces_dir(None)
        self.tmp.cleanup()

    def test_dropping_nested_chat_keeps_workspace_files(self):
        workspace.write_text("u", "w1", "index.html", "<p>hi</p>")
        chats.save_store(
            "u",
            {
                "activeId": "w1",
                "chats": [
                    {
                        "id": "w1",
                        "mode": "code",
                        "title": "App",
                        "messages": [{"role": "user", "content": "x"}],
                    },
                    {
                        "id": "t1",
                        "mode": "code",
                        "parentId": "w1",
                        "title": "Fix",
                        "messages": [{"role": "user", "content": "y"}],
                    },
                ],
            },
        )
        chats.save_store(
            "u",
            {
                "activeId": "w1",
                "chats": [
                    {
                        "id": "w1",
                        "mode": "code",
                        "title": "App",
                        "messages": [{"role": "user", "content": "x"}],
                    }
                ],
            },
        )
        self.assertEqual(workspace.read_text("u", "w1", "index.html"), "<p>hi</p>")
        loaded = chats.load_store("u")
        self.assertEqual([chat["id"] for chat in loaded["chats"]], ["w1"])

    def test_stale_put_keeps_omitted_workspace_with_files(self):
        workspace.write_text("u", "w1", "index.html", "<title>Cafe Night</title><p>hi</p>")
        chats.save_store(
            "u",
            {
                "chats": [
                    {
                        "id": "w1",
                        "mode": "code",
                        "title": "Cafe",
                        "messages": [{"role": "user", "content": "x"}],
                    }
                ]
            },
        )
        chats.save_store(
            "u",
            {
                "chats": [
                    {
                        "id": "c1",
                        "mode": "chat",
                        "title": "Hi",
                        "messages": [{"role": "user", "content": "y"}],
                    }
                ]
            },
        )
        self.assertEqual(workspace.read_text("u", "w1", "index.html"), "<title>Cafe Night</title><p>hi</p>")
        loaded = chats.load_store("u")
        ids = {chat["id"] for chat in loaded["chats"]}
        self.assertIn("w1", ids)
        self.assertIn("c1", ids)

    def test_empty_leftover_dir_does_not_rehydrate(self):
        root = workspace.workspace_root("u", "w1", create=True, box=False)
        (root / "empty").mkdir()
        loaded = chats.load_store("u")
        self.assertEqual(loaded["chats"], [])

    def test_delete_workspace_then_put_does_not_restore(self):
        workspace.write_text("u", "w1", "index.html", "<title>Cafe Night</title><p>hi</p>")
        chats.save_store(
            "u",
            {
                "chats": [
                    {
                        "id": "w1",
                        "mode": "code",
                        "title": "Cafe",
                        "messages": [{"role": "user", "content": "x"}],
                    },
                    {
                        "id": "t1",
                        "mode": "code",
                        "parentId": "w1",
                        "title": "New chat",
                        "messages": [],
                    },
                ]
            },
        )
        workspace.delete_workspace("u", "w1")
        chats.forget_workspace("u", "w1")
        chats.save_store(
            "u",
            {
                "chats": [
                    {
                        "id": "c1",
                        "mode": "chat",
                        "title": "Hi",
                        "messages": [{"role": "user", "content": "y"}],
                    }
                ]
            },
        )
        loaded = chats.load_store("u")
        ids = {chat["id"] for chat in loaded["chats"]}
        self.assertNotIn("w1", ids)
        self.assertNotIn("t1", ids)
        self.assertIn("c1", ids)
        titles = {chat.get("title") for chat in loaded["chats"]}
        self.assertNotIn("Recovered workspace", titles)

    def test_html_orphan_folder_rehydrates(self):
        workspace.write_text("u", "w1", "index.html", "<title>Cafe Night</title><p>hi</p>")
        loaded = chats.load_store("u")
        by_id = {chat["id"]: chat for chat in loaded["chats"]}
        self.assertEqual(by_id["w1"]["title"], "Cafe Night")
        self.assertEqual(by_id["w1"]["mode"], "code")

    def test_image_only_orphan_folder_does_not_rehydrate(self):
        root = workspace.workspace_root("u", "w1", create=True, box=False)
        (root / "images").mkdir()
        (root / "images" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (root / "images" / "header.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        loaded = chats.load_store("u")
        self.assertEqual(loaded["chats"], [])
        titles = {chat.get("title") for chat in loaded["chats"]}
        self.assertNotIn("Recovered workspace", titles)

    def test_deleted_chat_image_folder_does_not_rehydrate(self):
        root = workspace.workspace_root("u", "c1", create=True, box=False)
        (root / "images").mkdir()
        (root / "images" / "generated.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        chats.save_store(
            "u",
            {
                "chats": [
                    {
                        "id": "c1",
                        "mode": "chat",
                        "title": "Harbor",
                        "messages": [{"role": "user", "content": "harbor at dusk"}],
                    }
                ]
            },
        )
        chats.save_store("u", {"chats": []})
        loaded = chats.load_store("u")
        self.assertEqual(loaded["chats"], [])

    def test_saved_recovered_image_only_is_dropped(self):
        root = workspace.workspace_root("u", "w1", create=True, box=False)
        (root / "images").mkdir()
        (root / "images" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        chats.save_store(
            "u",
            {
                "chats": [
                    {
                        "id": "w1",
                        "mode": "code",
                        "title": "Recovered workspace",
                        "messages": [
                            {
                                "role": "assistant",
                                "origin": "server",
                                "content": "Here are the 2 pictures.",
                            }
                        ],
                    },
                    {
                        "id": "t1",
                        "mode": "code",
                        "parentId": "w1",
                        "title": "New chat",
                        "messages": [],
                    },
                ]
            },
        )
        loaded = chats.load_store("u")
        self.assertEqual(loaded["chats"], [])
        self.assertFalse((root / "images" / "logo.png").is_file())

    def test_is_code_chat_matches_store_mode(self):
        chats.save_store(
            "u",
            {
                "chats": [
                    {
                        "id": "c1",
                        "mode": "chat",
                        "title": "Harbor",
                        "messages": [{"role": "user", "content": "harbor"}],
                    },
                    {
                        "id": "w1",
                        "mode": "code",
                        "title": "Cafe",
                        "messages": [{"role": "user", "content": "logo"}],
                    },
                ]
            },
        )
        self.assertFalse(chats.is_code_chat("u", "c1"))
        self.assertTrue(chats.is_code_chat("u", "w1"))
        self.assertFalse(chats.is_code_chat("u", "missing"))


if __name__ == "__main__":
    unittest.main()
