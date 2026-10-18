# TabbyAPI Stack UI Improvement Prompts

Work through these in order. Each step has a **prompt** to paste into a Cursor Agent chat and **verify** instructions. Start a fresh Agent chat for each major step. Push and verify on live between steps.

> **Branch:** Create a feature branch before starting: `git checkout -b ui-overhaul`
> **Commit often:** After each verified step, commit: `git add -A && git commit -m "step N: description"`

---

## Phase 1 — Architecture & Maintainability

### Step 1: Create a shared UI state store

**Why:** Right now GPU status, flight state, occupancy, and context usage are scattered across closure variables, `TabbyUI.lastGpuStatus`, window events, and DOM reads. A tiny pub/sub store centralizes all of it.

**Prompt:**
```
Create a new file `tabbyAPI/ui/static/store.js` — a lightweight reactive state store for the TabbyAPI UI. Requirements:

1. Export a global `TabbyStore` on `window` with these methods:
   - `get(key)` — read a value
   - `set(key, value)` — write a value and notify subscribers
   - `patch(key, partialObj)` — shallow-merge into an existing object value
   - `subscribe(key, callback)` — call `callback(newValue, oldValue)` whenever key changes. Return an unsubscribe function.
   - `getAll()` — return a frozen snapshot of the entire state

2. Pre-define these state keys with defaults:
   - `gpu` — `{ status: null, switching: false, switchTarget: "", serving: false, mode: "" }`
   - `flight` — `{ inFlight: false, chatId: "", away: false }`
   - `context` — `{ used: 0, max: 0, estimated: false, hide: true }`
   - `user` — `{ name: "", isAdmin: false }`
   - `page` — `{ current: "chat", previous: "" }`
   - `tasks` — `[]` (background tasks array)

3. Keep it under 80 lines. No dependencies. Vanilla JS IIFE like the other static files.
4. Add `store.js` to the script list in `index.html` — it must load BEFORE `utils.js`.

Do NOT modify any other files yet. This is just the store definition.
```

**Verify:** Open the UI in browser, open DevTools console, type `TabbyStore.get("gpu")` — should return the default object.

---

### Step 2: Split chat.js into modules

**Why:** `chat.js` is 15,500 lines in one function. Splitting it makes each piece independently editable.

**Prompt:**
```
Split `tabbyAPI/ui/static/chat.js` into these separate files. Each file should be a self-contained IIFE that attaches its mount/init function to `window`. The main `chat.js` remains but becomes a thin coordinator that calls into the extracted modules.

Extract these files:

1. `chat-core.js` — The pure helper functions at the top of chat.js (lines 1–190):
   - `tabbyChatComposeAction`, `tabbyChatAbortKeepsGoing`, `tabbyIsSsePing`, `tabbyIsNetworkDrop`, `tabbyLooksLikeRestart`, `tabbyNetworkErrorMessage`, `tabbyCleanStatusLabel`, `tabbyOccupancyHintIsOwnRun`, `tabbyStatusLabelPriority`, `tabbyImageRenderLabel`, `tabbyImageDestClass`, `tabbyImageProgressNote`, `tabbyLooksLikeChatNotImage`
   - The SVG constants (CHEVRON_SVG, NAV_STAR_SVG, etc.)
   - Attach all of these to `window.TabbyChatCore`

2. `code-editor.js` — Extract all Monaco editor tab management:
   - Tab bar rendering (openTab, closeTab, switchTab, tabFromPath)
   - File open/save/close logic
   - Split editor management
   - Editor find bar
   - The Monaco mount and theme sync
   - Attach to `window.TabbyCodeEditor`

3. `code-files.js` — Extract the file tree and git pane:
   - File tree rendering (renderTree, expandFolder, collapseFolder)
   - File filter input
   - Git pane (git status, changes list, history list, git toolbar actions)
   - Upload/download/zip logic
   - Attach to `window.TabbyCodeFiles`

4. `code-terminal.js` — Extract terminal management:
   - xterm initialization and sessions
   - Terminal tabs (new, close, switch)
   - Terminal resize
   - WebSocket connection
   - Attach to `window.TabbyCodeTerminal`

5. `code-preview.js` — Extract the browser preview:
   - Preview iframe management
   - Preview tabs (new, close, switch)
   - Address bar and navigation (back, forward, reload)
   - Preview resize
   - Attach to `window.TabbyCodePreview`

6. `chat.js` stays as the coordinator — it has `mountChat()` which:
   - Builds the HTML shell (the innerHTML template)
   - Calls into each module's init function passing the relevant DOM elements
   - Handles the chat log, streaming, message rendering, compose area, sidebar
   - Coordinates between chat and code modes

Update `index.html` to load the new files in order: `chat-core.js`, `code-editor.js`, `code-files.js`, `code-terminal.js`, `code-preview.js`, then `chat.js`.

IMPORTANT:
- Every function that was accessible between modules via closure must now go through `window.TabbyCodeEditor`, `window.TabbyCodeFiles`, etc.
- Test that Chat mode still works (send a message, switch chats, search)
- Test that Code mode still works (open file, edit, terminal, preview)
- Do not change any functionality — this is a pure refactor
```

