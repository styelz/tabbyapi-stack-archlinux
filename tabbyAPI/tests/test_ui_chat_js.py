"""UI console chat stop / queue / steer. Keep in sync with ui/static/chat.js."""

from __future__ import annotations

import json
import unittest
import re
from pathlib import Path

CHAT_JS = Path(__file__).resolve().parents[1] / "ui" / "static" / "chat.js"
CHAT_CSS = Path(__file__).resolve().parents[1] / "ui" / "static" / "styles.css"


def clean_reply_model(name: str) -> str:
    text = " ".join(str(name or "").split())
    if not text or text.lower() == "gpt-4o":
        return ""
    text = re.sub(r"\s*\(vision off on [^)]+\)\s*$", "", text, flags=re.I).strip()
    slash = text.find("/")
    if 0 < slash <= 32 and not re.search(r"\s", text[:slash]):
        text = text[slash + 1 :].strip()
    return text[:80]


def abort_keeps_going(
    user_abort: bool,
    assembled: str = "",
    held_job_id: str = "",
    recovered: str = "",
) -> bool:
    if user_abort:
        return False
    blob = f"{assembled or ''}\n{recovered or ''}"
    if re.search(
        r"here's the (picture|audio|video|clip)|here are the \d+ (pictures|audio clips|videos)|/v1/images/generated-",
        blob,
        flags=re.I,
    ):
        return True
    if " ".join(str(assembled or "").split()).strip():
        return True
    return bool(held_job_id)


def compose_action(in_flight: bool, typed: str, queued: str) -> tuple[str, bool]:
    text = (typed or "").strip()
    has_queue = bool((queued or "").strip())
    if not in_flight:
        return "send", False
    if text:
        return "queue", has_queue
    return "stop", has_queue


class ChatComposeActionTests(unittest.TestCase):
    def test_idle_send_never_steers(self):
        self.assertEqual(compose_action(False, "hello", ""), ("send", False))
        self.assertEqual(compose_action(False, "", "later"), ("send", False))

    def test_in_flight_empty_input_is_stop(self):
        self.assertEqual(compose_action(True, "", ""), ("stop", False))
        self.assertEqual(compose_action(True, "  ", "queued"), ("stop", True))

    def test_in_flight_typed_text_queues(self):
        self.assertEqual(compose_action(True, "more detail", ""), ("queue", False))
        self.assertEqual(compose_action(True, "instead", "old"), ("queue", True))


