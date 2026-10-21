const API_BASE = "";
let CHAT_SESSION_ID = localStorage.getItem("docquery_chat_session_id");
let IS_AUTHENTICATED = localStorage.getItem("docquery_authenticated") === "true";
// Remove the legacy browser-readable bearer token after migrating to cookies.
localStorage.removeItem("docquery_token");
const IS_GUEST_MODE = localStorage.getItem("docquery_guest_mode") === "true";
let lastRenderedDateKey = null;

if (!IS_AUTHENTICATED && !IS_GUEST_MODE) {
  window.location.href = "login.html";
}

const CONFIG = {
  UPLOAD_ENDPOINT: "/api/documents/upload",
  CHAT_ENDPOINT: "/chat",
};

const state = { documentReady: false, lastQuestion: null };
let chatBeingDeleted = null;
let currentPreviewBlobUrl = null;
let currentDocumentFilename = null; // for source-click-to-page

const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");
const dropzoneText = document.getElementById("dropzone-text");
const dropzoneFilename = document.getElementById("dropzone-filename");
const uploadStatus = document.getElementById("upload-status");
const uploadProgress = document.getElementById("upload-progress");
const uploadProgressBar = document.getElementById("upload-progress-bar");

const chatStatus = document.getElementById("chat-status");
const chatLog = document.getElementById("chat-log");
const chatEmpty = document.getElementById("chat-empty");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const chatSend = document.getElementById("chat-send");

const sidebarChatList = document.getElementById("sidebar-chat-list");
const newChatBtn = document.getElementById("new-chat-btn");
const sidebarUserBtn = document.getElementById("sidebar-user-btn");
const sidebarUserMenu = document.getElementById("sidebar-user-menu");
const sidebarUserName = document.getElementById("sidebar-user-name");
const sidebarUserAvatar = document.getElementById("sidebar-user-avatar");

const documentsToggle = document.getElementById("documents-toggle");
const documentsPanel = document.getElementById("documents-panel");
const documentsList = document.getElementById("documents-list");

const deleteOverlay = document.getElementById("delete-confirm-overlay");
const deleteCancelBtn = document.getElementById("delete-cancel-btn");
const deleteConfirmBtn = document.getElementById("delete-confirm-btn");

const currentChatTitle = document.getElementById("current-chat-title");
const sidebar = document.getElementById("sidebar");
const sidebarCollapseBtn = document.getElementById("sidebar-collapse-btn");
const sidebarExpandBtn = document.getElementById("sidebar-expand-btn");

const previewOverlay = document.getElementById("preview-overlay");
const previewFilename = document.getElementById("preview-filename");
const previewIframe = document.getElementById("preview-iframe");
const previewDownloadBtn = document.getElementById("preview-download-btn");
const previewCloseBtn = document.getElementById("preview-close-btn");

const IS_MOBILE = () => window.innerWidth <= 768;

function authHeaders(extra = {}) {
  return extra;
}

/* ============================================
   DOCUMENT PREVIEW MODAL (with optional page jump)
   ============================================ */
async function openDocumentPreview(filename, page) {
  previewFilename.textContent = filename;
  previewIframe.src = "";
  previewOverlay.hidden = false;

  try {
    const res = await fetch(
      API_BASE + `/chats/${CHAT_SESSION_ID}/documents/${encodeURIComponent(filename)}`,
      { headers: authHeaders(), credentials: "include" }
    );
    if (!res.ok) throw new Error("Couldn't load this document.");

    const blob = await res.blob();
    if (currentPreviewBlobUrl) URL.revokeObjectURL(currentPreviewBlobUrl);
    currentPreviewBlobUrl = URL.createObjectURL(blob);

    previewIframe.src = currentPreviewBlobUrl + (page ? `#page=${page}` : "");
    previewDownloadBtn.href = currentPreviewBlobUrl;
    previewDownloadBtn.download = filename;
  } catch (err) {
    console.error(err);
    previewFilename.textContent = filename + " — failed to load";
  }
}

function closePreview() {
  previewOverlay.hidden = true;
  previewIframe.src = "";
  if (currentPreviewBlobUrl) {
    URL.revokeObjectURL(currentPreviewBlobUrl);
    currentPreviewBlobUrl = null;
  }
}

