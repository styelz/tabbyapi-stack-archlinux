"""UI console chat stop / queue / steer. Keep in sync with ui/static/chat.js."""

from __future__ import annotations

import unittest
from pathlib import Path

CHAT_JS = Path(__file__).resolve().parents[1] / "ui" / "static" / "chat.js"
CHAT_CSS = Path(__file__).resolve().parents[1] / "ui" / "static" / "styles.css"


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

    def test_typed_text_during_session_is_queued(self):
        self.assertIn("function queueFollowup(", self.src)
        self.assertIn("if (inFlight)", self.src)
        self.assertIn("queueFollowup(text)", self.src)
        self.assertIn('label: "Queue"', self.src)
        self.assertIn("id=\"chat-queue\"", self.src)

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
        self.assertIn("function readLegacyStore(", self.src)
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
        self.assertIn("agentEmptyNudges", self.src)
        self.assertIn("AGENT_DONE_NUDGE", self.src)
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
        self.assertIn("Agent edits files, Ask answers without changing them", self.src)
        css = CHAT_CSS.read_text(encoding="utf-8")
        self.assertIn(".chat-agent-hint", css)
        self.assertIn(".chat-mode-hint", css)
        self.assertIn(".chat-mode-hint-now", css)
        self.assertIn(".chat-mode-hint-pill", css)

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
        self.assertNotIn("localStorage", html_src)
        self.assertNotIn("localStorage", login_src)
        self.assertNotIn("localStorage", utils_src)
        self.assertNotIn("sessionStorage", utils_src)
        self.assertNotIn("localStorage.setItem", self.src)
        self.assertNotIn("sessionStorage", self.src.split("function wipeClientUiStorage")[0])
        self.assertIn('api("prefs"', utils_src)
        self.assertIn("function patchPrefs(", utils_src)
        self.assertIn("function persistLayout(", self.src)
        self.assertIn("patchPrefs({ layout })", self.src)
        self.assertIn("patchPrefs({ codeAgent })", self.src)
        self.assertIn("wipeClientUiStorage()", self.src)
        self.assertIn("readLegacyStore()", self.src)
        self.assertIn("TabbyUI.flushPrefs", app_src)


if __name__ == "__main__":
    unittest.main()

