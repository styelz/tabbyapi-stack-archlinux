const GALLERY_IMAGE_ACCEPT =
  "image/png,image/jpeg,image/webp,image/gif,.png,.jpg,.jpeg,.webp,.gif";
const GALLERY_IMAGE_TYPES = /^(image\/(png|jpe?g|webp|gif))$/i;
const GALLERY_IMAGE_NAME = /\.(png|jpe?g|webp|gif)$/i;
const GALLERY_MAX_BYTES = 8 * 1024 * 1024;

let galleryUseJob = null;

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result || "");
      const at = dataUrl.indexOf(",");
      resolve(at >= 0 ? dataUrl.slice(at + 1) : dataUrl);
    };
    reader.onerror = () => reject(new Error("Could not read file."));
    reader.readAsDataURL(blob);
  });
}

function looksLikeGalleryImage(file) {
  if (!file) return false;
  if (GALLERY_IMAGE_TYPES.test(String(file.type || ""))) return true;
  return GALLERY_IMAGE_NAME.test(String(file.name || ""));
}

async function uploadGalleryFiles(fileList) {
  const files = Array.from(fileList || []).filter(looksLikeGalleryImage);
  if (!files.length) throw new Error("Upload a PNG, JPEG, WebP, or GIF.");
  const uploaded = [];
  for (const file of files) {
    if (file.size > GALLERY_MAX_BYTES) {
      throw new Error(`${file.name || "Image"} must be under 8 MB.`);
    }
    const data = await TabbyUI.api("gallery/upload", {
      method: "POST",
      body: {
        bytes_b64: await blobToBase64(file),
        filename: file.name || "image.png",
      },
    });
    uploaded.push({
      name: data.name,
      url: data.url,
      thumb: data.thumb,
    });
  }
  return uploaded;
}

function galleryFigureHtml(item, index) {
  const url = TabbyUI.escapeHtml(TabbyUI.resolveUiUrl(item.url));
  const thumb = TabbyUI.escapeHtml(TabbyUI.resolveUiUrl(item.thumb));
  const name = TabbyUI.escapeHtml(item.name);
  const owner = item.owner ? " · " + TabbyUI.escapeHtml(item.owner) : "";
  const meta = item.mtime
    ? `${TabbyUI.escapeHtml(item.mtime)} · ${TabbyUI.formatBytes(item.size)}`
    : "";
  return `
        <figure class="shot" data-name="${name}" data-index="${index}" data-url="${url}" data-thumb="${thumb}">
          <label class="pick" title="Select">
            <input type="checkbox" aria-label="Select ${name}" />
          </label>
          <a class="open" href="${url}" data-full="${url}">
            <img src="${thumb}" alt="${name}" loading="lazy" />
          </a>
          <figcaption>${name}${owner}${meta ? "<br>" + meta : ""}</figcaption>
        </figure>`;
}

function bindGalleryShiftRange(grid, state) {
  grid.addEventListener("click", (event) => {
    const pick = event.target.closest(".pick");
    if (!pick || !grid.contains(pick)) return;
    const i = Number(pick.closest("figure")?.dataset.index);
    if (!Number.isInteger(i) || !state.boxes[i]) return;
    if (event.shiftKey) {
      const from = state.lastIndex;
      const checked = state.boxes[i].checked;
      event.preventDefault();
      setTimeout(() => {
        const a = Math.min(from, i);
        const z = Math.max(from, i);
        for (let j = a; j <= z; j += 1) {
          if (state.boxes[j]) state.boxes[j].checked = checked;
        }
        if (state.paint) state.paint();
      }, 0);
      return;
    }
    state.lastIndex = i;
  });
}

function selectedGalleryNames(grid) {
  return Array.from(grid.querySelectorAll(".pick input:checked"))
    .map((box) => box.closest("figure")?.dataset.name)
    .filter(Boolean);
}