previewCloseBtn.addEventListener("click", closePreview);
previewOverlay.addEventListener("click", (e) => {
  if (e.target === previewOverlay) closePreview();
});

/* ============================================
   SIDEBAR COLLAPSE / MOBILE AUTO-CLOSE
   ============================================ */
function collapseSidebar() {
  sidebar.classList.add("is-collapsed");
  sidebarExpandBtn.hidden = false;
}
function expandSidebar() {
  sidebar.classList.remove("is-collapsed");
  sidebarExpandBtn.hidden = true;
}
sidebarCollapseBtn.addEventListener("click", collapseSidebar);
sidebarExpandBtn.addEventListener("click", expandSidebar);

// On mobile, the sidebar starts open (so it's discoverable) but should
// collapse itself as soon as the user picks or starts a chat, rather than
// staying open and forcing a manual close.


/* ============================================
   CLICK-OUTSIDE-TO-CLOSE
   ============================================ */
document.addEventListener("click", (e) => {
  if (!sidebarUserBtn.contains(e.target) && !sidebarUserMenu.contains(e.target)) {
    sidebarUserMenu.hidden = true;
  }
  if (!documentsToggle.contains(e.target) && !documentsPanel.contains(e.target)) {
    documentsPanel.hidden = true;
  }
});

documentsToggle.addEventListener("click", (e) => {
  e.stopPropagation();
  documentsPanel.hidden = !documentsPanel.hidden;
});

/* ============================================
   USER AREA
   ============================================ */
async function initUserArea() {
  if (IS_AUTHENTICATED) {
    try {
      const res = await fetch(API_BASE + "/auth/me", { headers: authHeaders(), credentials: "include" });
      if (!res.ok) throw new Error("Authentication expired.");
      const data = await res.json();
      sidebarUserName.textContent = data.email;
      sidebarUserAvatar.textContent = data.email[0].toUpperCase();
    } catch {
      IS_AUTHENTICATED = false;
      localStorage.removeItem("docquery_authenticated");
      window.location.href = "login.html";
      return;
    }
    sidebarUserMenu.innerHTML = `
      <button type="button" class="sidebar-user-menu-item" id="settings-btn">Settings</button>
      <button type="button" class="sidebar-user-menu-item" id="learn-more-btn">Learn more</button>
      <button type="button" class="sidebar-user-menu-item" id="quick-question-btn">Quick question</button>
      <div class="sidebar-user-menu-divider"></div>
      <button type="button" class="sidebar-user-menu-item" id="logout-btn">Log out</button>
    `;
  } else {
    sidebarUserName.textContent = "Guest";
    sidebarUserAvatar.textContent = "G";
    sidebarUserMenu.innerHTML = `
      <button type="button" class="sidebar-user-menu-item" id="settings-btn">Settings</button>
      <button type="button" class="sidebar-user-menu-item" id="learn-more-btn">Learn more</button>
      <button type="button" class="sidebar-user-menu-item" id="quick-question-btn">Quick question</button>
      <div class="sidebar-user-menu-divider"></div>
      <a href="login.html" class="sidebar-user-menu-item">Log in</a>
    `;
  }

  sidebarUserBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    sidebarUserMenu.hidden = !sidebarUserMenu.hidden;
  });

  // Placeholders — wire these up once you've decided what each should do.
  document.getElementById("settings-btn")?.addEventListener("click", () => {
    console.log("Settings clicked — not implemented yet");
  });
  document.getElementById("learn-more-btn")?.addEventListener("click", () => {
    window.location.href = "index.html#how-it-works";
  });
  document.getElementById("quick-question-btn")?.addEventListener("click", () => {
    console.log("Quick question clicked — not implemented yet");
  });

  const logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn) {
    logoutBtn.addEventListener("click", () => {
      fetch(API_BASE + "/auth/logout", { method: "POST", credentials: "include" }).catch(() => {}).finally(() => {
        localStorage.removeItem("docquery_authenticated");
        localStorage.removeItem("docquery_token");
        localStorage.removeItem("docquery_chat_session_id");
        localStorage.removeItem("docquery_guest_mode");
        window.location.href = "index.html";
      });
    });
  }
}
/* ============================================
   SIDEBAR CHAT LIST (with rename)
   ============================================ */
