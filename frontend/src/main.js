import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import TextAlign from "@tiptap/extension-text-align";
import Underline from "@tiptap/extension-underline";
import { api, apiDownload, clearToken, getToken, setToken } from "./api.js";
import { GhostText } from "./ghost.js";

const $ = (selector) => document.querySelector(selector);
const authScreen = $("#auth-screen");
const documentsScreen = $("#documents-screen");
const editorScreen = $("#editor-screen");
const errorMessage = $("#error-message");
const THEME_KEY = "inkwell.theme";

function applyTheme(theme) {
  const isDark = theme === "dark";
  window.document.documentElement.dataset.theme = isDark ? "dark" : "light";
  $("#toggle-dark-mode").textContent = isDark ? "Light mode" : "Dark mode";
  $("#toggle-dark-mode").setAttribute("aria-pressed", String(isDark));
}

applyTheme(localStorage.getItem(THEME_KEY) || "light");

let currentDocument = null;
let saveTimer = null;
let saveInFlight = null;
let loadingDocument = false;
let documentDirty = false;
let authMode = "login";
let assistantRequestId = 0;
let pendingSelection = null;

const editor = new Editor({
  element: $("#editor"),
  extensions: [
    StarterKit,
    Underline,
    TextAlign.configure({ types: ["heading", "paragraph"] }),
    GhostText.configure({ getDocId: () => currentDocument?.id }),
  ],
  content: "",
  editable: false,
  editorProps: {
    handleKeyDown(_view, event) {
      if (!(event.ctrlKey || event.metaKey) || event.altKey) return false;
      const key = event.key.toLowerCase();
      const chain = editor.chain().focus();
      if (key === "b") return chain.toggleBold().run();
      if (key === "i") return chain.toggleItalic().run();
      if (key === "u") return chain.toggleUnderline().run();
      if (key === "z" && event.shiftKey) return chain.redo().run();
      if (key === "z") return chain.undo().run();
      if (key === "y") return chain.redo().run();
      return false;
    },
  },
});

function showError(message = "") {
  errorMessage.textContent = message;
  errorMessage.hidden = !message;
}

function showScreen(screen) {
  authScreen.hidden = screen !== "auth";
  documentsScreen.hidden = screen !== "documents";
  editorScreen.hidden = screen !== "editor";
}

function setSaveStatus(text, state = "") {
  const status = $("#save-status");
  status.textContent = text;
  status.dataset.state = state;
}

function updateCounts() {
  const text = editor.getText({ blockSeparator: " " });
  const words = text.trim() ? text.trim().split(/\s+/u).length : 0;
  $("#word-count").textContent = `${words.toLocaleString()} ${words === 1 ? "word" : "words"}`;
  $("#character-count").textContent =
    `${text.length.toLocaleString()} ${text.length === 1 ? "character" : "characters"}`;
}

function getEditorContent(content) {
  if (!content) return "";
  try {
    const parsed = JSON.parse(content);
    if (parsed && typeof parsed === "object" && parsed.type === "doc") return parsed;
  } catch {
    // Older documents may contain HTML instead of serialized TipTap JSON.
  }
  return content;
}

function setAuthMode(mode) {
  authMode = mode;
  $("#auth-title").textContent = mode === "login" ? "Welcome back" : "Create your account";
  $("#auth-submit").textContent = mode === "login" ? "Log in" : "Register";
  $("#auth-switch").textContent =
    mode === "login" ? "Need an account? Register" : "Already have an account? Log in";
  $("#auth-password").autocomplete = mode === "login" ? "current-password" : "new-password";
  showError("");
}

