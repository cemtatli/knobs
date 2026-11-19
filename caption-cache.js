// Both player features use the same copy of YouTube's caption response and its URL.
(() => {
  const MAX_TRACKS = 8;
  const MAX_BYTES = 4 * 1024 * 1024;
  const tracks = new Map();
  const requests = new WeakMap();
  const key = (videoId, lang, target) => `${videoId}|${lang}|${target ?? ""}`;
  const isJson = (body) => body?.trimStart().startsWith("{");
  let bytes = 0;
  let refused = null;

  function remember(url, body, status) {
    const parsed = new URL(url, location.origin);
    const params = parsed.searchParams;
    const videoId = params.get("v");
    const lang = params.get("lang");
    if (!videoId || !lang || params.get("fmt") !== "json3") return;
    const id = key(videoId, lang, params.get("tlang"));
    if (!isJson(body)) {
      if (status === 429 || body?.includes("<html")) refused = id;
      return;
    }
    if (refused === id) refused = null;
    // Allow for UTF-16 strings. Oversized responses are still available to the original caller.
    const size = body.length * 2;
    if (size > MAX_BYTES) return;
    forget(id);
    while (tracks.size >= MAX_TRACKS || bytes + size > MAX_BYTES) forget(tracks.keys().next().value);
    tracks.set(id, { body, size, url: parsed, videoId, lang });
    bytes += size;
  }

  function forget(id) {
    const entry = tracks.get(id);
    if (!entry) return;
    bytes -= entry.size;
    tracks.delete(id);
  }

  function get(id) {
    const entry = tracks.get(id);
    if (!entry) return;
    tracks.delete(id);
    tracks.set(id, entry);
    return entry.body;
  }

  function siblingUrl(videoId, lang, target) {
    const entry = Array.from(tracks.values()).findLast((track) => track.videoId === videoId && track.lang === lang);
    if (!entry) return null;
    const url = new URL(entry.url);
    if (target) url.searchParams.set("tlang", target);
    else url.searchParams.delete("tlang");
    return url.toString();
  }

  const xhrOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    const previous = requests.get(this);
    if (previous) this.removeEventListener("load", previous);
    const result = xhrOpen.call(this, method, url, ...rest);
    if (String(url).includes("/api/timedtext")) {
      const capture = () => {
        requests.delete(this);
        if (!this.responseType || this.responseType === "text") remember(String(url), this.responseText, this.status);
      };
      requests.set(this, capture);
      this.addEventListener("load", capture, { once: true });
    } else requests.delete(this);
    return result;
  };

  const pageFetch = window.fetch;
  window.fetch = async function (input, init) {
    const response = await pageFetch.call(this, input, init);
    const url = typeof input === "string" ? input : input?.url ?? input?.href;
    if (url?.includes("/api/timedtext")) {
      response.clone().text().then((body) => remember(url, body, response.status)).catch(() => {});
    }
    return response;
  };

  async function fetchText(url) {
    const response = await pageFetch.call(window, url, { credentials: "include" });
    const body = await response.text();
    remember(url, body, response.status);
    return body;
  }

  window.__youdubCaptions = { key, isJson, get, siblingUrl, fetchText, refused: (id) => refused === id };
})();