async function loadChatList() {
  if (!IS_AUTHENTICATED) {
    sidebarChatList.innerHTML = `<li class="sidebar-chat-hint">Log in to save and revisit chats.</li>`;
    return;
  }

  const res = await fetch(API_BASE + "/chats", { headers: authHeaders(), credentials: "include" });
  if (!res.ok) return;
  const list = await res.json();

  sidebarChatList.innerHTML = "";
  list.forEach((c) => {
    const li = document.createElement("li");
    li.className = "sidebar-chat-item";
    if (c.id === CHAT_SESSION_ID) li.classList.add("is-active");

    const titleSpan = document.createElement("span");
    titleSpan.className = "sidebar-chat-title";
    titleSpan.textContent = c.title || "New chat";

    // Distinguish single-click (open) from double-click (rename) with a
    // short delay — otherwise the click's openChat() call reloads this
    // whole list an instant after dblclick starts the rename input,
    // wiping it out before you can type.
    let clickTimer = null;
    titleSpan.addEventListener("click", () => {
      clearTimeout(clickTimer);
      clickTimer = setTimeout(() => {
        openChat(c.id);
        if (IS_MOBILE()) collapseSidebar();
      }, 220);
    });
    titleSpan.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      clearTimeout(clickTimer);
      startRename(li, titleSpan, c.id, c.title);
    });

    const menuWrap = document.createElement("div");
    menuWrap.className = "sidebar-chat-menu-wrap";

    const menuBtn = document.createElement("button");
    menuBtn.className = "sidebar-chat-menu-btn";
    menuBtn.setAttribute("aria-label", "Chat options");
    menuBtn.innerHTML = "&#8942;"; // vertical ellipsis
    menuBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      document.querySelectorAll(".sidebar-chat-menu.is-open").forEach((m) => {
        if (m !== menu) m.classList.remove("is-open");
      });
      menu.classList.toggle("is-open");
    });

    const menu = document.createElement("div");
    menu.className = "sidebar-chat-menu";
    menu.innerHTML = `
      <button type="button" class="sidebar-chat-menu-item" data-action="rename">Rename</button>
      <button type="button" class="sidebar-chat-menu-item is-danger" data-action="delete">Delete</button>
    `;
    menu.querySelector('[data-action="rename"]').addEventListener("click", (e) => {
      e.stopPropagation();
      menu.classList.remove("is-open");
      startRename(li, titleSpan, c.id, c.title);
    });
    menu.querySelector('[data-action="delete"]').addEventListener("click", (e) => {
      e.stopPropagation();
      menu.classList.remove("is-open");
      chatBeingDeleted = c.id;
      deleteOverlay.hidden = false;
    });

    menuWrap.appendChild(menuBtn);
    menuWrap.appendChild(menu);

    li.appendChild(titleSpan);
    li.appendChild(menuWrap);
    sidebarChatList.appendChild(li);
  });
}

document.addEventListener("click", () => {
  document.querySelectorAll(".sidebar-chat-menu.is-open").forEach((m) => m.classList.remove("is-open"));
});

function formatLocalTime(isoUtcString) {
  const date = new Date(isoUtcString);
  const now = new Date();
  const isToday = date.toDateString() === now.toDateString();

  if (isToday) {
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  return date.toLocaleDateString([], { month: "short", day: "numeric" }) +
    ", " + date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function startRename(li, titleSpan, chatId, currentTitle) {
  const input = document.createElement("input");
  input.className = "sidebar-chat-rename-input";
  input.value = currentTitle || "";
  li.replaceChild(input, titleSpan);
  input.focus();
  input.select();

  const commit = async () => {
    const newTitle = input.value.trim();
    if (newTitle && newTitle !== currentTitle) {
      await fetch(API_BASE + `/chats/${chatId}`, {
        method: "PATCH",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ title: newTitle }),
        credentials: "include",
      });
      if (chatId === CHAT_SESSION_ID) currentChatTitle.textContent = newTitle;
    }
    loadChatList();
  };

  input.addEventListener("blur", commit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") input.blur();
    if (e.key === "Escape") { input.value = currentTitle; input.blur(); }
  });
}

deleteCancelBtn.addEventListener("click", () => {
  chatBeingDeleted = null;
  deleteOverlay.hidden = true;
});

