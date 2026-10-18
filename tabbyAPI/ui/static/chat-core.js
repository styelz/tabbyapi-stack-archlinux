/* chat-core.js — Pure helper functions and SVG constants.
 * No DOM, no state. Loaded before chat.js.
 */
(function () {
  "use strict";

  function tabbyChatComposeAction(inFlight, typed, queued) {
    const text = String(typed || "").trim();
    const hasQueue = Boolean(String(queued || "").trim());
    if (!inFlight) return { mode: "send", label: "Send", showSteer: false };
    if (text) return { mode: "queue", label: "Queue", showSteer: hasQueue };
    return { mode: "stop", label: "Stop", showSteer: hasQueue };
  }

  function tabbyChatAbortKeepsGoing(userAbort, assembled, heldJobId, recovered) {
    if (userAbort) return false;
    const blob = `${assembled || ""}\n${recovered || ""}`;
    if (
      /here's the (picture|audio|video|clip)|here are the \d+ (pictures|audio clips|videos)|\/v1\/images\/generated-/i.test(
        blob
      )
    ) {
      return true;
    }
    if (String(assembled || "").replace(/\s+/g, " ").trim()) return true;
    return Boolean(heldJobId);
  }

  function tabbyIsSsePing(text) {
    return /^ping\s*-\s*\d{4}-\d{2}-\d{2}[T\s]\d/i.test(String(text || "").trim());
  }

  function tabbyCollapseStutter(text) {
    const raw = String(text || "");
    const windowSize = 48;
    const maxUnique = 8;
    const words = [];
    const re = /[A-Za-z']+/g;
    let match;
    while ((match = re.exec(raw))) {
      words.push({ token: match[0].toLowerCase(), start: match.index });
    }
    if (words.length < windowSize) return raw;
    const cut = new Array(words.length).fill(false);
    for (let start = 0; start <= words.length - windowSize; start += 1) {
      const counts = new Map();
      for (let i = start; i < start + windowSize; i += 1) {
        const token = words[i].token;
        counts.set(token, (counts.get(token) || 0) + 1);
      }
      let top = 0;
      counts.forEach((count) => {
        if (count > top) top = count;
      });
      if (counts.size <= maxUnique && top >= Math.floor(windowSize / 3)) {
        for (let i = start; i < start + windowSize; i += 1) cut[i] = true;
      }
    }
    if (!cut.some(Boolean)) return raw;
    let out = "";
    let cursor = 0;
    let index = 0;
    while (index < words.length) {
      if (!cut[index]) {
        index += 1;
        continue;
      }
      const start = words[index].start;
      while (index < words.length && cut[index]) index += 1;
      const end = index < words.length ? words[index].start : raw.length;
      out += raw.slice(cursor, start) + "…\n\n";
      cursor = end;
    }
    out += raw.slice(cursor);
    return out.replace(/\n{3,}/g, "\n\n").trim();
  }

  function tabbyIsNetworkDrop(err) {
    if (!err || err.name === "AbortError") return false;
    const msg = String(err.message || "").toLowerCase();
    return /network\s*error|failed to fetch|load failed|networkerror|err_network|err_internet|err_connection|connection (reset|closed)/i.test(msg);
  }

  function tabbyLooksLikeRestart(err, status) {
    if (status === 502 || status === 503 || status === 504) return true;
    if (!err || err.name === "AbortError") return false;
    if (tabbyIsNetworkDrop(err)) return true;
    const msg = String(err.message || "");
    return /API unavailable|API unreachable|service may be restarting/i.test(msg);
  }

  function tabbyNetworkErrorMessage(err) {
    if (tabbyIsNetworkDrop(err)) {
      return "Lost the connection to the API. Retry the message.";
    }
    return String((err && err.message) || err || "Request failed");
  }

  function tabbyCleanStatusLabel(text) {
    return String(text || "")
      .split(/\r?\n/)
      .map((line) => line.replace(/^:\s*/, "").trim())
      .filter((line) => line && !tabbyIsSsePing(line))
      .join(" ")
      .trim();
  }

  function tabbyOccupancyHintIsOwnRun(hint) {
    const text = String(hint || "");
    return /Your session is running/i.test(text)
      || /Your previous request is still running/i.test(text);
  }

  function tabbyStatusLabelPriority(text) {
    const label = String(text || "").trim();
    if (/^(Rendering|Starting Comfy|Reloading the coding model|Planning the picture|Working on the picture)/i.test(label)) {
      return 3;
    }
    if (/^(Starting the picture|Switching|Restarting|Loading|Writing|Summarizing|Continuing)/i.test(label)) {
      return 2;
    }
    if (/^Queued$/i.test(label)) return 1;
    if (/^Thinking$/i.test(label)) return 0;
    return 2;
  }

  function tabbyImageRenderLabel(text) {
    return /^(Starting Comfy|Rendering image |Rendering audio|Rendering video|Rendering in Comfy|Working on the |Reloading the coding model)/i.test(
      String(text || "").trim()
    );
  }

  function tabbyImageDestClass(title, index) {
    const text = String(title || "").trim();
    if (/^Reloading the coding model/i.test(text) || /^Rendered\b/i.test(text)) {
      return "is-done";
    }
    const render = /Rendering (?:image|audio|video) (\d+) of (\d+)/i.exec(text);
    if (!render) {
      if (/^Rendering in Comfy/i.test(text) && Number(index) === 0) return "is-current";
      return "";
    }
    const current = Math.max(0, Number(render[1]) - 1);
    if (index < current) return "is-done";
    if (index === current) return "is-current";
    return "";
  }

  function tabbyImageProgressNote(label) {
    const text = String(label || "").trim();
    if (/^Starting Comfy/i.test(text)) {
      return "Unloading the coding model so Comfy can use the GPU.";
    }
    if (/^Rendering (?:image|audio|video) /i.test(text) || /^Rendering in Comfy/i.test(text)) {
      return "Comfy is rendering on the GPU.";
    }
    if (/^Reloading the coding model/i.test(text)) {
      return "The file is ready. Reloading the coding model onto the GPU.";
    }
    if (/^Working on the /i.test(text)) return "Preparing the GPU.";
    if (/^Queued$/i.test(text)) {
      return "Waiting to start. Next: unload the coding model and hand the GPU to Comfy.";
    }
    return "The GPU is generating media for this page.";
  }

  function tabbyLooksLikeChatNotImage(raw) {
    const text = String(raw || "").trim();
    if (!text) return false;
    if (/^qwen-image:/i.test(text)) return false;
    if (/^(?:please\s+)?(?:can you\s+|could you\s+)?(?:generate|draw|imagine|create|make|render)\b/i.test(text)) {
      return false;
    }
    const asksImage = /\b(?:images?|pictures?|photos?|pics?|posters?|logos?|icons?|banners?|pngs?)\b/i.test(text);
    const question = /^(?:what(?:'s|s)?|why|who|when|where|which|how\s+(?:are|do|does|did|can|to|is|come))\b/i.test(text);
    if (asksImage && !question) return false;
    return (
      /^(?:hi|hello|hey|yo|sup|thanks|thank you|thx|good (?:morning|afternoon|evening)|ok(?:ay)?|sure|yes|no|yep|nope|got it|cool|great)(?:\s|[!.]|$)/i.test(text)
      || /^(?:please\s+)?(?:tell me|explain|help(?:\s+me)?)\b/i.test(text)
      || /^(?:i(?:'m|m)?\s+(?:just\s+)?(?:have|need|want|think|wonder)|i have a question)\b/i.test(text)
      || /^(?:what(?:'s|s)?|why|who|when|where|which)\b/i.test(text)
      || /^(?:is|are|do|does|did|am)\s+(?:the|this|that|it|there|you|we|they|i|these|those)\b/i.test(text)
      || /^(?:can|could|would|should|will)\s+you\s+(?:explain|tell|help|show me how)\b/i.test(text)
      || /^how\s+(?:are|do|does|did|can|to|is|come)\b/i.test(text)
    );
  }

  var CHEVRON_SVG =
    '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="m15 5-7 7 7 7" /></svg>';
  var NAV_STAR_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="m8 1.8 1.85 3.75 4.15.6-3 2.92.7 4.13L8 11.3l-3.7 1.9.7-4.13-3-2.92 4.15-.6z" /></svg>';
  var NAV_RENAME_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M3.5 12.5 12 4l1.5 1.5-8.5 8.5H3.5z" /></svg>';
  var NAV_CLOSE_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M4 4l8 8M12 4l-8 8" /></svg>';
  var TREE_FOLDER_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M2 4.5A1.5 1.5 0 0 1 3.5 3H7l1.2 1.5H12.5A1.5 1.5 0 0 1 14 6v5.5A1.5 1.5 0 0 1 12.5 13h-9A1.5 1.5 0 0 1 2 11.5z" /></svg>';
  var TREE_FILE_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M4 2h5.2L12 4.8V14H4z" /></svg>';
  var FILES_NEW_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M8 3.5v9M3.5 8h9" /></svg>';
  var FILES_UPLOAD_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M8 11.5V3.5M4.5 7 8 3.5 11.5 7"/><path d="M3 13h10" /></svg>';
  var FILES_SITE_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M6.5 3.5H3.5v9h9V9.5"/><path d="M8.5 3.5H12.5V7.5M7.5 8.5 12.5 3.5"/></svg>';
  var FILES_PREVIEW_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M1.5 8s2.4-4.2 6.5-4.2S14.5 8 14.5 8s-2.4 4.2-6.5 4.2S1.5 8 1.5 8z"/><circle cx="8" cy="8" r="1.8" /></svg>';
  var FILES_TERM_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M2.5 3.5h11v9h-11z"/><path d="M5 6.5 7.2 8 5 9.5"/><path d="M8.2 9.5H11" /></svg>';
  var MIC_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><rect x="5.5" y="1.75" width="5" height="7.5" rx="2.5"/><path d="M3.5 8.25a4.5 4.5 0 0 0 9 0"/><path d="M8 12.75v1.75M5.5 14.5h5"/></svg>';
  var PAUSE_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><rect x="4.25" y="3.25" width="2.5" height="9.5" rx="0.6"/><rect x="9.25" y="3.25" width="2.5" height="9.5" rx="0.6"/></svg>';
  var PLAY_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M5 3.25v9.5L13.25 8z"/></svg>';
  var STOP_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><rect x="4" y="4" width="8" height="8" rx="1.25"/></svg>';
  var GIT_PULL_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M8 2.2v7.3"/><path d="M5.1 6.8 8 9.7l2.9-2.9"/><path d="M3 12.9h10"/></svg>';
  var GIT_PUSH_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M8 13.8V6.5"/><path d="M5.1 9.2 8 6.3l2.9 2.9"/><path d="M3 3.1h10"/></svg>';
  var GIT_SYNC_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M13.2 6.15A5.2 5.2 0 0 0 3.7 4.3"/><path d="M3.1 2.25v2.6h2.6"/><path d="M2.8 9.85a5.2 5.2 0 0 0 9.5 1.85"/><path d="M12.9 13.75v-2.6h-2.6"/></svg>';
  var GIT_FETCH_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M8 2.15v5.7"/><path d="M5.35 5.6 8 8.25 10.65 5.6"/><path d="M3.35 10.7h9.3V13.2h-9.3z"/></svg>';
  var GIT_REFRESH_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M13.15 8a5.15 5.15 0 1 1-1.5-3.65"/><path d="M13.2 2.35V5.25H10.3"/></svg>';
  var GIT_BRANCH_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><circle cx="4.7" cy="3.25" r="1.15" fill="currentColor"/><circle cx="11.3" cy="6.05" r="1.15" fill="currentColor"/><circle cx="4.7" cy="12.75" r="1.15" fill="currentColor"/><path d="M4.7 4.45v7.15"/><path d="M4.7 8.05c2.2.2 4.7-.35 5.35-2.05"/></svg>';
  var GIT_COMMIT_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M3.1 8.35 6.35 11.55 12.9 4.5"/></svg>';
  var GIT_MORE_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><circle cx="3.2" cy="8" r="0.95" fill="currentColor"/><circle cx="8" cy="8" r="0.95" fill="currentColor"/><circle cx="12.8" cy="8" r="0.95" fill="currentColor"/></svg>';
  var GIT_CLONE_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M2.9 6.35V4.15h3.05L7.25 5.85H13.1v6.9H2.9z"/><path d="M8 7.15v3.45"/><path d="M6.15 9.05 8 10.85 9.85 9.05"/></svg>';
  var GIT_INIT_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><circle cx="8" cy="8" r="5.15"/><path d="M8 5.2v5.6M5.2 8h5.6"/></svg>';
  var ACT_EXPLORER_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M2 4.5A1.5 1.5 0 0 1 3.5 3H7l1.2 1.5H12.5A1.5 1.5 0 0 1 14 6v5.5A1.5 1.5 0 0 1 12.5 13h-9A1.5 1.5 0 0 1 2 11.5z" /></svg>';
  var ACT_SEARCH_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><circle cx="7" cy="7" r="3.75"/><path d="m10 10 3.5 3.5"/></svg>';
  var ACT_GIT_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><circle cx="4.7" cy="3.25" r="1.15" fill="currentColor"/><circle cx="11.3" cy="6.05" r="1.15" fill="currentColor"/><circle cx="4.7" cy="12.75" r="1.15" fill="currentColor"/><path d="M4.7 4.45v7.15"/><path d="M4.7 8.05c2.2.2 4.7-.35 5.35-2.05"/></svg>';
  var ACT_CHANGES_SVG =
    '<svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M4 2h5.2L12 4.8V14H4z"/><path d="M7 7h3M7 9.5h3M6 7H5.5M6 9.5H5.5"/></svg>';

  window.TabbyChatCore = {
    tabbyChatComposeAction: tabbyChatComposeAction,
    tabbyChatAbortKeepsGoing: tabbyChatAbortKeepsGoing,
    tabbyIsSsePing: tabbyIsSsePing,
    tabbyCollapseStutter: tabbyCollapseStutter,
    tabbyIsNetworkDrop: tabbyIsNetworkDrop,
    tabbyLooksLikeRestart: tabbyLooksLikeRestart,
    tabbyNetworkErrorMessage: tabbyNetworkErrorMessage,
    tabbyCleanStatusLabel: tabbyCleanStatusLabel,
    tabbyOccupancyHintIsOwnRun: tabbyOccupancyHintIsOwnRun,
    tabbyStatusLabelPriority: tabbyStatusLabelPriority,
    tabbyImageRenderLabel: tabbyImageRenderLabel,
    tabbyImageDestClass: tabbyImageDestClass,
    tabbyImageProgressNote: tabbyImageProgressNote,
    tabbyLooksLikeChatNotImage: tabbyLooksLikeChatNotImage,
    CHEVRON_SVG: CHEVRON_SVG,
    NAV_STAR_SVG: NAV_STAR_SVG,
    NAV_RENAME_SVG: NAV_RENAME_SVG,
    NAV_CLOSE_SVG: NAV_CLOSE_SVG,
    TREE_FOLDER_SVG: TREE_FOLDER_SVG,
    TREE_FILE_SVG: TREE_FILE_SVG,
    FILES_NEW_SVG: FILES_NEW_SVG,
    FILES_UPLOAD_SVG: FILES_UPLOAD_SVG,
    FILES_SITE_SVG: FILES_SITE_SVG,
    FILES_PREVIEW_SVG: FILES_PREVIEW_SVG,
    FILES_TERM_SVG: FILES_TERM_SVG,
    MIC_SVG: MIC_SVG,
    PAUSE_SVG: PAUSE_SVG,
    PLAY_SVG: PLAY_SVG,
    STOP_SVG: STOP_SVG,
    GIT_PULL_SVG: GIT_PULL_SVG,
    GIT_PUSH_SVG: GIT_PUSH_SVG,
    GIT_SYNC_SVG: GIT_SYNC_SVG,
    GIT_FETCH_SVG: GIT_FETCH_SVG,
    GIT_REFRESH_SVG: GIT_REFRESH_SVG,
    GIT_BRANCH_SVG: GIT_BRANCH_SVG,
    GIT_COMMIT_SVG: GIT_COMMIT_SVG,
    GIT_MORE_SVG: GIT_MORE_SVG,
    GIT_CLONE_SVG: GIT_CLONE_SVG,
    GIT_INIT_SVG: GIT_INIT_SVG,
    ACT_EXPLORER_SVG: ACT_EXPLORER_SVG,
    ACT_SEARCH_SVG: ACT_SEARCH_SVG,
    ACT_GIT_SVG: ACT_GIT_SVG,
    ACT_CHANGES_SVG: ACT_CHANGES_SVG,
  };

  // Expose as globals so existing references in chat.js and other modules work.
  window.tabbyChatComposeAction = tabbyChatComposeAction;
  window.tabbyChatAbortKeepsGoing = tabbyChatAbortKeepsGoing;
  window.tabbyIsSsePing = tabbyIsSsePing;
  window.tabbyCollapseStutter = tabbyCollapseStutter;
  window.tabbyIsNetworkDrop = tabbyIsNetworkDrop;
  window.tabbyLooksLikeRestart = tabbyLooksLikeRestart;
  window.tabbyNetworkErrorMessage = tabbyNetworkErrorMessage;
  window.tabbyCleanStatusLabel = tabbyCleanStatusLabel;
  window.tabbyOccupancyHintIsOwnRun = tabbyOccupancyHintIsOwnRun;
  window.tabbyStatusLabelPriority = tabbyStatusLabelPriority;
  window.tabbyImageRenderLabel = tabbyImageRenderLabel;
  window.tabbyImageDestClass = tabbyImageDestClass;
  window.tabbyImageProgressNote = tabbyImageProgressNote;
  window.tabbyLooksLikeChatNotImage = tabbyLooksLikeChatNotImage;
  window.CHEVRON_SVG = CHEVRON_SVG;
  window.NAV_STAR_SVG = NAV_STAR_SVG;
  window.NAV_RENAME_SVG = NAV_RENAME_SVG;
  window.NAV_CLOSE_SVG = NAV_CLOSE_SVG;
  window.TREE_FOLDER_SVG = TREE_FOLDER_SVG;
  window.TREE_FILE_SVG = TREE_FILE_SVG;
  window.FILES_NEW_SVG = FILES_NEW_SVG;
  window.FILES_UPLOAD_SVG = FILES_UPLOAD_SVG;
  window.FILES_SITE_SVG = FILES_SITE_SVG;
  window.FILES_PREVIEW_SVG = FILES_PREVIEW_SVG;
  window.FILES_TERM_SVG = FILES_TERM_SVG;
  window.MIC_SVG = MIC_SVG;
  window.PAUSE_SVG = PAUSE_SVG;
  window.PLAY_SVG = PLAY_SVG;
  window.STOP_SVG = STOP_SVG;
  window.GIT_PULL_SVG = GIT_PULL_SVG;
  window.GIT_PUSH_SVG = GIT_PUSH_SVG;
  window.GIT_SYNC_SVG = GIT_SYNC_SVG;
  window.GIT_FETCH_SVG = GIT_FETCH_SVG;
  window.GIT_REFRESH_SVG = GIT_REFRESH_SVG;
  window.GIT_BRANCH_SVG = GIT_BRANCH_SVG;
  window.GIT_COMMIT_SVG = GIT_COMMIT_SVG;
  window.GIT_MORE_SVG = GIT_MORE_SVG;
  window.GIT_CLONE_SVG = GIT_CLONE_SVG;
  window.GIT_INIT_SVG = GIT_INIT_SVG;
  window.ACT_EXPLORER_SVG = ACT_EXPLORER_SVG;
  window.ACT_SEARCH_SVG = ACT_SEARCH_SVG;
  window.ACT_GIT_SVG = ACT_GIT_SVG;
  window.ACT_CHANGES_SVG = ACT_CHANGES_SVG;
})();
