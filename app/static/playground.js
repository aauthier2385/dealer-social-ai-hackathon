"use strict";
// The token stays in this page's memory only: never stored, never put in the URL.
(function () {
  const $ = (id) => document.getElementById(id);
  const form = $("ping-form"), prompt = $("prompt"), count = $("count"), send = $("send");
  const result = $("result"), error = $("error");

  function setStatus(kind, text) {
    $("status-dot").className = "dot " + kind;
    $("status-text").textContent = text;
  }

  function updateCount() { count.textContent = prompt.value.length + " / 500"; }
  prompt.addEventListener("input", updateCount);
  updateCount();

  fetch("/health", { cache: "no-store" })
    .then((r) => r.json())
    .then((h) => h.meta_enabled
      ? setStatus("ok", "Service up · Meta AI on")
      : setStatus("warn", "Service up · Meta AI off"))
    .catch(() => setStatus("bad", "Service unreachable"));

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    const token = $("token").value.trim();
    if (!token) { showError("Paste the service token first."); return; }
    if (!prompt.value.trim()) { showError("Type a prompt first."); return; }
    send.disabled = true;
    send.textContent = "Waiting for Muse Spark…";
    try {
      const response = await fetch("/v1/ping-model", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token },
        body: JSON.stringify({ prompt: prompt.value }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const wait = response.headers.get("Retry-After");
        showError((data.message || "Request failed.") + (wait ? " (retry in " + wait + "s)" : ""));
        return;
      }
      $("reply").textContent = data.reply;
      $("model").textContent = data.model || "–";
      $("latency").textContent = (data.latency_ms / 1000).toFixed(1) + " s";
      const u = data.usage || {};
      $("tokens").textContent = u.total_tokens != null ? String(u.total_tokens) : "–";
      result.hidden = false;
    } catch (e) {
      showError("Could not reach the service.");
    } finally {
      send.disabled = false;
      send.textContent = "Send to Muse Spark";
    }
  });

  function showError(message) {
    result.hidden = true;
    error.textContent = message;
    error.hidden = false;
  }
})();