function selectedGalleryItems(grid) {
  return Array.from(grid.querySelectorAll("figure.shot")).filter((fig) => {
    const box = fig.querySelector(".pick input");
    return Boolean(box && box.checked);
  }).map((fig) => ({
    name: fig.dataset.name || "",
    url: fig.dataset.url || fig.querySelector("a.open")?.dataset.full || "",
    thumb: fig.dataset.thumb || "",
  })).filter((item) => item.name && item.url);
}

function goToChat() {
  if ((location.hash || "").replace("#", "") !== "chat") {
    location.hash = "#chat";
  }
}

function queueGalleryUse(action, items) {
  galleryUseJob = { action, items: Array.isArray(items) ? items.slice() : [] };
  window.dispatchEvent(new CustomEvent("tabby-gallery-use"));
  if (action === "attach" || action === "upload") goToChat();
}

function takeGalleryUse() {
  const job = galleryUseJob;
  galleryUseJob = null;
  return job;
}

function pickGallery({
  title = "Choose images",
  confirm = "Attach",
  multiple = true,
} = {}) {
  return new Promise((resolve) => {
    const wrap = document.createElement("div");
    wrap.className = "dialog-modal";
    wrap.setAttribute("role", "dialog");
    wrap.setAttribute("aria-modal", "true");
    wrap.innerHTML =
      '<div class="dialog-card gallery-pick">' +
      "<h2></h2>" +
      '<div class="toolbar gallery-pick-bar">' +
      '<button type="button" class="btn gallery-pick-upload">Upload</button>' +
      '<span class="gallery-pick-count">0 selected</span>' +
      '<span class="spacer"></span>' +
      '<div class="pager gallery-pick-pager"></div>' +
      "</div>" +
      '<input class="gallery-pick-file" type="file" accept="' +
      GALLERY_IMAGE_ACCEPT +
      '" multiple hidden />' +
      '<div class="grid gallery-pick-grid"></div>' +
      '<p class="error gallery-pick-error" hidden></p>' +
      '<div class="dialog-actions">' +
      '<button type="button" class="btn dialog-no">Cancel</button>' +
      '<button type="button" class="btn primary dialog-yes"></button>' +
      "</div></div>";
    wrap.querySelector("h2").textContent = title;
    wrap.querySelector(".dialog-yes").textContent = confirm;
    const grid = wrap.querySelector(".gallery-pick-grid");
    const pager = wrap.querySelector(".gallery-pick-pager");
    const countEl = wrap.querySelector(".gallery-pick-count");
    const errorEl = wrap.querySelector(".gallery-pick-error");
    const yesBtn = wrap.querySelector(".dialog-yes");
    const fileInput = wrap.querySelector(".gallery-pick-file");
    const state = { boxes: [], lastIndex: 0, page: 1, paint: null };
    let kept = new Set();

    function showError(message) {
      errorEl.hidden = !message;
      errorEl.textContent = message || "";
    }

    function paint() {
      state.boxes.forEach((box) => {
        const fig = box.closest("figure");
        if (fig) fig.classList.toggle("is-on", box.checked);
      });
      const n = selectedGalleryNames(grid).length;
      countEl.textContent = `${n} selected`;
      yesBtn.disabled = !n;
    }
    state.paint = paint;

    async function load(nextPage, extraNames) {
      kept = new Set(selectedGalleryNames(grid));
      (extraNames || []).forEach((name) => name && kept.add(name));
      const target = nextPage || 1;
      if (target !== state.page) state.lastIndex = 0;
      state.page = target;
      showError("");
      const data = await TabbyUI.api(`gallery/list?page=${state.page}&per_page=24`);
      if (!Array.isArray(data.items) || !data.items.length) {
        grid.innerHTML = "<p class='muted'>No images yet. Upload a photo or generate one in Chat.</p>";
        pager.innerHTML = "";
        state.boxes = [];
        paint();
        return;
      }
      grid.innerHTML = data.items.map((item, index) => galleryFigureHtml(item, index)).join("");
      state.boxes = Array.from(grid.querySelectorAll(".pick input"));
      state.boxes.forEach((box) => {
        const name = box.closest("figure")?.dataset.name;
        box.checked = Boolean(name && kept.has(name));
      });
      const links = [];
      for (let n = 1; n <= data.pages; n += 1) {
        links.push(
          n === data.page
            ? `<span class="btn is-current" aria-current="page">${n}</span>`
            : `<button type="button" class="btn" data-page="${n}">${n}</button>`
        );
      }
      pager.innerHTML = `Page ${data.page} / ${data.pages} · ${data.total} ` + links.join("");
      pager.querySelectorAll("button[data-page]").forEach((btn) => {
        btn.addEventListener("click", () => load(Number(btn.dataset.page)));
      });
      paint();
    }

    function finish(value) {
      document.removeEventListener("keydown", onKey);
      wrap.remove();
      resolve(value);
    }

    function onKey(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        finish(null);
      }
      if (event.key === "Enter" && !yesBtn.disabled) {
        event.preventDefault();
        finish(selectedGalleryItems(grid));
      }
    }

    bindGalleryShiftRange(grid, state);
    grid.addEventListener("change", (event) => {
      if (event.target.matches(".pick input")) {
        if (!multiple && event.target.checked) {
          state.boxes.forEach((box) => {
            if (box !== event.target) box.checked = false;
          });
        }
        paint();
      }
    });
    grid.addEventListener("click", (event) => {
      if (event.target.closest(".pick")) return;
      const fig = event.target.closest("figure.shot");
      if (!fig || !grid.contains(fig)) return;
      event.preventDefault();
      const box = fig.querySelector(".pick input");
      if (!box) return;
      if (event.detail === 2) {
        box.checked = true;
        if (!multiple) {
          state.boxes.forEach((other) => {
            if (other !== box) other.checked = false;
          });
        }
        paint();
        finish(selectedGalleryItems(grid));
        return;
      }
      box.checked = !box.checked;
      if (!multiple && box.checked) {
        state.boxes.forEach((other) => {
          if (other !== box) other.checked = false;
        });
      }
      state.lastIndex = Number(fig.dataset.index) || state.lastIndex;
      paint();
    });
    wrap.querySelector(".gallery-pick-upload").addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      const files = fileInput.files;
      fileInput.value = "";
      if (!files || !files.length) return;
      uploadGalleryFiles(files)
        .then((uploaded) => load(1, uploaded.map((item) => item.name)))
        .catch((err) => showError(err.message));
    });
    wrap.querySelector(".dialog-no").addEventListener("click", () => finish(null));
    yesBtn.addEventListener("click", () => finish(selectedGalleryItems(grid)));
    wrap.addEventListener("click", (event) => {
      if (event.target === wrap) finish(null);
    });
    wrap.addEventListener("dragover", (event) => {
      if (Array.from(event.dataTransfer.types || []).includes("Files")) {
        event.preventDefault();
        wrap.classList.add("is-drop");
      }
    });
    wrap.addEventListener("dragleave", (event) => {
      if (event.relatedTarget && wrap.contains(event.relatedTarget)) return;
      wrap.classList.remove("is-drop");
    });
    wrap.addEventListener("drop", (event) => {
      event.preventDefault();
      wrap.classList.remove("is-drop");
      const files = event.dataTransfer && event.dataTransfer.files;
      if (!files || !files.length) return;
      uploadGalleryFiles(files)
        .then((uploaded) => load(1, uploaded.map((item) => item.name)))
        .catch((err) => showError(err.message));
    });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(wrap);
    yesBtn.disabled = true;
    load(1).catch((err) => showError(err.message));
    wrap.querySelector(".gallery-pick-upload").focus();
  });
}

