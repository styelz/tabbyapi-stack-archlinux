function mountUsers(root) {
  root.innerHTML = `
    <div class="users-page">
      <div class="page-head">
        <div>
          <h1>Users</h1>
          <p class="page-sub">Tabby-only accounts. They are not Linux users.</p>
        </div>
        <div class="page-actions">
          <button class="btn" type="button" id="users-refresh">Refresh</button>
        </div>
      </div>
      <div class="card">
        <h2>Create account</h2>
        <p class="muted">Tabby-only users. They are not Linux accounts. Extra users can use Chat, Code, Status, Gallery, and Logs, but cannot create users or change Settings.</p>
        <form id="user-create" class="users-form">
          <label>Username
            <input id="new-username" name="username" autocomplete="off" required minlength="3" maxlength="32" />
          </label>
          <label>Password
            <input id="new-password" name="password" type="password" required minlength="8" />
          </label>
          <button class="btn primary" type="submit">Create</button>
        </form>
        <p class="error" id="user-error" hidden></p>
        <p class="muted" id="user-ok" hidden></p>
      </div>
      <div class="card">
        <h2>Accounts</h2>
        <table class="users-table data-table">
          <thead>
            <tr>
              <th>Username</th>
              <th>Created</th>
              <th class="num">Logins</th>
              <th class="num">Chats</th>
              <th class="num">Images</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="users-body"></tbody>
        </table>
        <p class="muted" id="users-empty" hidden>No extra accounts yet.</p>
      </div>
    </div>
  `;
  const form = root.querySelector("#user-create");
  const err = root.querySelector("#user-error");
  const ok = root.querySelector("#user-ok");
  const body = root.querySelector("#users-body");
  const empty = root.querySelector("#users-empty");

  function showError(message) {
    err.hidden = !message;
    err.textContent = message || "";
    if (message) ok.hidden = true;
  }

  function showOk(message) {
    ok.hidden = !message;
    ok.textContent = message || "";
    if (message) err.hidden = true;
  }

  async function load() {
    showError("");
    const data = await TabbyUI.api("users");
    const rows = data.users || [];
    empty.hidden = rows.some((user) => !user.is_admin);
    body.innerHTML = rows
      .map((user) => {
        const name = TabbyUI.escapeHtml(user.username);
        const created = TabbyUI.escapeHtml(user.created_at || (user.is_admin ? "Linux admin" : ""));
        const logins = Number(user.logins || 0);
        const chats = Number(user.chats || 0);
        const images = Number(user.images || 0);
        const actions = user.is_admin
          ? `<span class="muted">PAM account</span>`
          : `<button class="btn" type="button" data-reset="${name}">Reset password</button>
            <button class="btn danger" type="button" data-del="${name}">Delete</button>`;
        return `<tr data-name="${name}">
          <td>${name}${user.is_admin ? ' <span class="muted">admin</span>' : ""}</td>
          <td class="muted">${created}</td>
          <td class="num">${logins}</td>
          <td class="num">${chats}</td>
          <td class="num">${images}</td>
          <td class="users-actions">${actions}</td>
        </tr>`;
      })
      .join("");
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    showError("");
    showOk("");
    try {
      const username = root.querySelector("#new-username").value.trim();
      const password = root.querySelector("#new-password").value;
      await TabbyUI.api("users", { method: "POST", body: { username, password } });
      form.reset();
      showOk(`Created ${username}.`);
      await load();
    } catch (exc) {
      showError(exc.message || "Could not create user.");
    }
  });

  async function resetPassword(username) {
    const password = await TabbyUI.promptModal({
      title: "Reset password",
      text: `New password for ${username}.`,
      label: "Password",
      type: "password",
      minlength: 8,
      yes: "Save",
      no: "Cancel",
    });
    if (!password) return;
    await TabbyUI.api(`users/${encodeURIComponent(username)}/password`, {
      method: "POST",
      body: { password },
    });
    showOk(`Password updated for ${username}.`);
  }

  async function deleteUser(username) {
    const yes = await TabbyUI.confirmModal({
      title: "Delete account",
      text: `Delete ${username}? Their chats are removed. Gallery files stay for admin.`,
      yes: "Delete",
      no: "Cancel",
    });
    if (!yes) return;
    await TabbyUI.api(`users/${encodeURIComponent(username)}`, { method: "DELETE" });
    showOk(`Deleted ${username}.`);
    await load();
  }

  body.addEventListener("contextmenu", (event) => {
    const row = event.target.closest("tr[data-name]");
    if (!row || !body.contains(row)) return;
    const username = row.dataset.name || "";
    const admin = Boolean(row.querySelector(".users-actions .muted"));
    TabbyUI.showContextMenu(event, [
      { label: "Copy username", run: () => TabbyUI.copyText(username) },
      admin ? null : { label: "Reset password", run: () => resetPassword(username).catch((exc) => showError(exc.message || "Request failed.")) },
      admin ? null : { label: "Delete", danger: true, run: () => deleteUser(username).catch((exc) => showError(exc.message || "Request failed.")) },
    ]);
  });

  body.addEventListener("click", async (event) => {
    const reset = event.target.closest("[data-reset]");
    const del = event.target.closest("[data-del]");
    try {
      if (reset) await resetPassword(reset.dataset.reset);
      else if (del) await deleteUser(del.dataset.del);
    } catch (exc) {
      showError(exc.message || "Request failed.");
    }
  });

  root.querySelector("#users-refresh").addEventListener("click", () => {
    load().catch((exc) => showError(exc.message));
  });

  load().catch((exc) => showError(exc.message));
  return {
    resume() {
      load().catch(() => {});
    },
    destroy() {},
  };
}

window.mountUsers = mountUsers;