class ChatJsStopQueueSteerTests(unittest.TestCase):
    def setUp(self):
        self.src = CHAT_JS.read_text(encoding="utf-8")

    def test_compose_helper_matches_python_matrix(self):
        self.assertIn("function tabbyChatComposeAction(inFlight, typed, queued)", self.src)
        self.assertIn('mode: "send"', self.src)
        self.assertIn('mode: "queue"', self.src)
        self.assertIn('mode: "stop"', self.src)
        self.assertIn("showSteer", self.src)

    def test_chat_waits_out_api_restart_then_resends(self):
        self.assertIn("function tabbyLooksLikeRestart(err, status)", self.src)
        self.assertIn("async function pauseForRestart(working, activity, note)", self.src)
        self.assertIn("async function retryAfterRestart()", self.src)
        self.assertIn("The API is restarting. This chat will continue when it is ready.", self.src)
        self.assertIn("The API is back. Sending again.", self.src)
        self.assertIn("streamResume = Boolean(sendAgent)", self.src)
        self.assertIn(
            "if (!stopKind && toolRounds < MAX_AGENT_ROUNDS && !allInspectSkipped)",
            self.src,
        )
        self.assertNotIn(
            "if (!stopKind && !streamResume && toolRounds < MAX_AGENT_ROUNDS && !allInspectSkipped)",
            self.src,
        )
        self.assertIn("Catching up if that reply is still running.", self.src)
        self.assertIn("if (data && data.down)", self.src)
        self.assertNotIn("Restarting. Chat is paused until the API is ready.", self.src)

    def test_workspace_write_retries_a_dropped_fetch(self):
        self.assertIn("function tabbyNetworkErrorMessage(err)", self.src)
        self.assertIn("Lost the connection to the API. Retry the message.", self.src)
        self.assertIn("Lost the connection. Retrying the write.", self.src)
        self.assertIn("const maxTries = 6", self.src)
        tool_src = self.src.split("async function executeWorkspaceTool(")[1].split(
            "function normalizeToolChange("
        )[0]
        self.assertIn("tabbyIsNetworkDrop(err)", tool_src)
        self.assertIn("tabbyLooksLikeRestart(err)", tool_src)
        self.assertIn("typeof onRetry === \"function\"", tool_src)
        self.assertNotIn("throw new Error", tool_src)

    def test_drops_persist_media_echo_in_store(self):
        self.assertIn("function generatedMediaNames(text)", self.src)
        self.assertIn("function dropDuplicateMediaReplies(list)", self.src)
        self.assertIn("dropDuplicateMediaReplies(cloneMessages(item.messages))", self.src)
        self.assertIn("tabby-image-job:", self.src)

    def test_send_button_becomes_stop_during_session(self):
        self.assertIn('label: "Stop"', self.src)
        self.assertIn("abortSession(\"stop\")", self.src)
        self.assertIn("classList.toggle(\"is-stop\"", self.src)
        self.assertIn("chat-stop-icon", self.src)

    def test_abort_controller_cancels_fetch(self):
        self.assertIn("new AbortController()", self.src)
        self.assertIn("signal: abortController.signal", self.src)
        self.assertIn('err.name === "AbortError"', self.src)
        self.assertNotRegex(self.src, r"if \(inFlight\) return;")

    def test_live_thought_formats_fences_without_rebuilding_trace(self):
        self.assertIn("function paintThoughtSoon()", self.src)
        self.assertIn("function paintLiveReason(block)", self.src)
        self.assertIn('block.classList.add("is-live")', self.src)
        self.assertIn("block.innerHTML = TabbyUI.renderMarkdown(raw);", self.src)
        self.assertIn("function pinMarkdownCode(root)", self.src)
        self.assertIn("pinMarkdownCode(block);", self.src)
        self.assertIn("max-height: min(28rem, 55vh)", CHAT_CSS.read_text(encoding="utf-8"))
        self.assertNotIn("if (block.textContent !== reasoningText) block.textContent = reasoningText;", self.src)
        self.assertIn("function thoughtStepsSignature()", self.src)
        self.assertIn("function ensureLiveReasonBlock()", self.src)
        self.assertIn("if (live && thought.childElementCount && sig === thoughtStepSig)", self.src)
        self.assertIn("function freezeLiveReason()", self.src)
        self.assertIn("function appendReasonBlock(live)", self.src)
        self.assertIn("thought.appendChild(block);", self.src)
        self.assertNotIn("thought.insertBefore(block, thought.firstChild);", self.src)
        self.assertIn("freezeLiveReason();", self.src)
        self.assertIn('steps.push({ type: "thought", content: reasoningText });', self.src)
        paint = self.src.split("function paintThought()")[1].split("function addStatusNote(")[0]
        self.assertLess(paint.find("steps.forEach("), paint.find("appendReasonBlock(live)"))
        add_step = self.src.split("addStep(step, origin)")[1].split("setReasoning(text)")[0]
        self.assertIn("freezeLiveReason();", add_step)
        self.assertLess(add_step.find("if (step.type === \"demote\")"), add_step.rfind("freezeLiveReason();"))
        css = CHAT_CSS.read_text(encoding="utf-8")
        self.assertIn(".think-reason.is-live", css)
        self.assertIn("contain: paint", css)
        self.assertIn(".think-body > *", css)

    def test_reply_model_name_does_not_flip_during_stream(self):
        self.assertIn("if (modelName && !(opts && opts.replace)) return;", self.src)
        self.assertIn("working.setModel(named, { replace: true })", self.src)
        poll = self.src.split("function startStatusPoll")[1].split("function sleep(")[0]
        self.assertIn('kind === "switch" || kind === "restart"', poll)
        self.assertIn("text.replace(/\\s*\\(vision off on [^)]+\\)\\s*$/i", self.src)
        self.assertEqual(
            clean_reply_model(
                "turboderp/Qwen3.8-27B-exl3 SC_3.00bpw_H4_V4 (vision off on RTX 4070 Ti 12 GB)"
            ),
            "Qwen3.8-27B-exl3 SC_3.00bpw_H4_V4",
        )
        self.assertEqual(
            clean_reply_model("Qwen3.8-27B-exl3-SC_3.00bpw_H4_V4"),
            "Qwen3.8-27B-exl3-SC_3.00bpw_H4_V4",
        )
        self.assertEqual(clean_reply_model("gpt-4o"), "")

    def test_typed_text_during_session_is_queued(self):
        self.assertIn("function queueFollowup(", self.src)
        self.assertIn("if (inFlight)", self.src)
        self.assertIn("queueFollowup(text)", self.src)
        self.assertIn('label: "Queue"', self.src)
        self.assertIn("id=\"chat-queue\"", self.src)

    def test_compose_draft_survives_reload(self):
        self.assertIn("tabby-chat-compose", self.src)
        self.assertIn("function rememberCompose(", self.src)
        self.assertIn("function restoreCompose(", self.src)
        self.assertIn("rememberCompose()", self.src.split('input.addEventListener("input"')[1].split("input.addEventListener")[0])
        self.assertIn("rememberCompose(store.activeId)", self.src.split("function loadChat")[1].split("async function deleteChat")[0])
        self.assertIn("restoreCompose(id)", self.src.split("function loadChat")[1].split("async function deleteChat")[0])
        load_store = self.src.split("async function loadStore()")[1].split(
            'window.addEventListener("tabby-gpu-status"'
        )[0]
        self.assertIn("restoreCompose(store.activeId)", load_store)
        self.assertIn("sessionStorage.setItem(COMPOSE_STORE_KEY", self.src)
        wipe_at = self.src.index("function wipeClientUiStorage(")
        self.assertGreater(self.src.index("tabby-chat-compose"), wipe_at)

    def test_finished_media_abort_flushes_queued_followup(self):
        self.assertIn("function tabbyChatAbortKeepsGoing(", self.src)
        self.assertIn("if (stopKind === \"stop\") userAbort = true", self.src)
        self.assertIn("!tabbyChatAbortKeepsGoing(", self.src)
        self.assertIn("if (!userAbort && queuedTextFor(flightChatId))", self.src)
        self.assertIn("tabby-chat-queue", self.src)
        self.assertTrue(
            abort_keeps_going(False, "Here's the picture.\n![](/v1/images/generated-1.png)")
        )
        self.assertTrue(abort_keeps_going(False, "", "job-1"))
        self.assertFalse(abort_keeps_going(True, "Here's the picture."))
        self.assertFalse(abort_keeps_going(False, "", ""))

    def test_gguf_base_model_gets_a_composer_hint(self):
        self.assertIn('id="chat-gguf-base-hint"', self.src)
        self.assertIn("function paintGgufBaseHint()", self.src)
        self.assertIn("base (completion) model", self.src)
        self.assertIn("switch to qwen", self.src)
        self.assertIn("gguf_base", self.src)

    def test_code_write_hint_for_models_without_file_tools(self):
        self.assertIn('id="chat-code-write-hint"', self.src)
        self.assertIn("function paintCodeWriteHint()", self.src)
        self.assertIn("cannot write Code files", self.src)
        self.assertIn("codeWriteBlockKind", self.src)
        self.assertIn("writes_files", self.src)

    def test_queued_message_can_steer(self):
        self.assertIn("id=\"chat-steer\"", self.src)
        self.assertIn("abortSession(\"steer\")", self.src)
        self.assertIn('if (stopKind === "steer")', self.src)
        self.assertIn("showSteer: hasQueue", self.src)

    def test_empty_stop_does_not_keep_working_bubble(self):
        self.assertIn("working.discard()", self.src)
        self.assertIn("function abortSession(kind)", self.src)
        self.assertIn("conversation_id: flightChatId || store.activeId", self.src)

    def test_split_save_and_preview_opener(self):
        self.assertIn("function editorTabForHost(hostHint, pathHint)", self.src)
        self.assertIn("function stashEditor(hostHint, pathHint)", self.src)
        self.assertIn("saveTab(host, path)", self.src)
        self.assertIn("tab.opener = null", self.src)
        self.assertIn("function applyListing(data, chatId)", self.src)
        self.assertIn("store.activeId = viewing", self.src)

    def test_finished_reply_keeps_elapsed_time(self):
        self.assertIn("item.elapsed_s = elapsedSec", self.src)
        self.assertIn("item.status_label = statusLabel", self.src)
        self.assertNotIn("Replied in ${elapsed}", self.src)
        self.assertIn("timeEl.textContent = seconds != null ? TabbyUI.formatDuration(seconds) : \"\"", self.src)

    def test_model_wait_unlocks_when_gpu_is_serving(self):
        self.assertIn("TabbyUI.gpuSwitchPaused", self.src)
        self.assertNotIn("if (data.units && data.units.comfyui) return true", self.src)
        ready = self.src.split("function modelLooksReady")[1].split("async function waitForModelReady")[0]
        self.assertIn("data.llama_up", ready)
        self.assertIn("data.loaded", ready)
        after = self.src.split("await syncModelGate();")[1].split("next = takeQueue")[0]
        self.assertNotIn("!data.tabby_model", after)
        self.assertIn("statusIsBusy(data)", after)
        utils = Path(__file__).resolve().parents[1] / "ui" / "static" / "utils.js"
        utils_src = utils.read_text(encoding="utf-8")
        self.assertIn("const loaded = this.gpuIsServing(data);", utils_src)

    def test_coding_job_status_is_not_picture_planning(self):
        self.assertIn('if (phase === "writing_code" || phase === "coding") return "Writing the page"', self.src)
        self.assertNotIn('if (phase === "writing_code" || phase === "coding") return "Planning the picture"', self.src)

    def test_mode_toggle_opens_a_separate_conversation(self):
        self.assertIn("function chatForMode(mode)", self.src)
        self.assertIn("function setChatMode(mode)", self.src)
        self.assertNotIn("chat.mode = next", self.src)
        self.assertIn("chatMode(chat) === mode", self.src)
        self.assertIn("lastByMode", self.src)

    def test_sidebar_row_actions_overlay_the_cell(self):
        css = CHAT_CSS.read_text(encoding="utf-8")
        self.assertIn(".chat-nav-tools {", css)
        self.assertIn(".chat-file-tools {", css)
        self.assertIn("position: absolute", css)
        self.assertIn(".chat-nav:hover .chat-nav-tools", css)
        self.assertIn(".chat-nav:focus-within .chat-nav-tools", css)
        self.assertRegex(css, r"\.chat-nav-tools \{[^}]*opacity: 0")
        self.assertNotRegex(css, r"\.chat-nav \{[^}]*grid-template-columns: 18px minmax\(0, 1fr\) auto")
        self.assertNotRegex(css, r"\.chat-file \{[^}]*grid-template-columns: minmax\(0, 1fr\) auto auto auto auto")
        self.assertIn('class="chat-file-tools"', self.src)

    def test_dirty_tabs_are_stashed_per_chat(self):
        self.assertIn("let tabsByChat", self.src)
        self.assertIn("function stashCurrentTabs()", self.src)
        self.assertIn("function switchWorkspaceTabs(chatId)", self.src)
        self.assertIn("function warnDirtyUnload(event)", self.src)
        self.assertIn("anyDirtyTabs()", self.src)

    def test_optimizing_status_refreshes_files(self):
        self.assertIn("function refreshFilesSoon(", self.src)
        self.assertIn("if (chatsShareWorkspace(chatId)) refreshFilesSoon()", self.src)
        self.assertIn("working.addStep(event.step, \"stream\")", self.src)

    def test_files_overflow_and_history_collapse(self):
        self.assertIn('id="chat-files-more"', self.src)
        self.assertIn('data-files-more="refresh"', self.src)
        self.assertIn('id="chat-files-history-toggle"', self.src)
        self.assertIn("function setHistoryOpen(open)", self.src)
        self.assertIn("function setChangesOpen(open)", self.src)
        self.assertIn("persistLayout()", self.src)
        self.assertIn("chat-files-twist", self.src)
        self.assertIn("function changeMenuItems(", self.src)
        self.assertIn("function discardChange(", self.src)
        self.assertIn("function discardAllChanges(", self.src)
        self.assertIn('label: "Discard Changes"', self.src)
        self.assertIn('label: "Discard All Changes"', self.src)
        self.assertIn("filesChangesList.contains(changeRow)", self.src)
        css = CHAT_CSS.read_text(encoding="utf-8")
        self.assertIn(".chat-files-history.is-collapsed", css)
        self.assertIn(".chat-files-twist", css)
        self.assertIn(".chat-files.is-drop", css)

    def test_find_in_chat_bar(self):
        self.assertIn('id="chat-find"', self.src)
        self.assertIn("function openFind(seed)", self.src)
        self.assertIn("function jumpSidebarSearch()", self.src)
        self.assertIn("function paintFindHits()", self.src)

    def test_stack_occupancy_banner_and_chip(self):
        self.assertIn('id="chat-waiting-mark"', self.src)
        self.assertIn('function applyStackOccupancy(data, working, kind)', self.src)
        self.assertNotIn("function showIdleOccupancy(hint)", self.src)
        self.assertIn("queued && !ownChat", self.src)
        self.assertIn("function tabbyOccupancyHintIsOwnRun(hint)", self.src)
        self.assertIn("opts.occupancy", self.src)
        self.assertIn("function summaryFromCodeSteps(steps)", self.src)
        self.assertIn("function compactToolTitle(step)", self.src)
        self.assertIn("function lastPendingToolIndex(steps, incoming)", self.src)
        self.assertIn("function stepIsVisible(step)", self.src)
        self.assertIn("Wrote ${unique[0]}.", self.src)
        self.assertIn("mtime === (Number(tab.mtime) || 0)", self.src)
        utils = Path(__file__).resolve().parents[1] / "ui" / "static" / "utils.js"
        app = Path(__file__).resolve().parents[1] / "ui" / "static" / "app.js"
        status = Path(__file__).resolve().parents[1] / "ui" / "static" / "status.js"
        utils_src = utils.read_text(encoding="utf-8")
        app_src = app.read_text(encoding="utf-8")
        status_src = status.read_text(encoding="utf-8")
        self.assertIn("IN USE · ${kindLabel}", utils_src)
        self.assertIn("function gpuSwitchPaused(data)", utils_src)
        self.assertIn("if (gpuIsServing(data)) return false", utils_src)
        self.assertIn("WAITING · ${name}", utils_src)
        self.assertIn("gpu_waiting", utils_src)
        self.assertIn("You are in a queue", utils_src)
        self.assertIn("stack_queue.busy || data.stack_queue.queued", app_src)
        self.assertIn('occupied && !switchLocked && current !== name ? "Wait"', app_src)
        self.assertIn("function occupancyLabel(data)", status_src)
        self.assertIn('if (queue.queued) return queue.hint || "You are in a queue"', status_src)
        self.assertIn('if (queue.mine) return queue.hint || "Your session is running"', status_src)
        self.assertIn('if (queue.busy) return "In use"', status_src)
        self.assertIn('fact("Stack"', status_src)
        self.assertIn("already_running", status_src)
        self.assertIn("/already running/i.test(result.message", status_src)
        self.assertIn("waitUntilReady({ requireDown: true, watchUpdate: true })", status_src)
        self.assertIn("waitUntilReady({ requireDown: false, watchUpdate: true })", status_src)
        self.assertIn("offerGitRestart", status_src)
        self.assertIn("needs_restart", status_src)
        self.assertIn("The API was not restarted.", status_src)
        self.assertIn("progress-meter", utils_src)
        self.assertIn("startUpdateLog", status_src)
        self.assertIn("Waiting for the first log lines", utils_src)
        self.assertIn("Still working", utils_src)
        self.assertIn('class="progress-idle"', utils_src)
        self.assertIn("function setProgress(", utils_src)
        self.assertIn("get updatePrompt()", utils_src)
        self.assertIn("data.prompt", utils_src)
        self.assertIn("==>\\s*\\[(\\d+)%\\]", utils_src)
        css = CHAT_CSS.read_text(encoding="utf-8")
        self.assertIn(".progress-meter", css)
        self.assertIn(".progress-spin", css)
        self.assertIn(".progress-log-hint", css)
        self.assertIn(".progress-idle", css)
        self.assertIn(".progress-status", css)

    def test_tree_drag_and_editor_find(self):
        self.assertIn('application/x-tabby-path', self.src)
        self.assertIn("function moveProjectItem(", self.src)
        self.assertIn('id="editor-find"', self.src)
        self.assertIn("function openEditorFind()", self.src)
        self.assertIn("function flushDrafts(", self.src)
        self.assertIn('id="chat-preview"', self.src)
        self.assertIn("function showPreview(", self.src)
        self.assertIn("function isPreviewTab(", self.src)
        self.assertIn('id="chat-preview-tab"', self.src)
        self.assertIn("function dockPreview()", self.src)
        css = CHAT_CSS.read_text(encoding="utf-8")
        self.assertIn(".chat-preview.is-tab", css)
        self.assertIn('id="chat-term"', self.src)
        self.assertIn("function openTerm()", self.src)
        self.assertIn("window.TabbyLsp", self.src)

    def test_code_mode_workspaces_nest_chats(self):
        self.assertIn("function workspaceId(", self.src)
        self.assertIn("function chatParentId(", self.src)
        self.assertIn("function startNestedChat(", self.src)
        self.assertIn("function listedWorkspaceRows(", self.src)
        self.assertIn("function workspaceActivity(", self.src)
        self.assertIn("return (b.updatedAt || 0) - (a.updatedAt || 0)", self.src.split("function listedWorkspaceRows")[1].split("function navRowMeta")[0])
        self.assertNotIn("workspaceActivity(", self.src.split("function listedWorkspaceRows")[1].split("function navRowMeta")[0])
        self.assertNotIn("root.updatedAt = now", self.src)
        self.assertNotIn("if (root) root.updatedAt", self.src.split("function touchActive")[1].split("function paintToolbar")[0])
        self.assertIn("function workspaceDisplayTitle(", self.src)
        self.assertIn("function listedWorkspaceKids(", self.src)
        self.assertIn("function lastWorkspaceThread(", self.src)
        self.assertIn("lastWorkspaceThread(parent", self.src.split("function chatIsKept")[1].split("function listedChats")[0])
        self.assertNotIn("hasUserTurn(chat) || chat.pinned || chat.id === store.activeId", self.src.split("function listedWorkspaceKids")[1].split("function listedWorkspaceRows")[0])
        self.assertNotIn("return false;", self.src.split("function workspaceExpanded")[1].split("function workspaceDisplayTitle")[0])
        self.assertIn("function chatsShareWorkspace(", self.src)
        self.assertIn("function openWorkspaceNav(", self.src)
        self.assertIn("function preferredCodeChat(", self.src)
        self.assertIn('emptyChat("code", root.id)', self.src)
        self.assertIn("startNestedChat(id)", self.src)
        self.assertIn("hideHistoryMenu();", self.src)
        self.assertIn("function fallbackCodeChat(", self.src)
        self.assertIn("fallbackCodeChat(parentId)", self.src)
        self.assertNotIn("renderHistoryMenu();", self.src.split("async function deleteChat")[1].split("function startNestedChat")[0])
        self.assertNotIn("addCodeWorkspace()", self.src.split("async function deleteChat")[1].split("function startNestedChat")[0])
        self.assertIn("await dropWorkspace(id)", self.src.split("async function deleteChat")[1].split("function startNestedChat")[0])
        self.assertIn("function revertCodeHistory(", self.src)
        self.assertIn("function laterWorkspaceChats(", self.src)
        self.assertIn("function historySpecFromMessages(", self.src)
        self.assertIn("prev && prev.created && prev.run && prev.run === run", self.src)
        self.assertIn("Workspace files will revert to before this chat.", self.src.split("async function deleteChat")[1].split("function startNestedChat")[0])
        self.assertIn("revertCodeHistory(spec, workspaceId(chat))", self.src.split("async function deleteChat")[1].split("function startNestedChat")[0])
        self.assertIn("laterWorkspaceChats(chat)", self.src.split("async function deleteChat")[1].split("function startNestedChat")[0])
        delete_turn = self.src.split("async function deleteTurn")[1].split("function splitStartIndex")[0]
        self.assertIn("revertCodeHistory(spec)", delete_turn)
        self.assertIn("messages.splice(idx)", delete_turn)
        self.assertIn("every reply after it", delete_turn)
        self.assertIn("if (item.historyRun)", self.src)
        self.assertIn("userItem.historyRun = historyRun", self.src)
        self.assertIn("await Promise.all(", self.src.split("async function clearHistory")[1].split("function hideHistoryMenu")[0])
        self.assertNotIn("function mergeChatStores", self.src)
        self.assertIn("function wipeClientUiStorage(", self.src)
        self.assertNotIn("function readLegacyStore(", self.src)
        self.assertNotIn("readLegacyStore()", self.src)
        self.assertIn('id="chat-tabs"', self.src)
        self.assertIn("chat-editor-col", self.src)
        self.assertIn("Boolean(tab) && !previewAsTab", self.src)
        self.assertIn("New workspace", self.src)
        self.assertIn("New chat in this workspace", self.src)
        self.assertIn('data-nav="thread"', self.src)
        self.assertIn('data-nav="twist"', self.src)
        self.assertIn("kidCount > 0", self.src)
        self.assertIn('label: "Expand"', self.src)
        self.assertIn('label: "Collapse"', self.src)
        self.assertIn("chat-nav-group", self.src)
        self.assertIn("root ? openWorkspaceNav(id) : loadChat(id)", self.src)
        self.assertIn("isWorkspaceRoot(target)", self.src)
        self.assertIn("function pinTarget(", self.src)
        self.assertIn("kind !== \"child\"", self.src.split("function navRowTools")[1].split("function navRowHtml")[0])
        self.assertIn("kind !== \"child\" && item.pinned", self.src)
        self.assertNotIn("function workspacePinTarget(", self.src)
        self.assertNotIn("function workspaceShowsKids(", self.src)
        self.assertNotIn("function ensureNestedChat(", self.src)
        self.assertNotIn("function refreshCodeChats(", self.src)
        self.assertNotIn("function noteChatFiles(", self.src)
        self.assertNotIn("function toolbarNamedChat(", self.src)
        self.assertIn("body.chat_id = workspaceId(targetChat) || activeWorkspaceId()", self.src)
        self.assertIn("parentId", self.src)
        self.assertIn("isWorkspaceRoot(item)", self.src)
        css = CHAT_CSS.read_text(encoding="utf-8")
        self.assertIn(".chat-nav.is-child", css)
        self.assertIn(".chat-nav.is-workspace", css)
        self.assertIn(".chat-shell.is-code .chat-nav-group", css)
        self.assertIn(".chat-shell.is-code .chat-nav-list", css)
        self.assertIn(".chat-nav.is-current:not(.is-active)", css)
        self.assertIn(".chat-nav-tools", css)

    def test_context_usage_ring(self):
        utils = Path(__file__).resolve().parents[1] / "ui" / "static" / "utils.js"
        html = Path(__file__).resolve().parents[1] / "ui" / "static" / "index.html"
        app = Path(__file__).resolve().parents[1] / "ui" / "static" / "app.js"
        css = CHAT_CSS.read_text(encoding="utf-8")
        utils_src = utils.read_text(encoding="utf-8")
        html_src = html.read_text(encoding="utf-8")
        app_src = app.read_text(encoding="utf-8")
        self.assertIn('id="context-chip"', html_src)
        self.assertIn("paintContextUsage(", utils_src)
        self.assertIn("if (json.usage) onEvent({ usage: json.usage })", self.src)
        self.assertIn("tabby-context-usage:", self.src)
        self.assertIn("function applyUsage(", self.src)
        self.assertIn("function paintActiveContext(", self.src)
        self.assertIn("function cloneUsage(", self.src)
        self.assertIn(".context-usage-widget", css)
        self.assertIn(".progress-arc", css)
        self.assertIn("closeContextMenu()", app_src)
        self.assertIn("openContextMenu()", app_src)

    def test_code_agent_ask_plan_uses_thread(self):
        self.assertIn('data-agent="agent"', self.src)
        self.assertIn('data-agent="ask"', self.src)
        self.assertIn('data-agent="plan"', self.src)
        self.assertIn("Implement the approved plan above. Do not wait for more confirmation.", self.src)
        self.assertIn("function isBuildPromptText(text)", self.src)
        self.assertIn("function lastUnbuiltPlanIndex()", self.src)
        self.assertIn("function canBuildPlan(idx)", self.src)
        self.assertIn("<approved_plan>", self.src)
        self.assertIn("function buildApprovedPlan(", self.src)
        self.assertIn("opts.agent = replayAgent", self.src)
        self.assertIn("normalizeAgent((opts && opts.agent) || codeAgent)", self.src)
        self.assertIn("activityFromPrompt(outboundText, sendAgent)", self.src)
        self.assertIn("AGENT_EMPTY_NUDGE", self.src)
        self.assertIn("AGENT_CLAIM_NUDGE", self.src)
        self.assertIn("AGENT_WORK_NUDGE", self.src)
        self.assertIn("AGENT_NO_EDIT_REPLY", self.src)
        self.assertIn("MAX_AGENT_EMPTY_NUDGES = 2", self.src)
        self.assertIn("function looksLikeEditClaim(text)", self.src)
        self.assertIn("function userItemWantsEdits(", self.src)
        self.assertIn("needsClaimNudge", self.src)
        self.assertIn("needsWorkNudge", self.src)
        self.assertIn("mutatedPaths.delete(written)", self.src)
        self.assertIn("roundMutateFailed", self.src)
        self.assertIn("function lastRealUserItemFor(", self.src)
        self.assertIn("assembled = AGENT_NO_EDIT_REPLY", self.src)
        self.assertNotIn("Do not generate images unless they asked.", self.src)
        self.assertIn("agentEmptyNudges", self.src)
        self.assertIn("AGENT_DONE_NUDGE", self.src)
        self.assertIn("function mergeToolCallDeltas(existing, incoming)", self.src)
        self.assertIn("toolCalls = mergeToolCallDeltas(toolCalls, event.tool_calls)", self.src)
        self.assertIn("function cleanedToolCalls(", self.src)
        self.assertIn("function isCompleteJsonValue(", self.src)
        self.assertIn("out.tool_calls = cleanedToolCalls(item.tool_calls)", self.src)
        self.assertIn("assistantItem.tool_calls = cleanedToolCalls(toolCalls)", self.src)
        self.assertIn("function shouldSkipInspectTool(", self.src)
        self.assertIn("out.content = \"\"", self.src.split("function outboundAssistant")[1].split("function outboundTool")[0])
        self.assertIn("heldJobId", self.src)
        self.assertIn("pollImageHoldReply", self.src)
        self.assertIn("imageHoldEmpty", self.src)
        self.assertIn('promptAgent === "ask" || promptAgent === "plan"', self.src)
        self.assertIn("function readonlyModeHint(agent, text)", self.src)
        self.assertIn("function attachModeHint(host, idx)", self.src)
        self.assertIn("chat-mode-hint-now", self.src)
        self.assertIn("now.dataset.agent", self.src)
        self.assertIn("chat-mode-hint-pill", self.src)
        self.assertIn("btn.dataset.modeHint = target", self.src)
        self.assertIn('note: "Preparing the GPU."', self.src)
        self.assertIn("function tabbyImageRenderLabel(text)", self.src)
        self.assertIn("function tabbyImageDestClass(title, index)", self.src)
        self.assertIn('row.className = tabbyImageDestClass(title, index)', self.src)
        dest_fn = self.src.split("function tabbyImageDestClass")[1].split("function tabbyImageProgressNote")[0]
        self.assertIn('return "is-done"', dest_fn)
        self.assertIn("Reloading the coding model", dest_fn)
        self.assertIn("function beginImageHold(", self.src)
        self.assertIn("think-image-progress", self.src)
        self.assertIn("imageHoldActive", self.src)
        self.assertIn("if (working.beginImageHold) working.beginImageHold()", self.src)
        self.assertIn("Agent edits files, Ask answers without changing them", self.src)
        css = CHAT_CSS.read_text(encoding="utf-8")
        self.assertIn(".chat-agent-hint", css)
        self.assertIn(".chat-mode-hint", css)
        self.assertIn(".chat-mode-hint-now", css)
        self.assertIn(".chat-mode-hint-pill", css)
        self.assertIn(".think-image-progress", css)
        self.assertIn(".think-image-progress-dests", css)

    def test_code_agent_steps_stay_compact(self):
        utils = Path(__file__).resolve().parents[1] / "ui" / "static" / "utils.js"
        utils_src = utils.read_text(encoding="utf-8")
        self.assertIn("Point img src or CSS url", utils_src)
        self.assertIn("Write the page now", utils_src)
        self.assertIn("Do not Write PNG", utils_src)
        self.assertIn("lastPendingToolIndex(steps, row)", self.src)
        self.assertIn("compactToolTitle(step)", self.src)
        self.assertIn("writeResultIsEcho(step, path)", self.src)
        self.assertIn("if (row) thought.appendChild(row)", self.src)
        self.assertIn("draft !== lastText", self.src)

    def test_code_git_pane(self):
        css = CHAT_CSS.read_text(encoding="utf-8")
        self.assertIn('id="chat-files-git"', self.src)
        self.assertIn('id="chat-files-git-list"', self.src)
        self.assertIn('id="chat-files-git-toggle"', self.src)
        self.assertIn("function refreshGit(", self.src)
        self.assertIn("function refreshGitSoon(", self.src)
        self.assertIn("function gitListSignature(", self.src)
        self.assertIn("if (!force && sig === gitPaintSig && filesGitList.childElementCount) return;", self.src)
        self.assertIn('await withGitBusy("refresh"', self.src)
        refresh_fn = self.src.split("async function refreshGit(")[1].split("async function gitPromptToken")[0]
        self.assertNotIn("gitBusy = true", refresh_fn)
        self.assertIn("if (!inFlight) refreshGitSoon();", self.src.split("function paintFiles(")[1].split("function isImageTab")[0])
        self.assertIn('activeMode() === "code" && codeAgent === "agent"', self.src)
        self.assertIn("function openGitDiff(", self.src)
        self.assertIn("__git__/", self.src)
        self.assertIn("Initialize repository", self.src)
        self.assertIn("This workspace has a .git folder", self.src)
        self.assertIn('gitActionBtn("refresh"', self.src)
        self.assertIn('gitActionBtn("commit"', self.src)
        self.assertIn('gitActionBtn("push"', self.src)
        self.assertIn("workspace/${encodeURIComponent(chatId)}/git", self.src)
        self.assertIn("function setGitOpen(open)", self.src)
        self.assertIn(".chat-git-commit", css)
        self.assertIn("#chat-files-git:not(.is-collapsed)", css)

    def test_prefs_live_on_the_server(self):
        utils = Path(__file__).resolve().parents[1] / "ui" / "static" / "utils.js"
        html = Path(__file__).resolve().parents[1] / "ui" / "static" / "index.html"
        login = Path(__file__).resolve().parents[1] / "ui" / "static" / "login.html"
        app = Path(__file__).resolve().parents[1] / "ui" / "static" / "app.js"
        utils_src = utils.read_text(encoding="utf-8")
        html_src = html.read_text(encoding="utf-8")
        login_src = login.read_text(encoding="utf-8")
        app_src = app.read_text(encoding="utf-8")
        self.assertIn("window.TABBY_UI_PREFS = null;", html_src)
        self.assertIn("window.TABBY_UI_EPOCH = null;", html_src)
        self.assertIn("dropPrefixed(localStorage", html_src)
        self.assertIn("dropPrefixed(localStorage)", login_src)
        self.assertIn('cache: "no-store"', utils_src)
        self.assertNotIn("localStorage", utils_src)
        self.assertNotIn("sessionStorage", utils_src)
        self.assertNotIn("localStorage.setItem", self.src)
        self.assertNotIn("localStorage.getItem", self.src)
        self.assertNotIn("sessionStorage", self.src.split("function wipeClientUiStorage")[0])
        self.assertIn('api("prefs"', utils_src)
        self.assertIn("function patchPrefs(", utils_src)
        self.assertIn("function persistLayout(", self.src)
        self.assertIn("patchPrefs({ layout })", self.src)
        self.assertIn("patchPrefs({ codeAgent })", self.src)
        self.assertIn("wipeClientUiStorage()", self.src)
        self.assertNotIn("readLegacyStore()", self.src)
        load_store = self.src.split("async function loadStore()")[1].split(
            'window.addEventListener("tabby-gpu-status"'
        )[0]
        self.assertIn("wipeClientUiStorage()", load_store)
        self.assertNotIn("imported", load_store)
        self.assertNotIn("persist()", load_store)
        self.assertIn("persistReady = fetched", load_store)
        self.assertIn("TABBY_UI_EPOCH", load_store)
        self.assertIn('cache: "reload"', load_store)
        self.assertIn("TabbyUI.flushPrefs", app_src)

    def test_missing_model_switch_confirms_and_shows_progress(self):
        utils = Path(__file__).resolve().parents[1] / "ui" / "static" / "utils.js"
        app = Path(__file__).resolve().parents[1] / "ui" / "static" / "app.js"
        status = Path(__file__).resolve().parents[1] / "ui" / "static" / "status.js"
        utils_src = utils.read_text(encoding="utf-8")
        app_src = app.read_text(encoding="utf-8")
        status_src = status.read_text(encoding="utf-8")
        self.assertIn("function offerMissingModelDownload(", utils_src)
        self.assertIn("function followCatalogDownload(", utils_src)
        self.assertIn("function missingProfileToken(", utils_src)
        self.assertIn('kind: "catalog"', utils_src)
        self.assertIn("progressModal", utils_src)
        self.assertIn('hint === "Download"', app_src)
        self.assertIn('? "Download"', app_src)
        self.assertIn('textContent = missing ? "Download"', status_src)
        self.assertIn("offerMissingModelDownload", status_src)
        self.assertIn("maybeDownloadForSwitch", self.src)
        self.assertIn("maybeWarnBlindVision", self.src)
        self.assertIn("missingProfileToken", self.src)
        self.assertIn("Not installed — download", self.src)
        self.assertNotIn("You can watch progress on the Models page.", app_src)
        self.assertNotIn("location.hash = \"#models\"", status_src)

    def test_vision_off_image_warns_before_send(self):
        utils = Path(__file__).resolve().parents[1] / "ui" / "static" / "utils.js"
        utils_src = utils.read_text(encoding="utf-8")
        self.assertIn("function modelVisionOff(", utils_src)
        self.assertIn("function alertVisionOffImage(", utils_src)
        self.assertIn("This model cannot see pictures", utils_src)
        self.assertIn("Switch to qwen", utils_src)
        self.assertIn("Vision off — pictures are not seen", utils_src)
        self.assertIn("function maybeWarnBlindVision(", self.src)
        self.assertIn("pendingHasImage()", self.src)
        self.assertIn("startQwenSwitch()", self.src)