function documentRow(document) {
  const row = window.document.createElement("li");
  row.className = "document-row";

  const openButton = window.document.createElement("button");
  openButton.className = "document-open";
  openButton.textContent = document.title;
  openButton.addEventListener("click", async () => {
    openButton.disabled = true;
    openButton.textContent = "Loading...";
    try {
      await openDocument(document.id);
    } finally {
      openButton.disabled = false;
      openButton.textContent = document.title;
    }
  });

  const date = window.document.createElement("span");
  date.className = "document-date";
  date.textContent = document.updated_at
    ? `Edited ${new Date(document.updated_at).toLocaleString()}`
    : "";

  const renameButton = window.document.createElement("button");
  renameButton.textContent = "Rename";
  renameButton.addEventListener("click", async () => {
    const title = window.prompt("New document title", document.title)?.trim();
    if (!title || title === document.title) return;
    renameButton.disabled = true;
    renameButton.textContent = "Renaming...";
    try {
      await api(`/documents/${document.id}`, {
        method: "PUT",
        body: JSON.stringify({ title }),
      });
      await loadDocuments();
    } catch (error) {
      showError(error.message);
    } finally {
      renameButton.disabled = false;
      renameButton.textContent = "Rename";
    }
  });

  const deleteButton = window.document.createElement("button");
  deleteButton.textContent = "Delete";
  deleteButton.className = "danger-button";
  deleteButton.addEventListener("click", async () => {
    if (!window.confirm(`Delete "${document.title}"? This cannot be undone.`)) return;
    deleteButton.disabled = true;
    deleteButton.textContent = "Deleting...";
    try {
      await api(`/documents/${document.id}`, { method: "DELETE" });
      if (currentDocument?.id === document.id) currentDocument = null;
      await loadDocuments();
    } catch (error) {
      showError(error.message);
    } finally {
      deleteButton.disabled = false;
      deleteButton.textContent = "Delete";
    }
  });

  row.append(openButton, date, renameButton, deleteButton);
  return row;
}

async function loadDocuments() {
  showScreen("documents");
  showError("");
  const list = $("#documents-list");
  list.replaceChildren();
  $("#documents-loading").hidden = false;
  try {
    const documents = await api("/documents");
    if (documents.length === 0) {
      $("#documents-empty").hidden = false;
    } else {
      $("#documents-empty").hidden = true;
      for (const document of documents) list.append(documentRow(document));
    }
  } catch (error) {
    showError(error.message);
  } finally {
    $("#documents-loading").hidden = true;
  }
}

async function loadReferences(documentId) {
  const list = $("#reference-list");
  list.replaceChildren();
  $("#references-empty").hidden = true;
  $("#references-status").textContent = "Loading references...";
  try {
    const references = await api(`/documents/${documentId}/references`);
    if (currentDocument?.id !== documentId) return;
    $("#references-status").textContent = "";
    if (references.length === 0) {
      $("#references-empty").hidden = false;
      return;
    }
    $("#references-empty").hidden = true;
    for (const reference of references) {
      const item = window.document.createElement("li");
      const title = window.document.createElement("span");
      title.textContent = reference.title;
      const remove = window.document.createElement("button");
      remove.type = "button";
      remove.className = "danger-button";
      remove.textContent = "Remove";
      remove.addEventListener("click", async () => {
        remove.disabled = true;
        remove.textContent = "Removing...";
        try {
          await api(`/references/${reference.id}`, { method: "DELETE" });
          await loadReferences(documentId);
        } catch (error) {
          showError(error.message);
        } finally {
          remove.disabled = false;
          remove.textContent = "Remove";
        }
      });
      item.append(title, remove);
      list.append(item);
    }
  } catch (error) {
    $("#references-status").textContent = "Could not load references.";
    showError(error.message);
  }
}