deleteConfirmBtn.addEventListener("click", async () => {
  if (!chatBeingDeleted) return;
  const deletingCurrentChat = chatBeingDeleted === CHAT_SESSION_ID;

  try {
    await fetch(API_BASE + `/chats/${chatBeingDeleted}`, {
      method: "DELETE",
      headers: authHeaders(),
      credentials: "include",
    });
  } catch (err) {
    console.error(err);
  }

  deleteOverlay.hidden = true;
  chatBeingDeleted = null;

  if (deletingCurrentChat) {
    resetChatUI();
  }
  await loadChatList();
});

/* ============================================
   INIT
   ============================================ */
function init() {
  if (IS_MOBILE()) collapseSidebar(); // sidebar starts closed on mobile
  resetChatUI();
  initUserArea();
  loadChatList();

  if (CHAT_SESSION_ID) {
    openChat(CHAT_SESSION_ID);
  }
}

async function openChat(chatId) {
  CHAT_SESSION_ID = chatId;
  localStorage.setItem("docquery_chat_session_id", chatId);

  resetChatUI(false);

  const res = await fetch(API_BASE + `/chats/${chatId}`, { headers: authHeaders(), credentials: "include" });
  if (!res.ok) {
    if (res.status === 403 && IS_GUEST_MODE) {
      resetChatUI();
      showUploadStatus("This guest chat predates the new security check. Start a new guest chat and re-upload your PDF.", true);
      return;
    }
    await loadChatList();
    return;
  }

  const data = await res.json();
  state.documentReady = data.has_documents;
  currentChatTitle.textContent = data.title || "New chat";

  if (data.has_documents) {
    dropzoneText.innerHTML = 'Tap to add another PDF<br><span class="dropzone-subtext">to this chat</span>';
    showUploadStatus("This chat already has document(s) — ask away, or add more.", false);
    enableChat();
  }

  currentDocumentFilename = (data.documents && data.documents[0]) || null;
  renderDocumentsList(data.documents || []);

  data.messages.forEach((m) => {
    const el = appendMessage(m.role === "user" ? "user" : "assistant", m.content, { createdAt: m.created_at });
    if (m.role === "user") state.lastQuestion = m.content;
    if (m.sources && m.sources.length) renderSourceChips(el, m.sources);
  });

  await loadChatList();
}

function resetChatUI(clearSessionId = true) {
  if (clearSessionId) {
    CHAT_SESSION_ID = null;
    localStorage.removeItem("docquery_chat_session_id");
  }
  lastRenderedDateKey = null;
  chatLog.innerHTML = "";
  chatEmpty.hidden = false;
  chatLog.appendChild(chatEmpty);
  state.documentReady = false;
  state.lastQuestion = null;
  currentDocumentFilename = null;
  chatInput.disabled = true;
  chatSend.disabled = true;
  chatStatus.textContent = "Waiting for a document";
  chatStatus.className = "status-pill";
  dropzoneText.innerHTML = 'Drop your PDF here<br><span class="dropzone-subtext">or click to browse</span>';
  dropzoneFilename.textContent = "";
  showUploadStatus("", false);
  renderDocumentsList([]);
  currentChatTitle.textContent = "New chat";
  uploadProgress.hidden = true;
}

newChatBtn.addEventListener("click", () => {
  documentsPanel.hidden = true;
  resetChatUI();
  loadChatList();
  if (IS_MOBILE()) collapseSidebar();
});

function renderDocumentsList(filenames) {
  documentsList.innerHTML = "";
  if (filenames.length === 0) {
    documentsList.innerHTML = `<li class="documents-empty">No documents uploaded yet.</li>`;
    return;
  }
  filenames.forEach((name) => {
    const li = document.createElement("li");
    li.textContent = name;
    li.classList.add("documents-list-item");
    li.addEventListener("click", () => openDocumentPreview(name));
    documentsList.appendChild(li);
  });
}

async function ensureChatSession() {
  if (CHAT_SESSION_ID) return CHAT_SESSION_ID;

  const endpoint = IS_AUTHENTICATED ? "/chats" : "/auth/guest";
  const res = await fetch(API_BASE + endpoint, { method: "POST", headers: authHeaders(), credentials: "include" });
  if (!res.ok) throw new Error("Couldn't start a new chat.");
  const data = await res.json();

  CHAT_SESSION_ID = data.chat_session_id;
  localStorage.setItem("docquery_chat_session_id", CHAT_SESSION_ID);
  return CHAT_SESSION_ID;
}