**Verify:** Both Chat and Code modes work identically to before. No console errors.

---

### Step 3: Wire the state store into app.js and the new modules

**Prompt:**
```
Now wire `TabbyStore` into the existing code. Modify these files:

1. **app.js** — Replace the scattered GPU/user state:
   - In `refreshHeaderStatus()`, after getting status data, call `TabbyStore.set("gpu", { status: data, switching: ..., serving: ..., mode: ... })` instead of only calling `TabbyUI.paintGpuChip(data)`
   - In the `auth/check` handler, call `TabbyStore.set("user", { name, isAdmin })`
   - In `show(name)`, call `TabbyStore.set("page", { current: name, previous: old })`

2. **chat.js** — Subscribe to store changes:
   - Subscribe to `gpu` for the loading bar, comfy hint, and code-write hint
   - Subscribe to `flight` for the in-flight/away bar
   - Subscribe to `page` to know when chat is active/paused

3. **utils.js** — In `paintGpuChip()` and `paintApiDown()`, also update `TabbyStore.set("gpu", ...)` so any subscriber gets the update

Keep backward compatibility: `TabbyUI.lastGpuStatus` should still work (read from store). The store is the single source of truth; the existing `window.dispatchEvent("tabby-gpu-status")` pattern is kept for now but subscribers can use either.
```

**Verify:** Open DevTools, run `TabbyStore.subscribe("gpu", (v) => console.log("gpu:", v))`. Switch models — you should see updates logged.

---

## Phase 2 — IDE Experience

### Step 4: Enable Monaco minimap, sticky scroll, and breadcrumbs

**Why:** These are free wins — Monaco supports them out of the box. They instantly make the Code editor feel professional.

**Prompt:**
```
In `tabbyAPI/ui/static/monaco.js`, find where Monaco editors are created (the `monaco.editor.create()` calls). Add these editor options:

1. **Minimap:** `minimap: { enabled: true, scale: 1, showSlider: "mouseover" }`
2. **Sticky scroll:** `stickyScroll: { enabled: true }`
3. **Bracket pair colorization:** `bracketPairColorization: { enabled: true }`
4. **Smooth scrolling:** `smoothScrolling: true`
5. **Cursor smooth animation:** `cursorSmoothCaretAnimation: "on"`
6. **Guides:** `guides: { bracketPairs: true, indentation: true }`

Also add a breadcrumb bar above the editor. In the Code mode editor column (`chat-editor-col`), add a `.editor-breadcrumb` div above the editor that shows the current file path split by `/` as clickable segments. Style it:

```css
.editor-breadcrumb {
  display: flex;
  align-items: center;
  gap: 0.15rem;
  padding: 0.25rem 0.75rem;
  font-size: 0.75rem;
  color: var(--muted);
  background: var(--bg-elev);
  border-bottom: 1px solid var(--line);
  overflow: hidden;
  flex: 0 0 auto;
}
.editor-breadcrumb span { white-space: nowrap; }
.editor-breadcrumb .sep { opacity: 0.4; }
.editor-breadcrumb .crumb { cursor: pointer; }
.editor-breadcrumb .crumb:hover { color: var(--accent); }
.editor-breadcrumb .crumb:last-child { color: var(--text); font-weight: 550; }
```

When the user clicks a breadcrumb segment that's a folder, trigger the file tree to expand/scroll to that folder. When a file tab changes, update the breadcrumb.
```

**Verify:** Open a file in Code mode. You should see a minimap on the right edge, breadcrumbs above the editor showing `folder / subfolder / file.js`, and sticky function headers when scrolling.

---

### Step 5: Add a command palette

**Why:** This is the single most "IDE-like" feature. `Ctrl+Shift+P` opens a fuzzy-search dropdown for every action.