async function loadVersions(documentId) {
  const list = $("#version-list");
  list.replaceChildren();
  $("#versions-status").textContent = "Loading versions...";
  $("#versions-empty").hidden = true;
  try {
    const versions = await api(`/documents/${documentId}/versions`);
    if (currentDocument?.id !== documentId) return;
    $("#versions-status").textContent = "";
    $("#versions-empty").hidden = versions.length > 0;
    for (const version of versions) {
      const item = window.document.createElement("li");
      const restoreButton = window.document.createElement("button");
      restoreButton.type = "button";
      restoreButton.textContent = new Date(version.created_at).toLocaleString();
      restoreButton.addEventListener("click", async () => {
        if (!window.confirm("Restore this version? Your current text will be saved in version history.")) return;
        if (documentDirty) {
          clearTimeout(saveTimer);
          saveTimer = null;
          if (!(await saveDocument())) return;
        }
        restoreButton.disabled = true;
        restoreButton.textContent = "Restoring...";
        try {
          const restored = await api(`/documents/${documentId}/restore/${version.id}`, {
            method: "POST",
          });
          if (currentDocument?.id !== documentId) return;
          loadingDocument = true;
          editor.commands.setContent(getEditorContent(restored.content), false);
          loadingDocument = false;
          documentDirty = false;
          updateCounts();
          setSaveStatus("Saved", "saved");
          showError("");
          await loadVersions(documentId);
        } catch (error) {
          loadingDocument = false;
          showError(error.message);
        } finally {
          restoreButton.disabled = false;
          restoreButton.textContent = new Date(version.created_at).toLocaleString();
        }
      });
      item.append(restoreButton);
      list.append(item);
    }
  } catch (error) {
    $("#versions-status").textContent = "Could not load version history.";
    showError(error.message);
  }
}

async function openDocument(id) {
  clearTimeout(saveTimer);
  assistantRequestId += 1;
  showError("");
  setSaveStatus("Loading...");
  try {
    const document = await api(`/documents/${id}`);
    loadingDocument = true;
    $("#document-title").textContent = document.title;
    editor.commands.setContent(getEditorContent(document.content), false);
    currentDocument = { id: document.id, title: document.title };
    documentDirty = false;
    editor.setEditable(true);
    loadingDocument = false;
    pendingSelection = null;
    updateCounts();
    setSaveStatus("Saved", "saved");
    showScreen("editor");
    editor.commands.focus();
    await loadReferences(document.id);
  } catch (error) {
    loadingDocument = false;
    if (error.status === 401) return;
    await loadDocuments();
    showError(error.message);
  }
}

async function createDocument() {
  showError("");
  const button = $("#create-document");
  button.disabled = true;
  button.textContent = "Creating...";
  try {
    const document = await api("/documents", {
      method: "POST",
      body: JSON.stringify({ title: "Untitled", content: "" }),
    });
    await openDocument(document.id);
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "New document";
  }
}

function leaveEditor() {
  clearTimeout(saveTimer);
  assistantRequestId += 1;
  currentDocument = null;
  pendingSelection = null;
  $("#reference-list").replaceChildren();
  $("#version-list").replaceChildren();
  $("#references-status").textContent = "";
  $("#versions-status").textContent = "";
  $("#version-panel").hidden = true;
  $("#toggle-versions").setAttribute("aria-expanded", "false");
  editor.setEditable(false);
  setSaveStatus("");
}

async function saveDocument() {
  if (!currentDocument) return true;
  if (saveInFlight) return saveInFlight;
  if (!documentDirty) return true;
  setSaveStatus("Saving...", "saving");
  saveInFlight = (async () => {
    const documentId = currentDocument.id;
    while (currentDocument?.id === documentId && documentDirty) {
      const content = JSON.stringify(editor.getJSON());
      const text = editor.getText();
      try {
        await api(`/documents/${documentId}`, {
          method: "PUT",
          body: JSON.stringify({ content }),
        });
      } catch (error) {
        if (currentDocument?.id === documentId) {
          setSaveStatus("Save failed", "error");
          showError(error.message);
        }
        return false;
      }
      if (currentDocument?.id !== documentId) return false;
      if (JSON.stringify(editor.getJSON()) === content) {
        documentDirty = false;
        setSaveStatus("Saved", "saved");
      }
      try {
        await api("/index", {
          method: "POST",
          body: JSON.stringify({ doc_id: documentId, text }),
        });
      } catch (error) {
        if (currentDocument?.id === documentId) {
          setSaveStatus("Saved - indexing failed", "error");
          showError(`Document saved, but indexing failed: ${error.message}`);
        }
      }
    }
    return true;
  })();
  try {
    return await saveInFlight;
  } finally {
    saveInFlight = null;
  }
}

