/* Browser client for the Seamless share HTTP/WebSocket protocol. */

function connect_seamless(update_server = null, rest_server = null, share_namespace = "ctx") {
  const page = (typeof window !== "undefined" && window.location)
    ? window.location
    : { origin: "http://localhost", protocol: "http:", hostname: "localhost" };

  function asHttpBase(value) {
    if (value === null || value === undefined || value === "") {
      return String(page.origin || `${page.protocol}//${page.host || page.hostname}`).replace(/\/$/, "");
    }
    if (typeof value === "number" || /^\d+$/.test(String(value))) {
      const protocol = page.protocol === "https:" ? "https:" : "http:";
      const hostname = page.hostname || new URL(page.origin).hostname;
      return `${protocol}//${hostname}:${Number(value)}`;
    }
    let url = new URL(String(value), page.origin || "http://localhost");
    if (url.protocol === "ws:") url.protocol = "http:";
    if (url.protocol === "wss:") url.protocol = "https:";
    url.hash = "";
    url.search = "";
    return url.href.replace(/\/$/, "");
  }

  function asWebSocketBase(httpBase) {
    const url = new URL(httpBase);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    return url.href.replace(/\/$/, "");
  }

  let restBase;
  let socketBase;
  if (update_server != null && rest_server != null) {
    const updateBase = asHttpBase(update_server);
    const restCandidate = asHttpBase(rest_server);
    if (updateBase !== restCandidate && typeof console !== "undefined" && console.warn) {
      console.warn("connect_seamless: update and REST servers differ; using rest_server for both");
    }
    restBase = restCandidate;
    socketBase = asWebSocketBase(restCandidate);
  } else if (update_server != null) {
    restBase = asHttpBase(update_server);
    socketBase = asWebSocketBase(restBase);
  } else if (rest_server != null) {
    restBase = asHttpBase(rest_server);
    socketBase = asWebSocketBase(restBase);
  } else {
    restBase = asHttpBase(null);
    socketBase = asWebSocketBase(restBase);
  }

  const namespace = String(share_namespace || "ctx").replace(/^\/+|\/+$/g, "");
  const ctx = { self: {} };
  const entries = new Map();
  let reconnectAttempt = 0;
  let reconnectTimer = null;
  let disposed = false;
  let adoptRestartSnapshot = false;
  let socketGeneration = 0;

  const self = ctx.self;
  self.sharelist = [];
  self.onsharelist = function () {};
  self.oninput = function () {};
  self.onchange = function () {};
  self.share_namespace = namespace;
  self.server = restBase;
  self.rest_server = restBase;
  self.update_server = socketBase;
  self.ws = null;
  self.get_value = function () {
    const result = {};
    for (const key of self.sharelist) {
      const entry = entries.get(key);
      if (entry) result[propertyName(key)] = entry.value;
    }
    return result;
  };

  function propertyName(key) {
    return String(key).replaceAll("/", "__");
  }

  function isCurrentEntry(entry) {
    return entries.get(entry.key) === entry;
  }

  function endpoint(entry) {
    return new URL(entry.url, `${restBase}/`).href;
  }

  function callHandlers(entry, eventName) {
    try { entry[eventName](entry.value); } catch (error) { console.error("Seamless share handler error:", error); }
    try { self[eventName](entry.value); } catch (error) { console.error("Seamless share handler error:", error); }
  }

  function makeEntry(key, metadata) {
    const entry = {
      key,
      url: metadata.url,
      value: undefined,
      checksum: metadata.checksum ?? null,
      marker: Number(metadata.marker ?? 0),
      serverRevision: 0,
      binary: Boolean(metadata.binary),
      content_type: metadata.content_type ?? "application/octet-stream",
      readonly: Boolean(metadata.readonly),
      auto_read: !String(key).includes("."),
      oninput: function () {},
      onchange: function () {},
      readGeneration: 0,
      localVersion: 0,
      pending: null,
      _hasPending: false,
      writing: false,
      set(value) { return setValue(entry, value); }
    };
    return entry;
  }

  function installSnapshot(shareMap) {
    const restartSnapshot = adoptRestartSnapshot;
    const listed = Object.keys(shareMap || {});
    const keep = new Set(listed);
    for (const [key] of entries) {
      if (!keep.has(key)) {
        entries.delete(key);
        delete ctx[propertyName(key)];
      }
    }

    for (const key of listed) {
      const metadata = shareMap[key] || {};
      let entry = entries.get(key);
      const created = !entry;
      if (!entry) {
        entry = makeEntry(key, metadata);
        entries.set(key, entry);
        Object.defineProperty(ctx, propertyName(key), {
          configurable: true,
          enumerable: true,
          get: () => entry
        });
      }
      const oldMarker = entry.marker;
      const oldChecksum = entry.checksum;
      const oldUrl = entry.url;
      const nextMarker = Number(metadata.marker ?? entry.marker ?? 0);
      const nextChecksum = metadata.checksum ?? null;
      entry.url = metadata.url ?? entry.url;
      entry.binary = Boolean(metadata.binary);
      entry.content_type = metadata.content_type ?? entry.content_type;
      entry.readonly = Boolean(metadata.readonly);
      if (adoptRestartSnapshot || nextMarker >= entry.marker) {
        entry.marker = nextMarker;
        entry.checksum = nextChecksum;
      }
      const changed = created || entry.marker !== oldMarker || entry.checksum !== oldChecksum || entry.url !== oldUrl || restartSnapshot;
      if (!created && changed) entry.serverRevision += 1;
      if (entry.auto_read && (changed || entry.value === undefined)) fetchValue(entry);
    }
    self.sharelist = listed;
    adoptRestartSnapshot = false;
    try { self.onsharelist(self.sharelist); } catch (error) { console.error("Seamless share handler error:", error); }
  }

  function updateMetadata(entry, checksum, marker) {
    const numericMarker = Number(marker);
    if (!Number.isFinite(numericMarker) || numericMarker < entry.marker) return false;
    const changed = numericMarker !== entry.marker || checksum !== entry.checksum;
    entry.marker = numericMarker;
    if (checksum !== undefined) entry.checksum = checksum;
    if (changed) entry.serverRevision += 1;
    return changed;
  }

  async function fetchValue(entry, { force = false } = {}) {
    if ((!entry.auto_read && !force) || !isCurrentEntry(entry)) return;
    const generation = ++entry.readGeneration;
    const requestedMarker = entry.marker;
    try {
      const response = await fetch(endpoint(entry), { method: "GET", cache: "no-store" });
      let newValue;
      if (response.status === 204) {
        newValue = null;
      } else if (response.status === 404) {
        let result = {};
        try { result = await response.json(); } catch (_) {}
        if (!result || result.error !== "share has no value") return;
        newValue = null;
      } else if (!response.ok) {
        return;
      } else if (entry.binary || (response.headers.get("content-type") || "").toLowerCase().startsWith("application/octet-stream")) {
        newValue = await response.blob();
      } else {
        newValue = await response.text();
      }
      if (generation !== entry.readGeneration || !isCurrentEntry(entry)) return;

      const markerHeader = response.headers.get("x-seamless-marker");
      const responseMarker = markerHeader === null ? NaN : Number(markerHeader);
      const responseChecksum = (response.headers.get("etag") || "").replace(/^W\//, "").replace(/^"|"$/g, "");
      if (Number.isFinite(responseMarker) &&
          (responseMarker < requestedMarker || responseMarker < entry.marker)) return;
      if (Number.isFinite(responseMarker) && responseMarker >= requestedMarker && responseMarker >= entry.marker) {
        entry.marker = responseMarker;
        if (responseChecksum) entry.checksum = responseChecksum;
      }
      entry.value = newValue;
      const contentType = response.headers.get("content-type");
      if (contentType) entry.content_type = contentType;
      callHandlers(entry, "oninput");
      callHandlers(entry, "onchange");
    } catch (error) {
      console.warn("Seamless share GET failed:", entry.key, error);
    }
  }

  function setValue(entry, value) {
    if (!isCurrentEntry(entry)) return false;
    if (entry.readonly) {
      console.warn(`Cannot set read-only share ${entry.key}`);
      return false;
    }
    if (entry.binary && value !== null && !(value instanceof Blob)) {
      console.warn(`Cannot set binary share ${entry.key} to a non-Blob value`);
      return false;
    }
    if (!entry.binary && value instanceof Blob) {
      value.text().then(text => setValue(entry, text));
      return true;
    }
    // Keep input handlers compatible with the legacy client: the new local
    // value is visible before oninput runs. A later acknowledgement may only
    // change it if this is still the newest local edit.
    entry.readGeneration += 1;
    entry.localVersion += 1;
    const revision = entry.localVersion;
    entry.value = value;
    callHandlers(entry, "oninput");
    entry.pending = { value, revision };
    entry._hasPending = true;
    if (!entry.writing) drainWrites(entry);
    return true;
  }

  async function drainWrites(entry) {
    if (entry.writing || !isCurrentEntry(entry)) return;
    entry.writing = true;
    try {
      while (entry._hasPending && isCurrentEntry(entry)) {
        const operation = entry.pending;
        const value = operation.value;
        entry.pending = null;
        entry._hasPending = false;
        const requestMarker = entry.marker;
        const requestServerRevision = entry.serverRevision;
        let response;
        try {
          const headers = {};
          if (entry.content_type) headers["Content-Type"] = entry.content_type;
          const body = value === null ? "" : value;
          const url = new URL(endpoint(entry));
          url.searchParams.set("marker", String(requestMarker));
          response = await fetch(url.href, { method: "PUT", headers, body });
        } catch (error) {
          console.warn("Seamless share PUT failed:", entry.key, error);
          continue;
        }
        if (!isCurrentEntry(entry)) break;

        let result = {};
        try { result = await response.json(); } catch (_) {}
        if (response.status === 409) {
          entry.pending = null;
          entry._hasPending = false;
          if (requestServerRevision === entry.serverRevision && result &&
              Number.isFinite(Number(result.marker)) && Number(result.marker) >= entry.marker) {
            entry.marker = Number(result.marker);
            if (result.checksum) entry.checksum = result.checksum;
          }
          await fetchValue(entry, { force: true });
          break;
        }
        if (!response.ok) {
          console.warn("Seamless share PUT rejected:", entry.key, response.status);
          continue;
        }
        const responseMarker = result && result.marker !== undefined ? Number(result.marker) : NaN;
        const responseIsCurrent = requestServerRevision === entry.serverRevision &&
          (!Number.isFinite(responseMarker) || responseMarker >= entry.marker);
        if (responseIsCurrent) {
          if (Number.isFinite(responseMarker)) entry.marker = responseMarker;
          if (result && result.checksum) entry.checksum = result.checksum;
          if (entry.localVersion === operation.revision) {
            entry.value = value;
            callHandlers(entry, "onchange");
          }
        }
      }
    } finally {
      entry.writing = false;
      if (entry._hasPending && isCurrentEntry(entry)) drainWrites(entry);
    }
  }

  function receiveMessage(event) {
    let message;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    if (!Array.isArray(message)) return;
    if (message[0] === "Seamless share update server") return;
    if (message[0] === "shares") {
      installSnapshot(message[1]);
      return;
    }
    if (message[0] === "update" && Array.isArray(message[1])) {
      const [key, checksum, marker] = message[1];
      const entry = entries.get(key);
      if (!entry) return;
      const changed = updateMetadata(entry, checksum, marker);
      if (changed && entry.auto_read) fetchValue(entry);
    }
  }

  function connect() {
    if (disposed) return null;
    if (reconnectTimer !== null) clearTimeout(reconnectTimer);
    reconnectTimer = null;
    const previous = self.ws;
    const generation = ++socketGeneration;
    self.ws = null;
    if (previous) adoptRestartSnapshot = true;
    if (previous && typeof previous.close === "function") previous.close();
    const url = `${socketBase}/${namespace.split("/").map(encodeURIComponent).join("/")}`;
    const Socket = (typeof window !== "undefined" && window.WebSocket) || globalThis.WebSocket;
    if (!Socket) throw new Error("WebSocket is unavailable in this environment");
    const socket = new Socket(url);
    self.ws = socket;
    const isCurrentSocket = () => socketGeneration === generation && self.ws === socket;
    const add = (name, callback) => {
      if (typeof socket.addEventListener === "function") socket.addEventListener(name, callback);
      else socket[`on${name}`] = callback;
    };
    add("message", event => {
      if (isCurrentSocket()) receiveMessage(event);
    });
    add("open", () => {});
    add("close", () => {
      if (!isCurrentSocket() || disposed || reconnectTimer !== null) return;
      self.ws = null;
      adoptRestartSnapshot = true;
      const delay = Math.min(1000 * (2 ** reconnectAttempt), 30000);
      reconnectAttempt += 1;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    });
    add("error", () => {
      // Browsers follow an error with close; reconnect scheduling stays centralized there.
    });
    return socket;
  }

  self.connect = connect;
  self.close = function () {
    disposed = true;
    if (reconnectTimer !== null) clearTimeout(reconnectTimer);
    reconnectTimer = null;
    ++socketGeneration;
    const socket = self.ws;
    self.ws = null;
    if (socket && typeof socket.close === "function") socket.close();
  };
  connect();
  return ctx;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { connect_seamless };
}
