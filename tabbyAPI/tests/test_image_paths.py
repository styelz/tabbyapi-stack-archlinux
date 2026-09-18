import unittest

from common.image_paths import (
    align_item_dests,
    dest_project_folder,
    dests_look_collapsed,
    download_pairs_from_job,
    ensure_site_prefix,
    generated_png_name_from_url,
    gpu_generated_file_missing,
    guess_output_path,
    image_download_command,
    image_download_note,
    image_poll_wait_command,
    image_running_note,
    image_running_shell_command,
    job_project_folders,
    living_download_pairs,
    tool_result_has_pngs,
    match_tool_name,
    resolve_output_paths,
    safe_rel_png_path,
    uniquify_rel_png_paths,
)


class ImagePathsTests(unittest.TestCase):
    def test_rejects_escape_and_absolute(self):
        self.assertEqual(safe_rel_png_path("images/logo.png"), "images/logo.png")
        self.assertEqual(safe_rel_png_path("../secret.png"), "images/generated.png")
        self.assertEqual(safe_rel_png_path("/tmp/x.png"), "images/generated.png")
        self.assertTrue(safe_rel_png_path("images/logo").endswith(".png"))
        self.assertEqual(safe_rel_png_path("audio/generated.wav"), "audio/generated.wav")
        self.assertEqual(safe_rel_png_path("videos/clip.mp4"), "videos/clip.mp4")
        self.assertEqual(
            safe_rel_png_path(
                "/home/pbp/Cursor/llm-test/pbptours/images/logo.png"
            ),
            "pbptours/images/logo.png",
        )
        self.assertEqual(
            safe_rel_png_path(
                r"C:\Users\pbp\Cursor\llm-test\pbptours\images\mercury.png"
            ),
            "pbptours/images/mercury.png",
        )
        self.assertEqual(
            safe_rel_png_path("/home/pbp/Cursor/llm-test/images/logo.png"),
            "images/logo.png",
        )
        self.assertEqual(safe_rel_png_path("v1/images/logo.png"), "images/logo.png")
        self.assertEqual(
            safe_rel_png_path("openai/v1/images/mars.png"), "images/mars.png"
        )
        self.assertEqual(
            dest_project_folder("v1/images/logo.png"),
            "",
        )
        aligned = align_item_dests(
            [
                {
                    "prompt": "logo",
                    "output_path": "v1/images/logo.png",
                },
                {
                    "prompt": "photograph of planet Mars",
                    "output_path": "v1/images/mars.png",
                },
            ]
        )
        self.assertEqual(aligned, ["images/logo.png", "images/mars.png"])
        items = [
            {
                "prompt": "logo that says Planet By Planet Tours",
                "output_path": "/home/pbp/Cursor/llm-test/pbptours/images/logo.png",
            },
            {
                "prompt": "photograph of planet Mercury",
                "output_path": "/home/pbp/Cursor/llm-test/pbptours/images/mercury.png",
            },
        ]
        self.assertEqual(
            resolve_output_paths(items),
            ["pbptours/images/logo.png", "pbptours/images/mercury.png"],
        )

    def test_download_pairs_and_command(self):
        item = type(
            "Item",
            (),
            {
                "output_path": "images/logo.png",
                "urls": ["https://gpu.example/v1/images/generated-1.png"],
            },
        )()
        job = type("Job", (), {"items": [item], "urls": [], "output_path": "images/"})()
        pairs = download_pairs_from_job(job)
        self.assertEqual(pairs[0][1], "images/logo.png")
        command = image_download_command(pairs)
        self.assertIn("curl -fsSL", command)
        self.assertIn("https://gpu.example/v1/images/generated-1.png", command)
        self.assertIn("-o 'images/logo.png'", command)
        self.assertIn("ls -l -- 'images/logo.png'", command)
        self.assertNotIn("python -c", command)
        self.assertNotIn("base64", command)
        self.assertNotIn("..", command)
        note = image_download_note(pairs)
        self.assertIn("images/logo.png", note)
        self.assertIn("https://gpu.example/v1/images/generated-1.png", note)
        wait = image_poll_wait_command(job, wait_s=20)
        self.assertTrue(wait.startswith("sleep 20;"))
        self.assertIn("ls -l --", wait)
        self.assertIn("'images/logo.png'", wait)
        self.assertNotIn("python -c", wait)
        running = image_running_shell_command(job, wait_s=20)
        self.assertNotIn("curl ", running)
        self.assertTrue(running.startswith("sleep 20;"))
        self.assertNotIn("https://gpu.example/v1/images/generated-1.png", running)
        note = image_running_note(job)
        self.assertIn("Still generating", note)
        self.assertIn("Do not invent", note)
        self.assertNotIn("Images are ready", note)
        self.assertNotIn("generated-1.png", note)

    def test_colliding_output_paths_are_uniquified(self):
        self.assertEqual(guess_output_path("qwen-image: a cafe logo"), "images/logo.png")
        self.assertEqual(guess_output_path("cosmic header banner"), "images/header.png")
        self.assertEqual(
            guess_output_path(
                "photograph of planet Mercury, cratered gray rocky world, "
                "wide photographic scene, no text, no letters, no logo, "
                "no user interface, no website, no browser, no mockup"
            ),
            "images/mercury.png",
        )
        collapsed = [
            {
                "prompt": "qwen-image: logo that says Planet By Planet Tours",
                "output_path": "images/logo.png",
            },
            {
                "prompt": (
                    "photograph of planet Mercury, cratered gray rocky world, "
                    "no text, no letters, no logo"
                ),
                "output_path": "images/logo-2.png",
            },
            {
                "prompt": (
                    "photograph of planet Neptune, deep blue ice giant, "
                    "no text, no letters, no logo"
                ),
                "output_path": "images/logo-3.png",
            },
        ]
        self.assertTrue(dests_look_collapsed(collapsed))
        self.assertEqual(ensure_site_prefix("images/logo.png", "pbptours"), "pbptours/images/logo.png")
        self.assertEqual(
            align_item_dests(collapsed, site_folder="pbptours"),
            [
                "pbptours/images/logo.png",
                "pbptours/images/mercury.png",
                "pbptours/images/neptune.png",
            ],
        )
        self.assertEqual(
            uniquify_rel_png_paths(["images/generated.png", "images/generated.png"]),
            ["images/generated.png", "images/generated-2.png"],
        )
        first = {"prompt": "elegant logo", "output_path": ""}
        second = {"prompt": "hero banner", "output_path": ""}
        self.assertEqual(
            resolve_output_paths([first, second]),
            ["images/logo.png", "images/header.png"],
        )
        item_a = type(
            "Item",
            (),
            {
                "output_path": "images/generated.png",
                "urls": ["https://gpu.example/v1/images/a.png"],
            },
        )()
        item_b = type(
            "Item",
            (),
            {
                "output_path": "images/generated.png",
                "urls": ["https://gpu.example/v1/images/b.png"],
            },
        )()
        job = type("Job", (), {"items": [item_a, item_b], "urls": [], "output_path": ""})()
        pairs = download_pairs_from_job(job)
        self.assertEqual(pairs[0][1], "images/generated.png")
        self.assertEqual(pairs[1][1], "images/generated-2.png")
        command = image_download_command(pairs)
        self.assertIn("curl -fsSL", command)
        self.assertIn("-o 'images/generated.png'", command)
        self.assertIn("-o 'images/generated-2.png'", command)
        self.assertIn("https://gpu.example/v1/images/a.png", command)
        self.assertIn("https://gpu.example/v1/images/b.png", command)
        self.assertEqual(image_download_command([("https://evil.example/'oops", "images/x.png")]), "")
        self.assertTrue(
            tool_result_has_pngs(
                "-rw-r--r-- 1 pbp pbp 204812 Aug 21 06:23 images/logo.png",
                ["images/logo.png"],
            )
        )
        self.assertFalse(
            tool_result_has_pngs(
                "ls: cannot access 'images/logo.png': No such file or directory",
                ["images/logo.png"],
            )
        )
        self.assertTrue(
            tool_result_has_pngs(
                "-rw-r--r-- 1 pbp pbp 204812 Aug 21 06:23 images/logo.png\n"
                "ls: cannot access 'images/extra.png': No such file or directory",
                ["images/logo.png"],
            )
        )

    def test_dest_project_folder_needs_a_site_prefix(self):
        """No folder is detected for a workspace-root images/ dest.

        This is the failure mode a broken site_folder() falls back to: every
        dest looks like plain images/x.png, so job_project_folders is empty
        and the cross-site guard in phrase_switch._job_matches_this_chat has
        nothing to compare against.
        """
        self.assertEqual(dest_project_folder("images/logo.png"), "")
        self.assertEqual(dest_project_folder("pbptours/images/logo.png"), "pbptours")
        item = type(
            "Item", (), {"output_path": "images/logo.png", "count": 1}
        )()
        job = type("Job", (), {"items": [item], "output_path": "images/logo.png"})()
        self.assertEqual(job_project_folders(job), set())
        site_item = type(
            "Item", (), {"output_path": "pbptours/images/logo.png", "count": 1}
        )()
        site_job = type(
            "Job",
            (),
            {"items": [site_item], "output_path": "pbptours/images/logo.png"},
        )()
        self.assertEqual(job_project_folders(site_job), {"pbptours"})

    def test_match_tool_name(self):
        self.assertEqual(match_tool_name(["Shell", "Read"], ["shell"]), "Shell")
        self.assertEqual(
            match_tool_name(["mcp_tabby-images_get_image_job"], ["get_image_job"]),
            "mcp_tabby-images_get_image_job",
        )
        self.assertEqual(
            match_tool_name(["mcp_tabby-images_get-image-job"], ["get_image_job"]),
            "mcp_tabby-images_get-image-job",
        )
        self.assertIsNone(match_tool_name(["Read"], ["shell"]))
        self.assertIsNone(
            match_tool_name(["kill_terminal"], ["terminal", "shell"])
        )
        self.assertEqual(
            match_tool_name(
                ["kill_terminal", "run_in_terminal"],
                ["terminal", "shell"],
            ),
            "run_in_terminal",
        )