def merge_tool_call_deltas(existing, incoming):
    """Keep in sync with mergeToolCallDeltas in ui/static/chat.js."""
    if not isinstance(incoming, list) or not incoming:
        return list(existing or [])
    indexed = any(isinstance(item, dict) and isinstance(item.get("index"), int) for item in incoming)
    if not indexed:
        return list(incoming)
    out = []
    for item in existing or []:
        row = dict(item or {})
        row["function"] = dict(row.get("function") or {})
        out.append(row)
    for part in incoming:
        if not isinstance(part, dict):
            continue
        idx = part.get("index")
        if not isinstance(idx, int):
            idx = len(out)
        while len(out) <= idx:
            out.append({"type": "function", "function": {"name": "", "arguments": ""}})
        dest = out[idx]
        if part.get("id"):
            dest["id"] = part["id"]
        if part.get("type"):
            dest["type"] = part["type"]
        fn = part.get("function") or {}
        dest_fn = dest.setdefault("function", {})
        if fn.get("name"):
            dest_fn["name"] = fn["name"]
        args = fn.get("arguments")
        if isinstance(args, str):
            prev = str(dest_fn.get("arguments") or "")
            if not prev:
                dest_fn["arguments"] = args
            elif prev == args:
                dest_fn["arguments"] = prev
            else:
                try:
                    json.loads(prev)
                    json.loads(args)
                    dest_fn["arguments"] = args
                except (json.JSONDecodeError, TypeError, ValueError):
                    dest_fn["arguments"] = prev + args
        elif isinstance(args, dict):
            dest_fn["arguments"] = args
    return out