/* ============================================
   UPLOAD — with real progress (XHR) + pseudo processing steps
   ============================================ */
dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("is-dragover"); });
dropzone.addEventListener("dragleave", () => dropzone.classList.remove("is-dragover"));
dropzone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropzone.classList.remove("is-dragover");
  const file = e.dataTransfer.files[0];
  if (file) handleFile(file);
});
fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  if (file) handleFile(file);
});

const MAX_UPLOAD_MB = 20;

async function handleFile(file) {
  if (file.type !== "application/pdf") {
    showUploadStatus("Please choose a PDF file.", true);
    return;
  }
  if (file.size > MAX_UPLOAD_MB * 1024 * 1024) {
    showUploadStatus(`This file is over the ${MAX_UPLOAD_MB} MB limit.`, true);
    return;
  }

  dropzoneFilename.textContent = file.name;
  showUploadStatus("", false);
  uploadProgress.hidden = false;
  uploadProgressBar.style.width = "0%";

  try {
    const chatSessionId = await ensureChatSession();

    const uploadId = newUploadId();
    const formData = new FormData();
    formData.append("file", file);
    formData.append("chat_session_id", chatSessionId);
    formData.append("upload_id", uploadId);

    const data = await uploadWithProgress(formData, uploadId, chatSessionId);

    uploadProgress.hidden = true;
    state.documentReady = true;
    dropzoneText.innerHTML = 'Tap to add another PDF<br><span class="dropzone-subtext">to this chat</span>';
    showUploadStatus(`"${file.name}" is indexed and ready.`, false);
    enableChat();

    currentDocumentFilename = file.name;
    const currentDocs = Array.from(documentsList.children)
      .filter((li) => !li.classList.contains("documents-empty"))
      .map((li) => li.textContent);
    renderDocumentsList([...currentDocs, file.name]);

    await loadChatList(); // picks up the just-created "New chat" entry
  } catch (err) {
    console.error(err);
    uploadProgress.hidden = true;
    dropzoneText.innerHTML = 'Drop your PDF here<br><span class="dropzone-subtext">or click to browse</span>';
    showUploadStatus(err.message || "Couldn't process document. Try uploading again.", true);
  }
}

function newUploadId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  // Fallback for older browsers: RFC 4122 version-4 shape from random bytes.
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function formatElapsed(totalSeconds) {
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const m = Math.floor(totalSeconds / 60);
  const s = String(totalSeconds % 60).padStart(2, "0");
  return `${m}m ${s}s`;
}

const PROGRESS_POLL_MS = 1500;

function uploadWithProgress(formData, uploadId, chatSessionId) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", API_BASE + CONFIG.UPLOAD_ENDPOINT);
    xhr.withCredentials = true;

    let processingStartedAt = null;
    let currentLabel = "Processing document";
    let currentDetail = null;
    let pollTimer = null;
    let tickTimer = null;

    const stopTimers = () => {
      clearInterval(pollTimer);
      clearInterval(tickTimer);
    };

    // Shows "Stage · detail · elapsed" under the dropzone. The elapsed timer
    // ticks every second on its own, so the user can see the upload is alive
    // even while the server is still inside one long stage.
    const renderProcessingStatus = () => {
      if (processingStartedAt === null) return;
      const elapsed = Math.floor((Date.now() - processingStartedAt) / 1000);
      const parts = [`${currentLabel}…`];
      if (currentDetail) parts.push(currentDetail);
      parts.push(formatElapsed(elapsed));
      showUploadStatus(parts.join(" · "), false);
    };

    const pollProgress = async () => {
      try {
        const url = `${API_BASE}${CONFIG.UPLOAD_ENDPOINT}/${uploadId}/progress`
          + `?chat_session_id=${encodeURIComponent(chatSessionId)}`;
        const res = await fetch(url, { credentials: "include" });
        if (!res.ok) return; // not started yet, or finished: keep the current text
        const info = await res.json();
        currentLabel = info.label || currentLabel;
        currentDetail = info.detail || null;
        renderProcessingStatus();
      } catch {
        // Progress is best-effort. The upload request itself decides the outcome.
      }
    };

    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) {
        const pct = Math.round((e.loaded / e.total) * 100);
        uploadProgressBar.style.width = pct + "%";
        dropzoneText.textContent = `Uploading… ${pct}%`;
      }
    });

    xhr.upload.addEventListener("load", () => {
      // The file has reached the server. Server processing starts now, so
      // switch to stage-based status instead of a bare spinner.
      dropzoneText.textContent = "Processing document…";
      uploadProgressBar.classList.add("is-indeterminate");
      processingStartedAt = Date.now();
      renderProcessingStatus();
      pollProgress();
      pollTimer = setInterval(pollProgress, PROGRESS_POLL_MS);
      tickTimer = setInterval(renderProcessingStatus, 1000);
    });

    xhr.onload = () => {
      stopTimers();
      let data;
      try { data = JSON.parse(xhr.responseText); } catch { data = {}; }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(data);
      } else {
        reject(new Error(data.detail || `Upload failed (${xhr.status})`));
      }
      uploadProgressBar.classList.remove("is-indeterminate");
    };
    xhr.onerror = () => {
      stopTimers();
      reject(new Error("Network error during upload."));
    };

    xhr.send(formData);
  });
}

