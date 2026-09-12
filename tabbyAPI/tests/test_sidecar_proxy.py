import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
import httpx

from sidecar import proxy as proxy_mod
from sidecar.app import create_app
from sidecar.backend_key import ensure_backend_key
from sidecar.supervise import sidecar_command, tabby_command


def _stub_backend(expected_key: str) -> FastAPI:
    stub = FastAPI()

    @stub.get("/health")
    def health():
        return {"status": "ok"}

    @stub.get("/v1/models")
    async def models(request: Request):
        auth = request.headers.get("authorization") or ""
        key = request.headers.get("x-api-key") or ""
        if expected_key not in auth and key != expected_key:
            return PlainTextResponse("nope", status_code=401)
        return {"object": "list", "data": [{"id": "gpt-4o"}]}

    @stub.post("/v1/chat/completions")
    async def chat(request: Request):
        auth = request.headers.get("authorization") or ""
        if expected_key not in auth:
            return PlainTextResponse("nope", status_code=401)
        body = await request.json()
        if body.get("stream"):

            async def sse():
                yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                yield b"data: [DONE]\n\n"

            return StreamingResponse(sse(), media_type="text/event-stream")
        return {
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "saw_backend_key": True,
        }

    @stub.post("/v1/completions")
    async def completions():
        return {"choices": [{"text": "ok"}]}

    @stub.post("/v1/embeddings")
    async def embeddings():
        return {"data": [{"embedding": [0.1]}]}

    return stub


class SidecarProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key_file = Path(self.tmp.name) / "backend.key"
        self.key = "internal-backend-key"
        os.environ["TABBY_BACKEND_KEY"] = self.key
        os.environ["SIDECAR_BACKEND_KEY_FILE"] = str(self.key_file)
        os.environ["TABBY_BACKEND_URL"] = "http://tabby.test"
        proxy_mod.reset_client()
        transport = httpx.ASGITransport(app=_stub_backend(self.key))
        self.backend = httpx.AsyncClient(transport=transport, base_url="http://tabby.test")
        proxy_mod.set_client(self.backend)
        from common import auth as api_auth

        self._prev_keys = api_auth.AUTH_KEYS
        self._prev_disable = api_auth.DISABLE_AUTH
        api_auth.DISABLE_AUTH = False
        api_auth.AUTH_KEYS = api_auth.AuthKeys(api_key="user-pass", admin_key="admin-pass")
        self.app = create_app(mount_stack=False, intercept_chat=False)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://sidecar.test",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.backend.aclose()
        proxy_mod.reset_client()
        from common import auth as api_auth

        api_auth.AUTH_KEYS = self._prev_keys
        api_auth.DISABLE_AUTH = self._prev_disable
        os.environ.pop("TABBY_BACKEND_KEY", None)
        os.environ.pop("SIDECAR_BACKEND_KEY_FILE", None)
        os.environ.pop("TABBY_BACKEND_URL", None)
        self.tmp.cleanup()

    async def test_health_and_models_forward(self):
        health = await self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")
        denied = await self.client.get("/v1/models")
        self.assertEqual(denied.status_code, 401)
        ok = await self.client.get("/v1/models", headers={"Authorization": "Bearer user-pass"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["data"][0]["id"], "gpt-4o")

    async def test_rejects_bad_user_key(self):
        resp = await self.client.get("/v1/models", headers={"Authorization": "Bearer nope"})
        self.assertEqual(resp.status_code, 401)

    async def test_swaps_user_key_for_backend_key(self):
        resp = await self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer user-pass"},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["saw_backend_key"])

    async def test_streams_sse_chunks(self):
        async with self.client.stream(
            "POST",
            "/v1/chat/completions",
            headers={"Authorization": "Bearer user-pass"},
            json={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
        ) as resp:
            self.assertEqual(resp.status_code, 200)
            text = "".join([chunk async for chunk in resp.aiter_text()])
        self.assertIn("hi", text)
        self.assertIn("[DONE]", text)

    async def test_completions_and_embeddings(self):
        headers = {"Authorization": "Bearer user-pass"}
        comp = await self.client.post("/v1/completions", headers=headers, json={"prompt": "x"})
        self.assertEqual(comp.status_code, 200)
        emb = await self.client.post("/v1/embeddings", headers=headers, json={"input": "x"})
        self.assertEqual(emb.status_code, 200)

    def test_backend_key_file_roundtrip(self):
        os.environ.pop("TABBY_BACKEND_KEY", None)
        first = ensure_backend_key(self.key_file)
        second = ensure_backend_key(self.key_file)
        self.assertEqual(first, second)
        self.assertEqual(self.key_file.read_text(encoding="utf-8").strip(), first)

    def test_supervise_commands_bind_localhost(self):
        with mock.patch("sidecar.supervise.backend_dir", return_value=Path("/tmp/tabby")):
            cmd = tabby_command("python")
        self.assertEqual(cmd[2], "--host")
        self.assertEqual(cmd[3], "127.0.0.1")
        self.assertEqual(cmd[4], "--port")
        self.assertEqual(cmd[5], "5001")
        self.assertEqual(sidecar_command("python"), ["python", "-m", "sidecar"])

    def test_child_env_puts_tabbyapi_on_pythonpath(self):
        from sidecar.paths import STACK_ROOT, TABBY_DIR
        from sidecar.supervise import child_env

        with mock.patch("sidecar.supervise.ensure_backend_key", return_value="k"):
            with mock.patch("sidecar.supervise.write_backend_tokens"):
                env = child_env("sidecar")
        parts = env["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(parts[0], str(STACK_ROOT))
        self.assertEqual(parts[1], str(TABBY_DIR))
        self.assertEqual(env["TABBY_PROCESS"], "sidecar")


class SidecarAuthEdgeTests(unittest.TestCase):
    def test_linux_password_is_accepted_then_backend_key_is_sent(self):
        from common import auth as api_auth
        from ui import auth, users

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        users.set_users_path(Path(tmp.name) / "ui_users.json")
        auth.clear_sessions()
        api_auth.clear_password_cache()
        prev_keys = api_auth.AUTH_KEYS
        prev_disable = api_auth.DISABLE_AUTH
        api_auth.AUTH_KEYS = api_auth.AuthKeys(api_key="yaml-api", admin_key="yaml-admin")
        api_auth.DISABLE_AUTH = False
        os.environ["TABBY_BACKEND_KEY"] = "internal-backend-key"
        self.addCleanup(lambda: os.environ.pop("TABBY_BACKEND_KEY", None))
        self.addCleanup(lambda: setattr(api_auth, "AUTH_KEYS", prev_keys))
        self.addCleanup(lambda: setattr(api_auth, "DISABLE_AUTH", prev_disable))
        auth.set_authenticator(lambda user, password: user == "tabby" and password == "pbp")
        with mock.patch.object(auth, "stack_username", return_value="tabby"):
            self.assertEqual(api_auth.permission_for_token("pbp"), "admin")
        self.assertEqual(api_auth.permission_for_token("internal-backend-key"), "admin")
        auth.set_authenticator(None)
        users.set_users_path(None)


class SidecarInterceptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.environ["TABBY_BACKEND_KEY"] = "internal-backend-key"
        os.environ["TABBY_BACKEND_URL"] = "http://tabby.test"

    def tearDown(self):
        os.environ.pop("TABBY_BACKEND_KEY", None)
        os.environ.pop("TABBY_BACKEND_URL", None)

    def _request(self):
        request = mock.Mock()
        request.headers = {}
        request.url.path = "/v1/chat/completions"
        request.client = mock.Mock(host="127.0.0.1")
        return request

    async def test_switch_phrase_does_not_forward(self):
        from sidecar.chat import handle_chat_completion

        forwarded = []

        async def fake_proxy(*_a, **_k):
            forwarded.append(True)
            raise AssertionError("switch must not hit Tabby")

        async def fake_image(*_a, **_k):
            return None

        with mock.patch("sidecar.chat.start_switch") as start:
            result = await handle_chat_completion(
                self._request(),
                {"messages": [{"role": "user", "content": "switch to qwen"}]},
                proxy_fn=fake_proxy,
                image_handler=fake_image,
                skip_occupancy=True,
            )
        start.assert_called_once_with("qwen")
        self.assertFalse(forwarded)
        self.assertIn("qwen", result.choices[0].message.content.lower())

    async def test_restart_phrase_does_not_forward(self):
        from sidecar.chat import handle_chat_completion

        async def fake_proxy(*_a, **_k):
            raise AssertionError("restart must not hit Tabby")

        async def fake_image(*_a, **_k):
            return None

        with mock.patch("sidecar.chat.start_restart", return_value=True):
            result = await handle_chat_completion(
                self._request(),
                {"messages": [{"role": "user", "content": "restart"}]},
                proxy_fn=fake_proxy,
                image_handler=fake_image,
                skip_occupancy=True,
            )
        self.assertIn("restart", result.choices[0].message.content.lower())

    async def test_image_handler_stays_on_sidecar(self):
        from common.phrase_switch import text_response
        from sidecar.chat import handle_chat_completion

        async def fake_proxy(*_a, **_k):
            raise AssertionError("image turn must stay on sidecar")

        async def fake_image(data, *_a, **_k):
            return text_response(data, "image-on-sidecar")

        with mock.patch("sidecar.chat.llm_is_ready", return_value=True):
            result = await handle_chat_completion(
                self._request(),
                {"messages": [{"role": "user", "content": "hello"}]},
                proxy_fn=fake_proxy,
                image_handler=fake_image,
                skip_occupancy=True,
            )
        self.assertEqual(result.choices[0].message.content, "image-on-sidecar")

    async def test_plain_chat_forwards(self):
        from sidecar.chat import handle_chat_completion

        seen = {}

        async def fake_proxy(request, path=None, body=None, **_k):
            seen["path"] = path
            seen["body"] = json.loads(body.decode("utf-8"))
            return {"ok": True}

        async def fake_image(*_a, **_k):
            return None

        with mock.patch("sidecar.chat.llm_is_ready", return_value=True):
            result = await handle_chat_completion(
                self._request(),
                {"messages": [{"role": "user", "content": "what is 2+2"}]},
                proxy_fn=fake_proxy,
                image_handler=fake_image,
                skip_occupancy=True,
            )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen["path"], "/v1/chat/completions")
        self.assertEqual(seen["body"]["messages"][0]["content"], "what is 2+2")

    async def test_occupancy_acquires_and_releases(self):
        from sidecar.chat import handle_chat_completion

        calls = []

        class FakeGate:
            def __init__(self, *_a, **_k):
                pass

            async def wait_until_acquired(self, *_a, **_k):
                calls.append("wait")

            async def release(self):
                calls.append("release")

        async def fake_proxy(*_a, **_k):
            calls.append("proxy")
            return {"ok": True}

        async def fake_image(*_a, **_k):
            return None

        with (
            mock.patch("ui.occupancy.StackGate", FakeGate),
            mock.patch("sidecar.chat.llm_is_ready", return_value=True),
        ):
            await handle_chat_completion(
                self._request(),
                {"messages": [{"role": "user", "content": "hello"}]},
                proxy_fn=fake_proxy,
                image_handler=fake_image,
            )
        self.assertEqual(calls, ["wait", "proxy", "release"])

    async def test_llama_mode_forwards_adapted_payload(self):
        from sidecar.chat import handle_chat_completion

        seen = {}

        async def fake_proxy(request, path=None, body=None, client=None, **_k):
            seen["path"] = path
            seen["body"] = json.loads(body.decode("utf-8"))
            seen["client"] = client
            return {"ok": True}

        async def fake_image(*_a, **_k):
            return None

        with (
            mock.patch("sidecar.chat.llm_is_ready", return_value=True),
            mock.patch("sidecar.settings.chat_backend_url", return_value="http://127.0.0.1:5002"),
            mock.patch("common.phrase_switch.gpu_is_llama", return_value=True),
            mock.patch("sidecar.proxy.get_client", return_value="llama-client"),
        ):
            result = await handle_chat_completion(
                self._request(),
                {
                    "messages": [{"role": "user", "content": "hello"}],
                    "model": "qwen",
                    "dry_multiplier": 1.5,
                },
                proxy_fn=fake_proxy,
                image_handler=fake_image,
                skip_occupancy=True,
            )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen["path"], "/v1/chat/completions")
        self.assertEqual(seen["body"]["model"], "gpt-4o")
        self.assertNotIn("dry_multiplier", seen["body"])
        self.assertNotIn("top_logprobs", seen["body"])
        self.assertEqual(seen["client"], "llama-client")

    async def test_forward_llm_chat_uses_forward_chat_for_console_standin(self):
        from types import SimpleNamespace

        seen = {}

        async def fake_forward(*_a, **_k):
            raise AssertionError("console stand-in must not use request.forward")

        async def fake_forward_chat(body, client=None):
            seen["body"] = json.loads(body.decode("utf-8"))
            seen["client"] = client
            return {"ok": True}

        proxy = SimpleNamespace(
            state=SimpleNamespace(id="console"),
            is_disconnected=lambda: False,
        )
        with (
            mock.patch("sidecar.proxy.llm_forward_client", return_value=(True, "llama-client")),
            mock.patch("sidecar.proxy.forward", new=fake_forward),
            mock.patch("sidecar.proxy.forward_chat", new=fake_forward_chat),
        ):
            result = await proxy_mod.forward_llm_chat(
                {
                    "messages": [{"role": "user", "content": "hello?"}],
                    "model": "qwen",
                    "dry_multiplier": 1.5,
                },
                request=proxy,
            )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen["body"]["model"], "gpt-4o")
        self.assertEqual(seen["body"]["messages"][0]["content"], "hello?")
        self.assertNotIn("dry_multiplier", seen["body"])
        self.assertEqual(seen["client"], "llama-client")

    async def test_ui_pipeline_llama_mode_forwards_to_llama(self):
        from endpoints.OAI.types.chat_completion import ChatCompletionRequest
        from endpoints.OAI.utils.pipeline import run_chat_completion_turn

        seen = {}

        async def fake_forward(payload, request=None, forward_fn=None):
            seen["payload"] = payload
            seen["request"] = request
            return {"ok": True, "via": "llama"}

        handler = mock.Mock()
        handler.poll = mock.AsyncMock()
        request = self._request()
        data = ChatCompletionRequest(
            messages=[{"role": "user", "content": "hello?"}],
            stream=False,
        )
        with (
            mock.patch("sidecar.settings.is_sidecar_process", return_value=True),
            mock.patch("sidecar.model_status.llm_is_ready", return_value=True),
            mock.patch(
                "endpoints.OAI.utils.pipeline.handle_image_chat",
                new=mock.AsyncMock(return_value=None),
            ),
            mock.patch("endpoints.OAI.utils.pipeline.gpu_is_comfy", return_value=False),
            mock.patch("sidecar.proxy.forward_llm_chat", new=fake_forward),
        ):
            result = await run_chat_completion_turn(
                request,
                data,
                handler,
                api_base="http://x/v1",
                console=True,
            )
        self.assertEqual(result, {"ok": True, "via": "llama"})
        self.assertEqual(seen["payload"]["messages"][0]["content"], "hello?")
        self.assertIs(seen["request"], request)

    async def test_generate_chat_sends_generate_only_header(self):
        seen = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            seen["header"] = request.headers.get("x-tabby-generate-only")
            seen["stream"] = json.loads(request.content).get("stream")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "plan"}}]},
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport, base_url="http://tabby.test")
        raw = await proxy_mod.generate_chat({"messages": []}, client=client)
        self.assertEqual(seen["header"], "1")
        self.assertFalse(seen["stream"])
        self.assertEqual(raw["choices"][0]["message"]["content"], "plan")