class MergeToolCallDeltasTests(unittest.TestCase):
    def test_llama_fragments_rebuild_a_write_call(self):
        chunks = [
            {
                "index": 0,
                "id": "call_write",
                "type": "function",
                "function": {"name": "Write", "arguments": ""},
            },
            {"index": 0, "function": {"arguments": '{"path":"styles.css","contents":'}},
            {"index": 0, "function": {"arguments": '"/* compact */\\n"}'}},
        ]
        merged = []
        for chunk in chunks:
            merged = merge_tool_call_deltas(merged, [chunk])
        self.assertEqual(merged[0]["function"]["name"], "Write")
        self.assertIn("styles.css", merged[0]["function"]["arguments"])
        self.assertIn("compact", merged[0]["function"]["arguments"])

    def test_exl_complete_list_is_kept(self):
        complete = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "Read", "arguments": '{"path":"index.html"}'},
            }
        ]
        self.assertEqual(merge_tool_call_deltas([], complete), complete)

    def test_duplicate_complete_snapshot_is_not_concatenated(self):
        snapshot = {
            "index": 0,
            "id": "call_write",
            "type": "function",
            "function": {
                "name": "Write",
                "arguments": '{"path":"index.html","contents":"<html>"}',
            },
        }
        merged = merge_tool_call_deltas([], [snapshot])
        merged = merge_tool_call_deltas(merged, [snapshot])
        args = merged[0]["function"]["arguments"]
        self.assertEqual(args, snapshot["function"]["arguments"])
        self.assertNotIn("}{", args)