**Prompt:**
```
Create `tabbyAPI/ui/static/palette.js` (this file already exists but may be empty or minimal — check first and extend it). Implement a command palette:

1. **Trigger:** `Ctrl+Shift+P` (or `Cmd+Shift+P` on Mac) opens it. `Escape` closes it.

2. **UI:** A centered modal overlay at the top of the viewport (like VS Code):
   - Dark semi-transparent backdrop
   - A card at `top: 15%`, centered horizontally, `width: min(36rem, 90vw)`
   - A search input at the top with placeholder "Type a command..."
   - A scrollable list of matching commands below

3. **Commands registry:** `window.TabbyPalette.register(id, { label, group, icon?, kbd?, run })`. Groups:
   - **Navigation:** Go to Chat, Go to Status, Go to Models, Go to Gallery, Go to Logs, Go to Settings
   - **Chat:** New chat, Search chats, Find in chat, Export markdown, Copy entire chat, Regenerate last reply, Continue in new chat
   - **Code:** Jump to file (Ctrl+P), Find in files, Save file, Toggle terminal, New terminal, Toggle preview, Split editor
   - **GPU:** Switch to qwen, Switch to gemma, Switch to comfy, Restart API
   - **Theme:** each theme family × mode combination
   - **Account:** Keyboard shortcuts, Download backup, Restore backup, Full screen, Log out

4. **Fuzzy matching:** Filter commands by fuzzy substring match on label + group. Highlight matching characters in the results.

5. **Keyboard navigation:** Arrow up/down to move selection, Enter to execute selected command, Escape to close.

6. **CSS classes:** `.cmd-palette`, `.cmd-palette-backdrop`, `.cmd-palette-card`, `.cmd-palette-input`, `.cmd-palette-list`, `.cmd-palette-item`, `.cmd-palette-item.is-active`, `.cmd-palette-group`, `.cmd-palette-kbd`, `.cmd-palette-match` (highlighted chars).

Add the CSS to `styles.css` and load `palette.js` in `index.html` after `utils.js` but before `chat.js`.

Register all the above commands in `app.js` after the pages and menus are set up. Each command's `run` function should call the same code the existing buttons/menus call.
```

**Verify:** Press `Ctrl+Shift+P`, type "theme" — see theme options. Type "terminal" — see toggle terminal. Press Enter on one — it executes. Press Escape — palette closes.

---

### Step 6: Add activity bar in Code mode

**Why:** The file tree, search, and git sections are stacked vertically in one panel. An activity bar lets users switch between them.

**Prompt:**
```
Add an activity bar to the Code mode file panel (`chat-files` aside). This is a narrow icon strip on the LEFT edge of the files pane (inside it, not a separate column).

1. **Icons (4):** Explorer (folder icon), Search (magnifying glass), Git (branch icon), Changes (diff icon). Use inline SVGs like the rest of the UI.

2. **Behavior:**
   - Clicking an icon shows that section as the primary content of the files pane
   - Explorer = file tree (current default)
   - Search = a "Find in files" panel with input, results list (uses the existing project-find functionality)
   - Git = git pane (branches, toolbar, status)
   - Changes = changes pane (file list with diff markers)
   - History stays as a collapsible section at the bottom of Git view

3. **Layout change:**
   - `.chat-files` gets `display: grid; grid-template-columns: 2.25rem 1fr;`
   - Left column is `.code-activity-bar` — vertical icon buttons
   - Right column is `.code-activity-content` — the active panel content

4. **CSS:**
   ```css
   .code-activity-bar {
     display: flex;
     flex-direction: column;
     align-items: center;
     gap: 0.125rem;
     padding: 0.375rem 0;
     border-right: 1px solid var(--line);
     background: var(--bg);
   }
   .code-activity-btn {
     display: flex;
     align-items: center;
     justify-content: center;
     width: 1.75rem;
     height: 1.75rem;
     border: 0;
     border-radius: 0.375rem;
     background: transparent;
     color: var(--muted);
     cursor: pointer;
   }
   .code-activity-btn:hover { color: var(--text); background: var(--hover-fill); }
   .code-activity-btn.is-active {
     color: var(--accent);
     background: rgba(var(--accent-rgb), 0.14);
   }
   .code-activity-btn svg { width: 1rem; height: 1rem; }
   ```

5. The files pane header (Files, count, actions) adapts to show the active section's title and relevant actions only.

Move the file tree, git pane, and changes pane rendering into their respective sections. The "Search" section is new — it reuses the project-find input but shows results as a list of file matches you can click to open.
```

**Verify:** In Code mode, the files pane now has a narrow icon bar on its left. Click the Git icon — the pane shows git status and branches. Click Explorer — back to file tree. Click Search — a search input with results.

---