class SidecarModelStatusTests(unittest.TestCase):
    def test_uses_backend_http_when_configured(self):
        os.environ["TABBY_BACKEND_URL"] = "http://tabby.test"
        os.environ["TABBY_BACKEND_KEY"] = "k"
        self.addCleanup(lambda: os.environ.pop("TABBY_BACKEND_URL", None))
        self.addCleanup(lambda: os.environ.pop("TABBY_BACKEND_KEY", None))
        from sidecar import model_status

        with mock.patch.object(
            model_status,
            "_get_json",
            return_value=(200, {"id": "qwen", "parameters": {"max_seq_len": 8}}),
        ):
            self.assertTrue(model_status.llm_is_ready())
            card = model_status.model_card()
        self.assertEqual(card["id"], "qwen")
        self.assertEqual(card["max_seq_len"], 8)
        self.assertFalse(model_status.llm_jobs_active())

    def test_is_sidecar_process_respects_env(self):
        from sidecar.settings import is_sidecar_process

        os.environ["TABBY_PROCESS"] = "sidecar"
        self.addCleanup(lambda: os.environ.pop("TABBY_PROCESS", None))
        self.assertTrue(is_sidecar_process())
        os.environ["TABBY_PROCESS"] = "tabby"
        self.assertFalse(is_sidecar_process())

    def test_pipeline_forwards_console_chat_when_sidecar(self):
        src = Path(__file__).resolve().parents[1].joinpath(
            "endpoints/OAI/utils/pipeline.py"
        ).read_text(encoding="utf-8")
        self.assertIn("forward_llm_chat", src)
        self.assertIn("is_sidecar_process", src)
        self.assertIn("generate_only", src)

    def test_backend_generate_only_skips_image_intercept(self):
        router = Path(__file__).resolve().parents[1].joinpath(
            "endpoints/OAI/router.py"
        ).read_text(encoding="utf-8")
        self.assertIn("x-tabby-generate-only", router)
        self.assertIn("generate_only=True", router)

    def test_jobs_load_profile_uses_backend_when_sidecar(self):
        src = Path(__file__).resolve().parents[1].joinpath("images/jobs.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("load_backend_model", src)
        self.assertIn("unload_backend", src)


class SidecarBackendDirTests(unittest.TestCase):
    def test_override_points_at_vanilla_tree(self):
        os.environ["TABBY_BACKEND_DIR"] = "/tmp/vanilla-tabby"
        self.addCleanup(lambda: os.environ.pop("TABBY_BACKEND_DIR", None))
        from sidecar.settings import backend_dir, uses_vanilla_backend

        self.assertEqual(backend_dir(), Path("/tmp/vanilla-tabby").resolve())
        self.assertTrue(uses_vanilla_backend())

    def test_write_backend_tokens(self):
        from sidecar.backend_key import write_backend_tokens

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dest = Path(tmp.name)
        path = write_backend_tokens(dest, key="secret-key")
        self.assertEqual(path, dest / "api_tokens.yml")
        text = path.read_text(encoding="utf-8")
        self.assertIn("secret-key", text)

    def test_stacked_app_owns_ui_gpu_and_images(self):
        from common import auth as api_auth

        api_auth.DISABLE_AUTH = True
        api_auth.AUTH_KEYS = api_auth.AuthKeys(api_key="x", admin_key="y")
        from sidecar.app import create_app

        app = create_app(mount_stack=True, intercept_chat=True)
        paths: list[str] = []

        def walk(routes) -> None:
            for route in routes:
                path = getattr(route, "path", None)
                if path:
                    paths.append(path)
                nested = getattr(route, "routes", None)
                if nested:
                    walk(nested)
                original = getattr(route, "original_router", None)
                if original is not None:
                    walk(getattr(original, "routes", []))

        walk(app.routes)
        joined = " ".join(paths)
        self.assertIn("/v1/ui", joined)
        self.assertIn("/v1/gpu/mode", joined)
        self.assertIn("/v1/images/generations", joined)
        self.assertIn("/mcp", joined)


class SidecarScriptsTests(unittest.TestCase):
    def test_run_api_exports_pythonpath(self):
        text = Path(__file__).resolve().parents[1].joinpath(
            "deploy/arch/run-api.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("PYTHONPATH=", text)
        self.assertIn("$STACK:$ROOT", text)
        self.assertIn("watch_api.py", text)

    def test_watch_api_uses_supervise(self):
        text = Path(__file__).resolve().parents[1].joinpath("watch_api.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("sidecar.supervise", text)

    def test_supervise_starts_ssh_tunnel(self):
        text = Path(__file__).resolve().parents[2].joinpath("sidecar/supervise.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("ensure_ssh_forwarder", text)

    def test_fetch_upstream_pin(self):
        from sidecar.fetch_upstream import PINNED_SHA, UPSTREAM_URL

        self.assertTrue(PINNED_SHA)
        self.assertIn("theroyallab/tabbyAPI", UPSTREAM_URL)