if (window.TabbyUI) {
  TabbyUI.pickGallery = pickGallery;
  TabbyUI.queueGalleryUse = queueGalleryUse;
  TabbyUI.takeGalleryUse = takeGalleryUse;
}

function mountGallery(root) {
  root.innerHTML = `
    <div class="page-head">
      <div>
        <h1>Gallery</h1>
        <p class="page-sub">Generated and uploaded images on this host.</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" id="gal-upload">Upload</button>
        <input id="gal-file" type="file" accept="${GALLERY_IMAGE_ACCEPT}" multiple hidden />
        <button class="btn" type="button" id="gal-attach" disabled>Attach to chat</button>
        <span class="muted" id="sel-count">0 selected</span>
        <button class="btn" id="del-sel" disabled>Delete selected</button>
        <button class="btn danger" id="del-all">Delete all</button>
        <div class="pager" id="pager"></div>
      </div>
    </div>
    <div class="grid" id="grid"></div>
    <p class="error" id="gallery-error" hidden></p>
    <div class="modal" id="modal">
      <div class="modal-inner">
        <img alt="" />
        <div class="modal-bar">
          <span class="modal-name" id="modal-name"></span>
          <a class="btn" id="modal-open" target="_blank" rel="noreferrer">Open original</a>
          <button class="btn" type="button" id="modal-attach">Attach to chat</button>
          <span class="muted">Esc closes</span>
        </div>
      </div>
    </div>
  `;
  const grid = root.querySelector("#grid");
  const pager = root.querySelector("#pager");
  const count = root.querySelector("#sel-count");
  const delSel = root.querySelector("#del-sel");
  const attachBtn = root.querySelector("#gal-attach");
  const uploadBtn = root.querySelector("#gal-upload");
  const fileInput = root.querySelector("#gal-file");
  const errorEl = root.querySelector("#gallery-error");
  const modal = root.querySelector("#modal");
  const modalImg = modal.querySelector("img");
  const modalName = root.querySelector("#modal-name");
  const modalOpen = root.querySelector("#modal-open");
  const modalAttach = root.querySelector("#modal-attach");

  function showError(message) {
    errorEl.hidden = !message;
    errorEl.textContent = message || "";
  }
  let page = 1;
  let loadGen = 0;
  let lastIndex = 0;
  let boxes = [];
  let kept = new Set();
  let isAdmin = false;
  TabbyUI.api("auth/check")
    .then((data) => {
      isAdmin = Boolean(data.is_admin);
    })
    .catch(() => {});

  function selected() {
    return selectedGalleryNames(grid);
  }

  function paint() {
    boxes.forEach((box) => {
      const fig = box.closest("figure");
      if (fig) fig.classList.toggle("is-on", box.checked);
    });
    const n = selected().length;
    count.textContent = `${n} selected`;
    delSel.disabled = !n;
    attachBtn.disabled = !n;
  }

  function snapshot() {
    kept = new Set(selected());
  }

  function restore() {
    boxes.forEach((box) => {
      const name = box.closest("figure")?.dataset.name;
      box.checked = Boolean(name && kept.has(name));
    });
  }

  function bindBoxes() {
    boxes = Array.from(grid.querySelectorAll(".pick input"));
  }

  function applyRange(from, to, checked) {
    const a = Math.min(from, to);
    const z = Math.max(from, to);
    for (let j = a; j <= z; j += 1) {
      if (boxes[j]) boxes[j].checked = checked;
    }
    paint();
  }

  grid.addEventListener("click", (event) => {
    const pick = event.target.closest(".pick");
    if (!pick || !grid.contains(pick)) return;
    const i = Number(pick.closest("figure")?.dataset.index);
    if (!Number.isInteger(i) || !boxes[i]) return;
    if (event.shiftKey) {
      const from = lastIndex;
      // The browser toggles the clicked box before this handler. That
      // new state is the range action (tick or untick). preventDefault
      // then undoes the single toggle; re-apply it across the range.
      const checked = boxes[i].checked;
      event.preventDefault();
      setTimeout(() => applyRange(from, i, checked), 0);
      return;
    }
    lastIndex = i;
  });
  grid.addEventListener("change", (event) => {
    if (event.target.matches(".pick input")) paint();
  });

  async function load(nextPage, extraNames) {
    const gen = ++loadGen;
    snapshot();
    (extraNames || []).forEach((name) => name && kept.add(name));
    const target = nextPage || 1;
    if (target !== page) lastIndex = 0;
    page = target;
    showError("");
    try {
      const data = await TabbyUI.api(`gallery/list?page=${page}&per_page=24`);
      if (gen !== loadGen) return;
      if (!Array.isArray(data.items) || !data.items.length) {
        grid.innerHTML = "<p class='muted'>No images yet. Upload a photo or generate one in Chat.</p>";
        pager.innerHTML = "";
        boxes = [];
        paint();
        return;
      }
      grid.innerHTML = data.items
        .map((item, index) => galleryFigureHtml(item, index))
        .join("");
      bindBoxes();
      restore();
      const links = [];
      for (let n = 1; n <= data.pages; n += 1) {
        links.push(
          n === data.page
            ? `<span class="btn is-current" aria-current="page">${n}</span>`
            : `<button type="button" class="btn" data-page="${n}">${n}</button>`
        );
      }
      pager.innerHTML = `Page ${data.page} / ${data.pages} · ${data.total} images ` + links.join("");
      pager.querySelectorAll("button[data-page]").forEach((btn) => {
        btn.addEventListener("click", () => load(Number(btn.dataset.page)));
      });
      paint();
      showError("");
    } catch (err) {
      if (gen !== loadGen) return;
      showError(err.message);
      throw err;
    }
  }
  function closeModal() {
    if (!modal.classList.contains("is-open")) return;
    modal.classList.remove("is-open");
    modalImg.removeAttribute("src");
  }

  function imageUrl(fig) {
    const link = fig && fig.querySelector("a.open");
    return (link && (link.dataset.full || link.href)) || "";
  }

  function itemFromFig(fig) {
    if (!fig) return null;
    const name = fig.dataset.name || "";
    const url = fig.dataset.url || imageUrl(fig);
    if (!name || !url) return null;
    return { name, url, thumb: fig.dataset.thumb || "" };
  }

  function attachItems(items) {
    if (!items || !items.length) return;
    queueGalleryUse("attach", items);
    closeModal();
  }

  function downloadNamed(url, name) {
    const link = document.createElement("a");
    link.href = url;
    link.download = name || "image.png";
    link.rel = "noreferrer";
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  async function deleteNames(names) {
    if (!names.length) return;
    const yes = await TabbyUI.confirmModal({
      title: "Delete images",
      text: `Delete ${names.length} image(s)?`,
      yes: "Delete",
      no: "Cancel",
    });
    if (!yes) return;
    try {
      await TabbyUI.api("gallery/delete", { method: "POST", body: { names } });
      await load(page);
    } catch (err) {
      showError(err.message);
    }
  }

  async function addLocalFiles(fileList) {
    try {
      showError("");
      const uploaded = await uploadGalleryFiles(fileList);
      await load(1, uploaded.map((item) => item.name));
    } catch (err) {
      showError(err.message);
    }
  }

  grid.addEventListener("contextmenu", (event) => {
    const fig = event.target.closest("figure.shot");
    if (!fig || !grid.contains(fig)) return;
    const name = fig.dataset.name || "";
    const url = imageUrl(fig);
    const box = fig.querySelector(".pick input");
    const on = Boolean(box && box.checked);
    const item = itemFromFig(fig);
    TabbyUI.showContextMenu(event, [
      { label: "Open", run: () => {
        modalImg.src = url;
        modalName.textContent = name;
        modalOpen.href = url;
        modal.classList.add("is-open");
      } },
      { label: "Open original", run: () => window.open(url, "_blank", "noreferrer") },
      { label: "Attach to chat", run: () => attachItems(item ? [item] : []) },
      { label: "Copy URL", run: () => TabbyUI.copyText(url) },
      { label: "Copy name", run: () => TabbyUI.copyText(name) },
      { label: "Download", run: () => downloadNamed(url, name) },
      { sep: true },
      { label: on ? "Deselect" : "Select", run: () => {
        if (!box) return;
        box.checked = !on;
        lastIndex = Number(fig.dataset.index) || lastIndex;
        paint();
      } },
      { label: "Delete", danger: true, run: () => deleteNames([name]) },
    ]);
  });

  modal.addEventListener("contextmenu", (event) => {
    if (!modal.classList.contains("is-open")) return;
    const name = modalName.textContent || "";
    const url = modalImg.getAttribute("src") || modalOpen.href || "";
    if (!url) return;
    TabbyUI.showContextMenu(event, [
      { label: "Open original", run: () => window.open(url, "_blank", "noreferrer") },
      { label: "Attach to chat", run: () => attachItems([{ name, url, thumb: "" }]) },
      { label: "Copy URL", run: () => TabbyUI.copyText(url) },
      { label: "Copy name", run: () => TabbyUI.copyText(name) },
      { label: "Download", run: () => downloadNamed(url, name) },
      { sep: true },
      { label: "Close", run: () => closeModal() },
    ]);
  });

  grid.addEventListener("click", (event) => {
    if (event.target.closest(".pick")) return;
    const link = event.target.closest("a.open");
    if (!link) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    const name = link.closest("figure")?.dataset.name || "";
    modalImg.src = link.dataset.full;
    modalName.textContent = name;
    modalOpen.href = link.dataset.full;
    modal.classList.add("is-open");
  });
  modal.addEventListener("click", (event) => {
    if (event.target.closest("#modal-open") || event.target.closest("#modal-attach")) return;
    closeModal();
  });
  function onKey(event) {
    if (event.key === "Escape") closeModal();
  }
  document.addEventListener("keydown", onKey);
  uploadBtn.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    const files = fileInput.files;
    fileInput.value = "";
    if (!files || !files.length) return;
    addLocalFiles(files);
  });
  attachBtn.addEventListener("click", () => {
    attachItems(selectedGalleryItems(grid));
  });
  modalAttach.addEventListener("click", () => {
    const name = modalName.textContent || "";
    const url = modalImg.getAttribute("src") || modalOpen.href || "";
    if (!name || !url) return;
    attachItems([{ name, url, thumb: "" }]);
  });
  delSel.addEventListener("click", async () => {
    const names = selected();
    if (!names.length) return;
    const yes = await TabbyUI.confirmModal({
      title: "Delete images",
      text: `Delete ${names.length} image(s)?`,
      yes: "Delete",
      no: "Cancel",
    });
    if (!yes) return;
    try {
      await TabbyUI.api("gallery/delete", { method: "POST", body: { names } });
      await load(page);
    } catch (err) {
      showError(err.message);
    }
  });
  root.querySelector("#del-all").addEventListener("click", async () => {
    const yes = await TabbyUI.confirmModal({
      title: "Delete all images",
      text: isAdmin ? "Delete ALL generated images?" : "Delete all of your images?",
      yes: "Delete all",
      no: "Cancel",
    });
    if (!yes) return;
    try {
      await TabbyUI.api("gallery/delete", { method: "POST", body: { all: true } });
      await load(1);
    } catch (err) {
      showError(err.message);
    }
  });

  root.addEventListener("dragover", (event) => {
    if (Array.from(event.dataTransfer.types || []).includes("Files")) {
      event.preventDefault();
      root.classList.add("is-drop");
    }
  });
  root.addEventListener("dragleave", (event) => {
    if (event.relatedTarget && root.contains(event.relatedTarget)) return;
    root.classList.remove("is-drop");
  });
  root.addEventListener("drop", (event) => {
    event.preventDefault();
    root.classList.remove("is-drop");
    const files = event.dataTransfer && event.dataTransfer.files;
    if (!files || !files.length) return;
    addLocalFiles(files);
  });

  load(1).catch((err) => {
    grid.innerHTML = "";
    showError(err.message);
  });
  return {
    pause() {
      closeModal();
    },
    resume() {
      load(page).catch((err) => showError(err.message));
    },
    destroy() {
      document.removeEventListener("keydown", onKey);
    },
  };
}

window.mountGallery = mountGallery;
window.pickGallery = pickGallery;