### Step 7: Inline diff view for agent file changes in chat

**Why:** When the AI agent writes or edits files, the chat shows collapsed tool calls. Professional AI IDEs show inline diffs.

**Prompt:**
```
When the Code agent executes `Write` or `StrReplace` tool calls, show an inline diff preview in the chat message bubble instead of (or in addition to) the raw collapsed tool args.

1. **For `StrReplace`:** Show a unified diff view:
   - Red lines for `old_string` (deleted)
   - Green lines for `new_string` (added)
   - File path as header
   - Use the existing diff CSS variables (`--diff-add-line`, `--diff-del-line`, etc.)

2. **For `Write`:** Show a collapsible preview of the written file:
   - File path as header with a "Created" or "Updated" badge
   - First 20 lines syntax-highlighted (using TabbyHighlight)
   - "Show more" / "Show less" toggle if file is longer
   - An "Open in editor" button that switches to Code mode and opens that file

3. **CSS classes:**
   ```css
   .agent-diff {
     margin: 0.3em 0;
     border: 1px solid var(--line);
     border-radius: 0.5rem;
     overflow: hidden;
     font-family: var(--mono);
     font-size: 0.75rem;
     line-height: 1.5;
   }
   .agent-diff-head {
     display: flex;
     align-items: center;
     gap: 0.5rem;
     padding: 0.375rem 0.625rem;
     background: var(--bg-elev-2);
     border-bottom: 1px solid var(--line);
     font-family: var(--sans);
     font-weight: 550;
   }
   .agent-diff-badge {
     font-size: 0.625rem;
     padding: 0.0625rem 0.375rem;
     border-radius: 999px;
   }
   .agent-diff-badge.is-created { background: rgba(var(--ok-rgb), 0.2); color: var(--ok); }
   .agent-diff-badge.is-edited { background: rgba(var(--accent-rgb), 0.2); color: var(--accent); }
   .agent-diff-open {
     margin-left: auto;
     font-size: 0.6875rem;
     color: var(--accent);
     cursor: pointer;
     background: none;
     border: 0;
   }
   .agent-diff-open:hover { text-decoration: underline; }
   .agent-diff-body {
     padding: 0.375rem 0;
     overflow-x: auto;
   }
   .agent-diff-line {
     display: block;
     padding: 0 0.625rem;
     white-space: pre;
   }
   .agent-diff-line.is-add { background: var(--diff-add-line); }
   .agent-diff-line.is-del { background: var(--diff-del-line); }
   .agent-diff-line.is-ctx { opacity: 0.7; }
   ```

4. Modify the agent step rendering in `chat.js` (the function that renders tool calls in the chat log). When a tool call is `Write` or `StrReplace`, instead of showing raw args in `.agent-step-args`, render the diff/preview widget. Keep the collapsed raw output available via "Show raw" toggle for debugging.

5. The "Open in editor" button should: switch to Code mode if not already, open the file in a tab, and scroll to the changed region.
```

**Verify:** In Code mode, ask the agent to edit a file. The chat log should show a colored diff inline — red for removed lines, green for added lines, with the filename as a header. Click "Open in editor" — it opens the file.

---

## Phase 3 — Visual Polish

### Step 8: Typography and spacing refinements

**Prompt:**
```
Make these CSS refinements in `tabbyAPI/ui/static/styles.css` for a more professional look:

1. **Chat message bubbles:**
   - Increase line-height to 1.65 for `.bubble p` and `.bubble li`
   - Add `letter-spacing: -0.006em` to `.bubble`
   - Slightly increase paragraph spacing: `.bubble p + p { margin-top: 0.6em; }`

2. **Chat/Code mode toggle:** Replace the rainbow gradient on `.chat-mode-btn.is-active` with a subtle solid accent:
   ```css
   .chat-mode-btn.is-active,
   .chat-mode-btn.is-active:hover {
     background: var(--accent);
     color: var(--mode-on-fg);
     box-shadow: 0 1px 0.125rem rgba(0, 0, 0, 0.18);
   }
   ```

3. **Panel depth:** Add subtle elevation to key panels:
   - `.chat-sidebar { box-shadow: 1px 0 0 0 var(--line); }`
   - `.chat-files { box-shadow: -1px 0 0 0 var(--line); }`
   - `.chat-term { box-shadow: 0 -1px 0 0 var(--line); }`
   - `.chat-preview { box-shadow: -1px 0 0 0 var(--line); }`

4. **Assistant turns:** Add a subtle left border accent:
   ```css
   .chat-turn-ast .bubble {
     border-left: 2px solid rgba(var(--accent-rgb), 0.35);
     padding-left: calc(0.875rem + 2px);
   }
   ```

5. **Inline code in chat:** Improve contrast:
   ```css
   .bubble code {
     padding: 0.125em 0.35em;
     border-radius: 0.3em;
     background: var(--code-inline-bg);
     font-size: 0.875em;
     font-weight: 500;
   }
   ```

6. **Scrollbar refinement:** Make scrollbar thumbs slightly wider in the editor:
   ```css
   .chat-editor *::-webkit-scrollbar { width: 0.75rem; }
   ```

7. **Focus states:** Ensure all interactive elements have visible focus rings for accessibility — audit buttons that might be missing `focus-visible` styles.

Keep every existing theme working. These changes use CSS variables so they adapt to all 10 theme variants.
```