def looks_like_edit_claim(text: str) -> bool:
    """Keep in sync with looksLikeEditClaim in ui/static/chat.js."""
    raw = " ".join(str(text or "").split()).strip()
    if not raw:
        return False
    return bool(
        re.search(r"^(?:done|fixed|updated|restored|undone)\b", raw, re.I)
        or re.search(r"\bundo complete\b", raw, re.I)
        or re.search(r"\bno further edits are needed\b", raw, re.I)
        or re.search(
            r"\bi(?:['\u2019]ve| have) (?:now )?(?:fixed|restored|removed|"
            r"updated|changed|written|edited|undone|moved|added)\b",
            raw,
            re.I,
        )
        or re.search(
            r"\bi (?:fixed|restored|removed|updated|changed|wrote|edited|"
            r"undid|moved|added)\b",
            raw,
            re.I,
        )
        or re.search(
            r"\bi(?:['\u2019]ll| will) (?:now )?(?:remove|fix|restore|undo|"
            r"change|update|edit|write|move|add|delete)\b",
            raw,
            re.I,
        )
        or re.search(
            r"\blet me (?:now )?(?:remove|fix|restore|undo|change|update|"
            r"edit|write)\b",
            raw,
            re.I,
        )
    )


class EditClaimTests(unittest.TestCase):
    def test_live_code_chat_false_completes(self):
        self.assertTrue(
            looks_like_edit_claim(
                "Done. The hamburger and cart counter now sit in the "
                "header-actions group on the far right of the header."
            )
        )
        self.assertTrue(
            looks_like_edit_claim(
                "Undo complete. I restored the original header markup "
                "so the hamburger works again. The CSS for header-actions "
                "wasn't present, so no further edits are needed."
            )
        )
        self.assertTrue(
            looks_like_edit_claim(
                'I see the issue — the brand is appearing twice '
                '("Wavelength Wavelength") because my earlier edit '
                "duplicated the <a> tag. I'll remove the duplicate brand."
            )
        )

    def test_analysis_without_a_promise_is_not_a_claim(self):
        self.assertFalse(
            looks_like_edit_claim(
                "I see the issue — the brand is appearing twice because "
                "the header has two brand links."
            )
        )
        self.assertFalse(looks_like_edit_claim(""))
        self.assertFalse(looks_like_edit_claim("The nav uses #mobile-nav."))


if __name__ == "__main__":
    unittest.main()