function showUploadStatus(message, isError) {
  uploadStatus.textContent = message;
  uploadStatus.hidden = !message;
  uploadStatus.classList.toggle("is-error", isError);
}

function enableChat() {
  chatStatus.textContent = "✓ Ready";
  chatStatus.className = "status-pill is-ready";
  chatInput.disabled = false;
  chatSend.disabled = false;
}

/* ============================================
   CHAT
   ============================================ */
chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const message = chatInput.value.trim();
  if (!message || !state.documentReady) return;
  chatInput.value = "";
  await askQuestion(message);
});

async function askQuestion(message) {
  state.lastQuestion = message;
  appendMessage("user", message);
  const assistantEl = appendMessage("assistant", "", { streaming: true, statusText: "Searching your document…" });

  chatInput.disabled = true;
  chatSend.disabled = true;

  try {
    await streamAnswer(message, assistantEl);
  } catch (err) {
    console.error(err);
    setMessageText(assistantEl, "Something went wrong reaching the backend.");
  } finally {
    stopStreamingCursor(assistantEl);
    chatInput.disabled = false;
    chatSend.disabled = false;
    chatInput.focus();
  }
}

async function streamAnswer(message, assistantEl) {
  const res = await fetch(CONFIG.CHAT_ENDPOINT, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ chat_session_id: CHAT_SESSION_ID, question: message }),
    credentials: "include",
  });

  if (!res.ok || !res.body) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Chat request failed (${res.status})`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let answerText = "";
  let firstTokenReceived = false;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const parts = buffer.split("\n\n");
    buffer = parts.pop();

    for (const part of parts) {
      const { event, data } = parseSSEEvent(part);
      if (!event || data === null) continue;
      let payload;
      try { payload = JSON.parse(data); } catch { continue; }

      if (event === "token") {
        if (!firstTokenReceived) {
          firstTokenReceived = true;
          clearMessageStatus(assistantEl);
        }
        answerText += payload.content ?? "";
        setMessageText(assistantEl, answerText, { streaming: true });
      } else if (event === "sources") {
        finalizeAssistantMessage(assistantEl, payload.sources ?? [], payload.confidence);
      } else if (event === "error") {
        clearMessageStatus(assistantEl);
        setMessageText(assistantEl, payload.message || "The assistant couldn't answer that.");
      }
    }
  }

  await loadChatList(); // title may have just been auto-set from this question
  if (currentChatTitle.textContent === "New chat") {
    // Refresh the topbar title too, once the backend has renamed it.
    const res2 = await fetch(API_BASE + `/chats/${CHAT_SESSION_ID}`, { headers: authHeaders(), credentials: "include" });
    if (res2.ok) {
      const data2 = await res2.json();
      currentChatTitle.textContent = data2.title || "New chat";
    }
  }
}

function parseSSEEvent(rawBlock) {
  let event = null;
  let data = null;
  rawBlock.split("\n").forEach((line) => {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) data = line.slice(5).trim();
  });
  return { event, data };
}

/* ============================================
   MESSAGE RENDERING + CONTROLS
   ============================================ */
function appendMessage(role, text, opts = {}) {
  chatEmpty.hidden = true;

  const createdAt = opts.createdAt || new Date().toISOString();
  const dateKey = new Date(createdAt).toDateString();

  if (dateKey !== lastRenderedDateKey) {
    const sep = document.createElement("div");
    sep.className = "date-separator";
    sep.textContent = formatDateSeparator(createdAt);
    chatLog.appendChild(sep);
    lastRenderedDateKey = dateKey;
  }

  const el = document.createElement("div");
  el.className = `chat-message ${role}`;

  const textEl = document.createElement("div");
  textEl.className = "msg-text";
  if (opts.statusText) {
    textEl.innerHTML = `<span class="msg-status">${escapeHtml(opts.statusText)}</span>`;
  } else {
    textEl.innerHTML = escapeHtml(text) + (opts.streaming ? '<span class="cursor"></span>' : "");
  }
  el.appendChild(textEl);

  const timeEl = document.createElement("div");
  timeEl.className = "msg-time";
  timeEl.textContent = formatLocalTime(createdAt);
  el.appendChild(timeEl);

  chatLog.appendChild(el);
  chatLog.scrollTop = chatLog.scrollHeight;
  return el;
}

function formatDateSeparator(isoUtcString) {
  const date = new Date(isoUtcString);
  const now = new Date();
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);

  if (date.toDateString() === now.toDateString()) return "Today";
  if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
  return date.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
}

function clearMessageStatus(el) {
  const textEl = el.querySelector(".msg-text");
  textEl.innerHTML = '<span class="cursor"></span>';
}

function setMessageText(el, text, opts = {}) {
  const textEl = el.querySelector(".msg-text");
  textEl.innerHTML = escapeHtml(text) + (opts.streaming ? '<span class="cursor"></span>' : "");
  chatLog.scrollTop = chatLog.scrollHeight;
}

function stopStreamingCursor(el) {
  const cursor = el.querySelector(".cursor");
  if (cursor) cursor.remove();
}

function finalizeAssistantMessage(el, sources, confidence) {
  if (confidence) {
    const badge = document.createElement("span");
    badge.className = `confidence-badge is-${confidence}`;
    badge.textContent = confidence === "high" ? "High confidence" : "Low confidence — verify from source";
    el.querySelector(".msg-text").after(badge);
  }
  if (sources.length > 0) {
    const note = document.createElement("div");
    note.className = "retrieved-note";
    note.textContent = `Retrieved ${sources.length} relevant section${sources.length > 1 ? "s" : ""}`;
    el.appendChild(note);
    renderSourceChips(el, sources);
  }
  addAnswerControls(el);
}

function renderSourceChips(el, sources) {
  const wrap = document.createElement("div");
  wrap.className = "source-chips";
  sources.forEach((s) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "source-chip";
    chip.textContent = s.page ? `📄 ${s.source} · p.${s.page} → View` : s.source;
    chip.addEventListener("click", () => openDocumentPreview(s.source, s.page));
    wrap.appendChild(chip);
  });
  el.appendChild(wrap);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function addAnswerControls(el) {
  const answerText = el.querySelector(".msg-text").textContent;
  const bar = document.createElement("div");
  bar.className = "answer-controls";
  bar.innerHTML = `
    <button type="button" class="answer-control-btn" data-action="copy">⧉ Copy</button>
    <button type="button" class="answer-control-btn" data-action="regenerate">↻ Regenerate</button>
    <button type="button" class="answer-control-btn" data-action="simplify">Simplify</button>
    <button type="button" class="answer-control-btn" data-action="summarize">Summarize</button>
  `;

  bar.querySelector('[data-action="copy"]').addEventListener("click", () => {
    navigator.clipboard.writeText(answerText);
  });
  bar.querySelector('[data-action="regenerate"]').addEventListener("click", () => {
    if (state.lastQuestion) askQuestion(state.lastQuestion);
  });
  bar.querySelector('[data-action="simplify"]').addEventListener("click", () => {
    if (state.lastQuestion) askQuestion(`${state.lastQuestion} — explain it in simpler terms`);
  });
  bar.querySelector('[data-action="summarize"]').addEventListener("click", () => {
    if (state.lastQuestion) askQuestion(`Summarize the answer to: ${state.lastQuestion}`);
  });

  el.appendChild(bar);
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

init();