**Verify:** Chat messages should look slightly more spacious and readable. The Chat/Code toggle should be a solid accent color. Assistant messages have a subtle left accent border.

---

### Step 9: Loading states and skeleton screens

**Prompt:**
```
Replace spinner loading states with skeleton screens for a more polished feel:

1. **File tree loading:** When the file tree is loading, show 8 skeleton rows:
   ```css
   .skeleton-row {
     height: 1.375rem;
     margin: 0.25rem 0.5rem;
     border-radius: 0.25rem;
     background: linear-gradient(90deg, var(--bg-elev-2) 25%, var(--hover-fill) 50%, var(--bg-elev-2) 75%);
     background-size: 200% 100%;
     animation: skeleton-shimmer 1.5s ease-in-out infinite;
   }
   @keyframes skeleton-shimmer {
     0% { background-position: 200% 0; }
     100% { background-position: -200% 0; }
   }
   ```
   Vary widths: 60%, 80%, 45%, 70%, 55%, 75%, 40%, 65%.

2. **Chat loading:** When switching to a chat that's loading messages, show 3 skeleton message bubbles (alternating left/right alignment to mimic user/assistant turns).

3. **Gallery loading:** Show a grid of skeleton cards matching the gallery figure dimensions.

4. **Status page loading:** Show skeleton cards for the status facts area.

5. **Editor loading:** Keep the current spinner for Monaco (it genuinely takes a moment to initialize), but add a pulsing file-icon placeholder behind it.

Add a utility function `TabbyUI.skeletonRows(count, container)` that creates the skeleton DOM and a `TabbyUI.clearSkeleton(container)` that removes them.
```

**Verify:** Navigate to Code mode with a workspace — the file tree shows shimmering skeleton rows before the tree loads. Switch chats — brief skeleton bubbles before messages appear.

---

### Step 10: Empty state improvements

**Prompt:**
```
Improve empty states across all pages to be more helpful and visually appealing:

1. **Gallery (empty):** Instead of just "No images yet", show:
   - A large muted image icon (use the gallery SVG from the rail, scaled up to 3rem)
   - "No images yet" heading
   - "Generated images, audio, and video will appear here. Switch to Comfy in chat and describe what you want." subtext
   - A "Switch to Comfy" button that calls `TabbyUI.switchGpu("comfy")`

2. **Code mode (no workspace):** When a Code chat has no files:
   - Show a large folder icon
   - "Empty workspace" heading
   - "Upload files, clone a repo, or ask the agent to create a project." subtext
   - Three action buttons: "Upload files", "Clone repo", "New file"

3. **Logs (no logs):** Show a subtle terminal icon with "No log output yet."

4. **Chat sidebar (no chats):** Currently shows nothing. Show:
   - "No chats yet"
   - "Start typing below to begin."

5. **CSS for empty states:**
   ```css
   .empty-state {
     display: flex;
     flex-direction: column;
     align-items: center;
     justify-content: center;
     gap: 0.75rem;
     padding: 2rem 1rem;
     text-align: center;
     color: var(--muted);
     flex: 1 1 auto;
   }
   .empty-state-icon {
     width: 3rem;
     height: 3rem;
     opacity: 0.4;
   }
   .empty-state-icon svg { width: 100%; height: 100%; }
   .empty-state h3 {
     margin: 0;
     font-size: 1rem;
     font-weight: 600;
     color: var(--text);
     opacity: 0.7;
   }
   .empty-state p {
     margin: 0;
     font-size: 0.8125rem;
     max-width: 24rem;
     line-height: 1.5;
   }
   .empty-state-actions {
     display: flex;
     gap: 0.5rem;
     margin-top: 0.25rem;
   }
   ```
```