editor.on("update", () => {
  updateCounts();
  if (loadingDocument || !currentDocument) return;
  documentDirty = true;
  setSaveStatus("Saving...", "saving");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    saveTimer = null;
    saveDocument();
  }, 1000);
});

const commands = {
  bold: () => editor.chain().focus().toggleBold().run(),
  italic: () => editor.chain().focus().toggleItalic().run(),
  underline: () => editor.chain().focus().toggleUnderline().run(),
  h1: () => editor.chain().focus().toggleHeading({ level: 1 }).run(),
  h2: () => editor.chain().focus().toggleHeading({ level: 2 }).run(),
  h3: () => editor.chain().focus().toggleHeading({ level: 3 }).run(),
  bullet: () => editor.chain().focus().toggleBulletList().run(),
  ordered: () => editor.chain().focus().toggleOrderedList().run(),
  "align-left": () => editor.chain().focus().setTextAlign("left").run(),
  "align-center": () => editor.chain().focus().setTextAlign("center").run(),
  "align-right": () => editor.chain().focus().setTextAlign("right").run(),
  "align-justify": () => editor.chain().focus().setTextAlign("justify").run(),
  undo: () => editor.chain().focus().undo().run(),
  redo: () => editor.chain().focus().redo().run(),
};

async function exportDocument(format, button) {
  if (!currentDocument) return;
  if (documentDirty) {
    clearTimeout(saveTimer);
    saveTimer = null;
    if (!(await saveDocument())) return;
  }
  const documentId = currentDocument.id;
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Exporting...";
  showError("");
  try {
    const response = await apiDownload(
      `/documents/${documentId}/export?format=${encodeURIComponent(format)}`,
    );
    const blob = await response.blob();
    const safeTitle = currentDocument?.id === documentId
      ? currentDocument.title
      : "document";
    let filename =
      safeTitle.replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_").replace(/[ .]+$/g, "").slice(0, 120) ||
      "document";
    if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(filename)) {
      filename = `_${filename}`;
    }
    const url = URL.createObjectURL(blob);
    const link = window.document.createElement("a");
    link.href = url;
    link.download = `${filename}.${format}`;
    window.document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

$("#toolbar").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-cmd]");
  if (button && commands[button.dataset.cmd]) commands[button.dataset.cmd]();
});

$("#export-docx").addEventListener("click", (event) =>
  exportDocument("docx", event.currentTarget),
);
$("#export-pdf").addEventListener("click", (event) =>
  exportDocument("pdf", event.currentTarget),
);
$("#toggle-dark-mode").addEventListener("click", () => {
  const theme = window.document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem(THEME_KEY, theme);
  applyTheme(theme);
});
$("#toggle-versions").addEventListener("click", async () => {
  if (!currentDocument) return;
  const panel = $("#version-panel");
  panel.hidden = !panel.hidden;
  $("#toggle-versions").setAttribute("aria-expanded", String(!panel.hidden));
  if (!panel.hidden) await loadVersions(currentDocument.id);
});
window.addEventListener(
  "keydown",
  (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      if (!editorScreen.hidden) {
        clearTimeout(saveTimer);
        saveTimer = null;
        void saveDocument();
      }
    }
  },
  { capture: true },
);

$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  showError("");
  const submitButton = $("#auth-submit");
  const originalLabel = submitButton.textContent;
  submitButton.disabled = true;
  submitButton.textContent = authMode === "login" ? "Signing in..." : "Creating account...";
  try {
    const result = await api(`/auth/${authMode === "login" ? "login" : "register"}`, {
      method: "POST",
      body: JSON.stringify({
        email: $("#auth-email").value,
        password: $("#auth-password").value,
      }),
    });
    setToken(result.access_token);
    $("#auth-form").reset();
    await loadDocuments();
  } catch (error) {
    showError(error.message);
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = originalLabel;
  }
});

