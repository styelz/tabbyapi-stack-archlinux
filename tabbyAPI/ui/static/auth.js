(function () {
  const form = document.getElementById("login-form");
  if (!form) return;
  const error = document.getElementById("login-error");
  const button = document.getElementById("login-btn");
  const base = window.TABBY_UI_BASE || "/v1/ui";
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    button.disabled = true;
    try {
      const response = await fetch(`${base}/auth/login`, {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          username: document.getElementById("username").value,
          password: document.getElementById("password").value,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        let msg = "Invalid username or password.";
        const detail = data.detail;
        if (Array.isArray(detail) && detail.length) {
          const first = detail[0];
          msg = (first && (first.msg || first.message)) || String(first);
        } else if (typeof detail === "string" && detail.trim()) {
          msg = detail;
        } else if (typeof data.message === "string" && data.message.trim()) {
          msg = data.message;
        }
        throw new Error(msg);
      }
      window.location.href = `${base}/`;
    } catch (err) {
      error.hidden = false;
      error.textContent = err.message || "Sign-in failed.";
    } finally {
      button.disabled = false;
    }
  });
})();