**Verify:** Open Gallery with no images — see the helpful empty state with an action button. Open a Code chat with no files — see the workspace empty state.

---

## Phase 4 — Smart Workflows

### Step 11: Workspace templates

**Prompt:**
```
When creating a new Code chat workspace (or when the Code mode empty state shows), offer workspace templates:

1. **Add a template picker** to the Code empty state (from Step 10). Show cards for:
   - **Blank** — empty workspace, just a new file
   - **Website** — creates `index.html`, `styles.css`, `app.js` with boilerplate
   - **Python** — creates `main.py`, `requirements.txt`, `.gitignore`
   - **Node.js** — creates `package.json`, `index.js`, `.gitignore`

2. **Template files** — create `tabbyAPI/ui/templates/` directory with JSON files:
   - `website.json`: `{ "files": { "index.html": "<!DOCTYPE html>...", "styles.css": "/* ... */", "app.js": "// ..." } }`
   - `python.json`: `{ "files": { "main.py": "...", "requirements.txt": "", ".gitignore": "..." } }`
   - `node.json`: `{ "files": { "package.json": "...", "index.js": "...", ".gitignore": "..." } }`

3. **Backend endpoint:** Add `POST /v1/ui/workspace/{chat_id}/template` to `router.py` that:
   - Accepts `{ "template": "website" }`
   - Reads the template JSON
   - Writes each file into the workspace
   - Returns `{ "ok": true, "files": [...] }`

4. **Frontend:** When a template card is clicked:
   - POST to the template endpoint
   - Refresh the file tree
   - Open the main file (index.html, main.py, or index.js) in the editor

5. **CSS for template cards:**
   ```css
   .template-grid {
     display: grid;
     grid-template-columns: repeat(auto-fill, minmax(8rem, 1fr));
     gap: 0.75rem;
     max-width: 28rem;
   }
   .template-card {
     display: flex;
     flex-direction: column;
     align-items: center;
     gap: 0.5rem;
     padding: 1rem 0.75rem;
     border: 1px solid var(--line);
     border-radius: var(--radius);
     background: var(--bg-elev);
     cursor: pointer;
     text-align: center;
     font-size: 0.8125rem;
     font-weight: 550;
   }
   .template-card:hover {
     border-color: rgba(var(--accent-rgb), 0.4);
     background: var(--hover-fill);
   }
   .template-card svg { width: 1.5rem; height: 1.5rem; color: var(--accent); }
   ```
```

**Verify:** Switch to Code mode in a new chat. See template cards. Click "Website" — files are created and `index.html` opens in the editor.

---

### Step 12: Background tasks panel

**Prompt:**
```
Create a unified background tasks panel that shows all ongoing GPU operations:

1. **Trigger:** A small notification bell/badge icon in the header (next to the GPU chip). Shows a count badge when tasks are active.

2. **Panel:** Clicking it opens a dropdown panel listing:
   - Model switches (with spinner and target name)
   - Image/audio/video renders (with progress %)
   - Model downloads (with progress %)
   - API restarts (with status)
   - Git operations in Code workspaces

3. **State:** Use `TabbyStore.get("tasks")` / `TabbyStore.set("tasks", [...])`. Each task:
   ```js
   { id, type: "switch"|"render"|"download"|"restart"|"git", label, progress: null|0-100, status: "running"|"done"|"failed", startedAt }
   ```

4. **Integration points:**
   - `switchGpu()` in app.js → add/update/remove a task
   - Image render progress events → add/update/remove
   - Download jobs in models.js → add/update/remove
   - Restart flow → add/update/remove

5. **Auto-dismiss:** Completed tasks fade out after 5 seconds. Failed tasks stay until dismissed.

6. **CSS:**
   ```css
   .tasks-menu { position: relative; }
   .tasks-badge {
     position: absolute;
     top: -0.125rem;
     right: -0.125rem;
     min-width: 0.875rem;
     height: 0.875rem;
     padding: 0 0.2rem;
     border-radius: 999px;
     background: var(--accent);
     color: var(--mode-on-fg);
     font-size: 0.5625rem;
     font-weight: 700;
     display: flex;
     align-items: center;
     justify-content: center;
   }
   .tasks-panel {
     position: absolute;
     right: 0;
     top: calc(100% + 0.375rem);
     width: min(22rem, calc(100vw - 1.25rem));
     max-height: min(20rem, calc(100vh - 5rem));
     overflow: auto;
     background: var(--bg-elev-2);
     border: 1px solid var(--line);
     border-radius: 0.75rem;
     box-shadow: var(--shadow);
     padding: 0.375rem;
   }
   .tasks-item {
     display: flex;
     align-items: center;
     gap: 0.5rem;
     padding: 0.5rem 0.625rem;
     border-radius: 0.5rem;
     font-size: 0.75rem;
   }
   .tasks-item:hover { background: var(--hover-fill); }
   .tasks-item-progress {
     flex: 1 1 auto;
     height: 0.25rem;
     border-radius: 999px;
     background: var(--line);
     overflow: hidden;
   }
   .tasks-item-fill {
     height: 100%;
     background: var(--accent);
     border-radius: inherit;
     transition: width 0.3s ease;
   }
   .tasks-empty {
     padding: 1rem;
     text-align: center;
     color: var(--muted);
     font-size: 0.75rem;
   }
   ```

Add the tasks icon to `index.html` header area, next to the GPU chip.
```

