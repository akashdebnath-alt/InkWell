const API = "http://localhost:8000";
const TOKEN_KEY = "inkwell.access_token";

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const isFormData = typeof FormData !== "undefined" && options.body instanceof FormData;
  if (options.body && !isFormData && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let response;
  try {
    response = await fetch(`${API}${path}`, { ...options, headers });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError("Can't connect to Inkwell. Check that the backend is running and try again.", 0);
  }
  if (response.status === 401 && !path.startsWith("/auth/")) {
    clearToken();
    window.dispatchEvent(new Event("inkwell:unauthorized"));
    throw new ApiError("Your session has expired. Please log in again.", 401);
  }

  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // Keep the HTTP status message when the server does not return JSON.
    }
    throw new ApiError(message, response.status);
  }
  return response;
}

export async function api(path, options = {}) {
  const response = await request(path, options);
  if (response.status === 204) return null;
  try {
    return await response.json();
  } catch {
    throw new ApiError("The server returned an unreadable response. Please try again.", response.status);
  }
}

export async function apiDownload(path, options = {}) {
  return request(path, options);
}

export async function apiStream(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "text/event-stream");
  const response = await request(path, { ...options, headers });
  if (!response.body) throw new ApiError("The server returned an empty stream.", response.status);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let text = "";
  let finished = false;

  function consumeFrame(frame) {
    for (const line of frame.split(/\r?\n/)) {
      if (!line.startsWith("data:")) continue;
      const data = line.slice(5).trim();
      if (data === "[DONE]") {
        finished = true;
        return;
      }
      const event = JSON.parse(data);
      if (event.error) throw new ApiError(event.error, response.status);
      if (typeof event.text === "string") text += event.text;
    }
  }

  try {
    while (!finished) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() || "";
      for (const frame of frames) consumeFrame(frame);
      if (done) {
        if (buffer.trim()) consumeFrame(buffer);
        break;
      }
    }
  } catch (error) {
    if (error.name === "AbortError" || error instanceof ApiError) throw error;
    throw new ApiError("The connection was interrupted while receiving the suggestion.", 0);
  }
  return text;
}