class StdioSaverTests(unittest.TestCase):
    def test_parse_job_id(self):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "tools" / "images_mcp_stdio.py"
        spec = importlib.util.spec_from_file_location("images_mcp_stdio", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(
            module.parse_job_id("Job a83fe614-51f8-4d3e-b2ca-448af3a64504: queued"),
            "a83fe614-51f8-4d3e-b2ca-448af3a64504",
        )
        self.assertEqual(module.parse_job_id("job_id=abc-123 extra"), "abc-123")
        pairs = module.pairs_from_job(
            {
                "items": [
                    {
                        "output_path": "images/generated.png",
                        "urls": ["https://gpu.example/v1/images/a.png"],
                    },
                    {
                        "output_path": "images/generated.png",
                        "urls": ["https://gpu.example/v1/images/b.png"],
                    },
                ]
            }
        )
        self.assertEqual(pairs[0][1], "images/generated.png")
        self.assertEqual(pairs[1][1], "images/generated-2.png")
        rewritten = module.rewrite_generate_arguments(
            {
                "images": [
                    {
                        "prompt": "logo",
                        "output_path": "/home/pbp/Cursor/llm-test/pbptours/images/logo.png",
                    }
                ]
            }
        )
        self.assertEqual(
            rewritten["images"][0]["output_path"], "pbptours/images/logo.png"
        )
        self.assertEqual(
            module.safe_rel_png_path(
                "/home/pbp/Cursor/llm-test/images/header.png"
            ),
            "images/header.png",
        )

    def test_living_download_pairs_drops_missing_timestamped_urls(self):
        from pathlib import Path
        from unittest import mock

        dead = (
            "https://gpu.example/v1/images/generated-20260822-214723-251774.png?t=1"
        )
        self.assertEqual(
            generated_png_name_from_url(dead),
            "generated-20260822-214723-251774.png",
        )
        item = type(
            "Item",
            (),
            {"output_path": "images/logo.png", "urls": [dead], "count": 1},
        )()
        job = type(
            "Job",
            (),
            {"items": [item], "output_path": "images/logo.png", "urls": [dead]},
        )()
        with mock.patch("common.gpu_mode.generated_image_path", return_value=None):
            self.assertTrue(gpu_generated_file_missing(dead))
            self.assertEqual(living_download_pairs(job), [])
        with mock.patch(
            "common.gpu_mode.generated_image_path",
            return_value=Path("/tmp/generated-20260822-214723-251774.png"),
        ):
            self.assertFalse(gpu_generated_file_missing(dead))
            self.assertEqual(living_download_pairs(job), [(dead, "images/logo.png")])
        alias = "https://gpu.example/v1/images/generated-logo.png"
        self.assertFalse(gpu_generated_file_missing(alias))

    def test_spawn_saver_skips_detached_process(self):
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1] / "tools" / "images_mcp_stdio.py"
        ).read_text(encoding="utf-8")
        self.assertIn("0x08000000", source)
        self.assertNotIn("0x00000008 | 0x00000200", source)


if __name__ == "__main__":
    unittest.main()