**Verify:** Switch models — see a task appear in the panel with a spinner. When done, it shows a checkmark and fades. Download a model — progress bar updates.

---

### Step 13: Semantic chat search

**Prompt:**
```
Add semantic search to the chat sidebar using the embeddings API:

1. **UI:** Add a toggle button next to the chat search input: "🔍" for text search (current), "✨" for semantic search.

2. **When semantic mode is active:**
   - On submit (Enter), take the query text
   - POST to `/v1/embeddings` with `{ "model": "qwen3-embedding", "input": [query] }`
   - Also get embeddings for each chat's title + first user message (cache these in memory, refresh on new chat/rename)
   - Compute cosine similarity between query embedding and each chat's embedding
   - Sort chats by similarity, show top matches with a relevance score badge

3. **Caching:** Store chat embeddings in `sessionStorage` keyed by `tabby-chat-emb-{chatId}`. Only re-embed when chat title changes or on first load.

4. **Fallback:** If the embeddings API returns an error (model not loaded), fall back to text search and show a subtle "Embeddings unavailable — using text search" note.

5. **Performance:** Embed in batches of 20 chats. Show a progress indicator while embedding.

6. **Cosine similarity function:**
   ```js
   function cosineSim(a, b) {
     let dot = 0, magA = 0, magB = 0;
     for (let i = 0; i < a.length; i++) {
       dot += a[i] * b[i];
       magA += a[i] * a[i];
       magB += b[i] * b[i];
     }
     return dot / (Math.sqrt(magA) * Math.sqrt(magB) + 1e-8);
   }
   ```
```

**Verify:** Toggle to semantic search. Type "that chat about the login form" — see results ranked by relevance, even if the word "login" doesn't appear in the chat title.

---

## Phase 5 — Code-Split & Performance

### Step 14: Lazy-load Monaco, xterm, and LSP

**Prompt:**
```
Code-split the heavy Code-mode dependencies so they only load when the user switches to Code mode:

1. **Monaco:** Instead of loading `monaco.js` synchronously in `index.html`, change it to:
   - `index.html` does NOT include `monaco.js` in the script list
   - `code-editor.js` has an `async ensureMonaco()` function that dynamically loads `monaco.js` via a `<script>` tag on first use
   - Show a skeleton/loading state in the editor pane while Monaco loads (~1-2s)
   - Cache the promise so subsequent calls are instant

2. **xterm:** Same pattern:
   - Remove `xterm.js` and `xterm-addon-fit.js` from the sync script list
   - `code-terminal.js` has `async ensureXterm()` that loads both on first terminal open
   - Show "Loading terminal..." placeholder while loading

3. **LSP:** Same pattern:
   - Remove `lsp.js` from sync load
   - `code-editor.js` loads it after Monaco is ready

4. **Preload hints:** Add `<link rel="prefetch">` tags in `index.html` for these scripts so the browser fetches them in the background after the initial page loads:
   ```html
   <link rel="prefetch" href="assets/monaco.js" as="script">
   <link rel="prefetch" href="assets/xterm.js" as="script">
   ```

5. **Measurement:** Add `performance.mark()` calls:
   - `tabby-page-ready` when the initial chat UI is interactive
   - `tabby-monaco-ready` when Monaco finishes loading
   - `tabby-xterm-ready` when xterm finishes loading

The result: Chat mode loads instantly. Code mode loads Monaco/xterm on first switch (with prefetch making it fast).
```

**Verify:** Hard-reload the page. Chat should appear instantly. DevTools Network tab should NOT show Monaco/xterm loading. Switch to Code mode — Monaco loads with a brief skeleton, then the editor appears. Check `performance.getEntriesByType("mark")` for timing.

---

## Phase 6 — Advanced Features