$("#auth-switch").addEventListener("click", () => {
  setAuthMode(authMode === "login" ? "register" : "login");
});
$("#create-document").addEventListener("click", createDocument);
$("#back-to-documents").addEventListener("click", async () => {
  if (documentDirty) {
    editor.setEditable(false);
    clearTimeout(saveTimer);
    saveTimer = null;
    const saved = await saveDocument();
    if (!saved) {
      if (currentDocument) editor.setEditable(true);
      return;
    }
  }
  leaveEditor();
  await loadDocuments();
});
$("#logout").addEventListener("click", () => {
  clearToken();
  leaveEditor();
  setAuthMode("login");
  showScreen("auth");
});

function replacePendingSelection(replacement) {
  const target = pendingSelection;
  if (!target || currentDocument?.id !== target.documentId) return;
  const currentText = editor.state.doc.textBetween(target.from, target.to, " ");
  if (currentText !== target.text) {
    showError("The selected text changed. Select it again before applying this result.");
    return;
  }
  editor.chain().focus().insertContentAt(
    { from: target.from, to: target.to },
    replacement,
  ).run();
  pendingSelection = null;
}

async function askAssistant(mode) {
  if (!currentDocument) return;
  const { from, to } = editor.state.selection;
  const selection = editor.state.doc.textBetween(from, to, " ");
  if (!selection.trim()) {
    showError("Select text in your document first.");
    return;
  }
  const documentId = currentDocument.id;
  pendingSelection = { documentId, from, to, text: selection };
  const requestId = ++assistantRequestId;
  const output = $("#assistant-results");
  output.replaceChildren();
  output.textContent = "Thinking...";
  const modeButtons = [...$("#assistant-modes").querySelectorAll("button")];
  for (const button of modeButtons) button.disabled = true;
  showError("");
  try {
    const result = await api("/assist", {
      method: "POST",
      body: JSON.stringify({
        doc_id: documentId,
        mode,
        request: $("#assistant-request").value.trim(),
        selection,
      }),
    });
    if (requestId !== assistantRequestId || currentDocument?.id !== documentId) return;
    output.replaceChildren();
    if (result.options) {
      for (const option of result.options) {
        const button = window.document.createElement("button");
        button.type = "button";
        button.className = "result-option";
        button.textContent = option;
        button.addEventListener("click", () => replacePendingSelection(option));
        output.append(button);
      }
    } else if (result.feedback) {
      const list = window.document.createElement("ul");
      for (const point of result.feedback) {
        const item = window.document.createElement("li");
        item.textContent = point;
        list.append(item);
      }
      output.append(list);
    } else if (result.rewrite) {
      const button = window.document.createElement("button");
      button.type = "button";
      button.className = "result-option";
      button.textContent = result.rewrite;
      button.addEventListener("click", () => replacePendingSelection(result.rewrite));
      output.append(button);
    }
  } catch (error) {
    if (requestId === assistantRequestId) output.textContent = "";
    showError(error.message);
  } finally {
    for (const button of modeButtons) button.disabled = false;
  }
}

$("#assistant-modes").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-mode]");
  if (button) askAssistant(button.dataset.mode);
});

$("#reference-upload").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file || !currentDocument) return;
  const documentId = currentDocument.id;
  const form = new FormData();
  form.append("doc_id", String(documentId));
  form.append("file", file);
  const upload = event.currentTarget;
  upload.disabled = true;
  $("#references-status").textContent = "Uploading and indexing...";
  showError("");
  try {
    await api("/reference", { method: "POST", body: form });
    await loadReferences(documentId);
  } catch (error) {
    $("#references-status").textContent = "Upload failed.";
    showError(error.message);
  } finally {
    upload.disabled = false;
    upload.value = "";
  }
});

window.addEventListener("inkwell:unauthorized", () => {
  leaveEditor();
  setAuthMode("login");
  showError("Your session has expired. Please log in again.");
  showScreen("auth");
});

window.addEventListener("inkwell:api-error", (event) => {
  showError(event.detail?.message || "The writing suggestion could not be loaded.");
});

if (getToken()) {
  await loadDocuments();
} else {
  setAuthMode("login");
  showScreen("auth");
}