### Step 15: Conversation branching

**Prompt:**
```
Add the ability to branch a conversation from any turn:

1. **UI:** On hover over any chat turn (`.chat-turn`), show a small "Branch from here" icon button in the turn actions (`.chat-actions`). Use a git-branch-style icon.

2. **Behavior:**
   - Click "Branch from here" → creates a new chat
   - The new chat copies all messages UP TO AND INCLUDING the selected turn
   - Opens the new chat with a title like "Branch of [original title]"
   - The original chat is unchanged

3. **Backend:** Add `POST /v1/ui/chats/{chat_id}/branch` to `router.py`:
   - Accepts `{ "up_to_index": N }` (the message index to branch at)
   - Creates a new chat entry
   - Copies messages 0..N from the source chat
   - Returns `{ "chat_id": "new-id", "title": "Branch of ..." }`

4. **Frontend:**
   - After branching, navigate to the new chat
   - Show a toast/notification: "Branched from turn N of [original title]"
   - The chat sidebar shows the new chat

5. **Visual hint:** In the new branched chat, show a subtle note at the top: "Branched from [original chat title] at turn N" with a link back to the original.
```

**Verify:** Open a chat with several turns. Hover over the 3rd turn — see the branch icon. Click it — a new chat opens with the first 3 turns copied. The original chat still has all its turns.

---

### Step 16: Approval workflow for agent file changes

**Prompt:**
```
Add an Accept/Reject workflow for agent file changes in Code mode:

1. **When the agent finishes a response that included Write or StrReplace calls:**
   - Show a "Changes Summary" bar at the bottom of the chat log (above the compose area)
   - List each file that was changed with: filename, type of change (Created/Edited), lines changed
   - Two buttons: "Accept All" (green) and "Revert All" (red)

2. **Accept All:** Dismisses the bar. Changes are already applied (the agent wrote them). This is just confirmation.

3. **Revert All:**
   - For each `StrReplace`: reverse the replacement (swap old_string and new_string, call the workspace API)
   - For each `Write` that created a new file: delete the file
   - For each `Write` that overwrote an existing file: restore from the changes pane's original content (if available) or show "Cannot revert — original content not saved"
   - Refresh the file tree and editor

4. **Per-file control:** Each file row in the summary has its own Accept/Reject buttons for granular control.

5. **CSS:**
   ```css
   .changes-summary {
     display: flex;
     flex-direction: column;
     gap: 0.375rem;
     padding: 0.625rem 0.75rem;
     margin: 0.5rem 0;
     border: 1px solid rgba(var(--accent-rgb), 0.3);
     border-radius: 0.625rem;
     background: var(--bg-elev);
   }
   .changes-summary-head {
     display: flex;
     align-items: center;
     justify-content: space-between;
     font-size: 0.8125rem;
     font-weight: 600;
   }
   .changes-summary-file {
     display: flex;
     align-items: center;
     gap: 0.5rem;
     padding: 0.25rem 0.375rem;
     border-radius: 0.375rem;
     font-size: 0.75rem;
     font-family: var(--mono);
   }
   .changes-summary-file:hover { background: var(--hover-fill); }
   .changes-summary-file .badge {
     font-size: 0.625rem;
     padding: 0.0625rem 0.375rem;
     border-radius: 999px;
   }
   .changes-summary-actions {
     display: flex;
     gap: 0.375rem;
     justify-content: flex-end;
   }
   ```

6. **Persistence:** Save the "pending review" state so if the user refreshes, the summary doesn't disappear until explicitly accepted/rejected.
```

**Verify:** Ask the agent to create or edit files. After it finishes, see a changes summary bar listing each file. Click "Revert All" on a file creation — the file is deleted and the tree updates.

---

## Completion Checklist

After all steps, verify end-to-end:

- [ ] All 10 themes (5 families × dark/light) still work
- [ ] Chat mode: send message, switch chats, search, find, queue, follow-ups
- [ ] Code mode: open file, edit, save, terminal, preview, git, Agent/Ask/Plan
- [ ] Status page: charts render, model switch, restart, update
- [ ] Models page: library, search, download
- [ ] Gallery: grid, lightbox, upload, delete
- [ ] Settings: load, edit, save, restart
- [ ] Users: create, list
- [ ] Logs: live stream
- [ ] Mobile (900px): rail moves to bottom, panels stack
- [ ] Command palette opens with Ctrl+Shift+P
- [ ] Keyboard shortcuts all still work
- [ ] No console errors

Then merge:
```
git checkout main
git merge ui-overhaul
git push origin main
```
