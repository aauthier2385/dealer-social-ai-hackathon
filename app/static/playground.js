"use strict";
// The token stays in this page's memory only: never stored, never put in the URL.
(function () {
  const $ = (id) => document.getElementById(id);
  const tokenInput = $("token");

  function token() { return tokenInput.value.trim(); }

  function setStatus(kind, text) {
    $("status-dot").className = "dot " + kind;
    $("status-text").textContent = text;
  }

  fetch("/health", { cache: "no-store" })
    .then((r) => r.json())
    .then((h) => h.meta_enabled
      ? setStatus("ok", "Service up · Meta AI on")
      : setStatus("warn", "Service up · Meta AI off"))
    .catch(() => setStatus("bad", "Service unreachable"));

  function show(el, message) { el.textContent = message; el.hidden = false; }

  async function readError(response) {
    const data = await response.json().catch(() => ({}));
    const wait = response.headers.get("Retry-After");
    return (data.message || "Request failed.") + (wait ? " (retry in " + wait + "s)" : "");
  }

  // ---------- Connection test ----------
  const form = $("ping-form"), prompt = $("prompt"), count = $("count"), send = $("send");
  const result = $("result"), error = $("error");
  function updateCount() { count.textContent = prompt.value.length + " / 500"; }
  prompt.addEventListener("input", updateCount);
  updateCount();

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    if (!token()) { result.hidden = true; show(error, "Paste the service token first."); return; }
    if (!prompt.value.trim()) { result.hidden = true; show(error, "Type a prompt first."); return; }
    send.disabled = true;
    send.textContent = "Waiting for Muse Spark…";
    try {
      const response = await fetch("/v1/ping-model", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token() },
        body: JSON.stringify({ prompt: prompt.value }),
      });
      if (!response.ok) { result.hidden = true; show(error, await readError(response)); return; }
      const data = await response.json();
      $("reply").textContent = data.reply;
      $("model").textContent = data.model || "–";
      $("latency").textContent = (data.latency_ms / 1000).toFixed(1) + " s";
      const u = data.usage || {};
      $("tokens").textContent = u.total_tokens != null ? String(u.total_tokens) : "–";
      result.hidden = false;
    } catch (e) {
      result.hidden = true;
      show(error, "Could not reach the service.");
    } finally {
      send.disabled = false;
      send.textContent = "Send to Muse Spark";
    }
  });

  // ---------- Relationship memory ----------
  const recordBtn = $("record"), fileInput = $("file"), fileName = $("file-name");
  const rememberBtn = $("remember"), progress = $("progress"), memError = $("memory-error");
  const memResult = $("memory-result");
  let audioBlob = null, recorder = null, chunks = [];

  function setAudio(blob, label) {
    audioBlob = blob;
    fileName.textContent = label;
    rememberBtn.disabled = !blob;
  }

  fileInput.addEventListener("change", () => {
    const f = fileInput.files && fileInput.files[0];
    if (f) setAudio(f, f.name + " · " + (f.size / 1024 / 1024).toFixed(1) + " MB");
  });

  recordBtn.addEventListener("click", async () => {
    memError.hidden = true;
    if (recorder && recorder.state === "recording") { recorder.stop(); return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      chunks = [];
      recorder = new MediaRecorder(stream);
      recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
      recorder.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        recordBtn.classList.remove("recording");
        recordBtn.textContent = "● Record";
        setAudio(new Blob(chunks, { type: recorder.mimeType }), "Recorded in browser");
      };
      recorder.start();
      recordBtn.classList.add("recording");
      recordBtn.textContent = "■ Stop";
      fileName.textContent = "Recording… talk about the customer, then press Stop";
    } catch (e) {
      show(memError, "Microphone not available. Allow mic access, or choose a recording file.");
    }
  });

  // Decode any browser-supported audio (m4a, mp3, webm…) and re-encode as 16 kHz mono 16-bit WAV,
  // the format Muse Voice Transcribe expects. Runs entirely on this computer.
  async function toWav(blob) {
    const rate = 16000;
    const data = await blob.arrayBuffer();
    const decoded = await new OfflineAudioContext(1, 1, rate).decodeAudioData(data);
    if (decoded.duration > 300) throw new Error("too-long");
    const offline = new OfflineAudioContext(1, Math.ceil(decoded.duration * rate), rate);
    const source = offline.createBufferSource();
    source.buffer = decoded;
    source.connect(offline.destination);
    source.start();
    const samples = (await offline.startRendering()).getChannelData(0);
    const out = new DataView(new ArrayBuffer(44 + samples.length * 2));
    const text = (o, s) => { for (let i = 0; i < s.length; i++) out.setUint8(o + i, s.charCodeAt(i)); };
    text(0, "RIFF"); out.setUint32(4, 36 + samples.length * 2, true); text(8, "WAVE");
    text(12, "fmt "); out.setUint32(16, 16, true); out.setUint16(20, 1, true); out.setUint16(22, 1, true);
    out.setUint32(24, rate, true); out.setUint32(28, rate * 2, true); out.setUint16(32, 2, true);
    out.setUint16(34, 16, true); text(36, "data"); out.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      out.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return { wav: new Blob([out.buffer], { type: "audio/wav" }), seconds: decoded.duration };
  }

  function el(tag, cls, textContent) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (textContent != null) node.textContent = textContent;
    return node;
  }

  function clock(ms) {
    const s = Math.round((ms || 0) / 1000);
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }

  const LABELS = {
    family: "Family", pets: "Pets", hobbies: "Hobbies", work: "Work", vehicle: "Vehicle",
    life_event: "Life event", contact_preference: "Contact", important_date: "Date",
    personal: "Personal",
  };

  function render(data) {
    $("summary").textContent = data.summary || "";
    const c = data.checks || {};
    const checks = $("checks");
    checks.replaceChildren(
      el("span", "check", (c.extracted || 0) + " found"),
      el("span", "check" + (c.quote_not_found ? " cut" : ""), (c.quote_not_found || 0) + " source not found"),
      el("span", "check" + (c.withheld ? " cut" : ""), (c.withheld || 0) + " withheld for privacy"),
      el("span", "check" + (c.judge_rejected ? " cut" : ""), (c.judge_rejected || 0) + " rejected by self-check"),
      el("span", "check good", (c.kept || 0) + " verified & kept"),
    );

    const people = $("people");
    people.replaceChildren(...(data.people || []).map((p) => {
      const li = el("li", "person");
      li.append(el("b", null, p.name));
      const bits = [p.relationship.replace("_", " ")];
      if (p.related_to && p.relationship === "family_member") bits.push("of " + p.related_to);
      if (p.vehicle_purchased) bits.push("bought " + p.vehicle_purchased);
      if (p.vehicle_traded) bits.push("traded " + p.vehicle_traded);
      li.append(el("span", null, bits.join(" · ")));
      return li;
    }));

    const facts = $("facts");
    facts.replaceChildren(...(data.facts || []).map((f) => {
      const li = el("li", "fact");
      const top = el("div", "fact-top");
      top.append(el("span", "chip", LABELS[f.category] || f.category));
      if (f.about) top.append(el("span", "chip", f.about));
      if (f.timing) top.append(el("span", "chip timing", f.timing));
      if (f.certainty === "uncertain") top.append(el("span", "chip uncertain", "Not sure"));
      li.append(top, el("span", "fact-text", f.fact));
      const quote = el("p", "quote", "“" + f.quote + "” ");
      const link = el("button", "source-link",
        f.source.sentence_id + (f.source.speaker ? " · speaker " + f.source.speaker : "") + " · " + clock(f.source.start_ms));
      link.type = "button";
      link.addEventListener("click", () => flash(f.source.sentence_id));
      quote.append(link);
      li.append(quote);
      return li;
    }));
    if (!(data.facts || []).length) facts.replaceChildren(el("li", "quote", "Nothing verified to remember from this note."));

    const withheld = $("withheld");
    const w = data.withheld || [];
    withheld.hidden = !w.length;
    if (w.length) {
      const kinds = [...new Set(w.map((x) => x.type))].join(", ");
      withheld.textContent = "🔒 " + w.length + " sensitive detail" + (w.length > 1 ? "s" : "") +
        " withheld (" + kinds + "). Not saved, and removed from the transcript.";
    }

    const transcript = $("transcript");
    transcript.replaceChildren(...(data.transcript || []).map((s) => {
      const li = el("li", s.redacted ? "redacted" : "");
      li.id = "t-" + s.id;
      li.append(el("span", "sid", s.id + (s.speaker ? " " + s.speaker : "")), document.createTextNode(s.text));
      return li;
    }));
    $("transcript-meta").textContent = "· " + (data.transcript || []).length + " sentences · " + data.audio_seconds + " s of audio";

    const t = data.timing_ms || {}, u = data.usage || {};
    const total = ((t.transcribe_ms || 0) + (t.extract_ms || 0) + (t.judge_ms || 0)) / 1000;
    $("memory-meta").textContent =
      "Transcribe " + ((t.transcribe_ms || 0) / 1000).toFixed(1) + " s · extract " +
      ((t.extract_ms || 0) / 1000).toFixed(1) + " s · self-check " + ((t.judge_ms || 0) / 1000).toFixed(1) +
      " s · total " + total.toFixed(1) + " s · " + (u.total_tokens || 0) + " tokens (" +
      (u.reasoning_tokens || 0) + " reasoning)";
    memResult.hidden = false;
  }

  function flash(id) {
    const box = document.querySelector(".transcript-box");
    box.open = true;
    const line = $("t-" + id);
    if (!line) return;
    line.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    line.classList.add("flash");
    setTimeout(() => line.classList.remove("flash"), 1600);
  }

  rememberBtn.addEventListener("click", async () => {
    memError.hidden = true;
    if (!token()) { show(memError, "Paste the service token first."); return; }
    if (!audioBlob) return;
    rememberBtn.disabled = true;
    try {
      progress.textContent = "Converting audio on this computer…";
      let converted;
      try {
        converted = await toWav(audioBlob);
      } catch (e) {
        show(memError, e.message === "too-long"
          ? "That recording is over 5 minutes. Keep voice notes shorter."
          : "This browser could not read that file. Try Chrome or Edge, or an .m4a, .mp3 or .wav file.");
        return;
      }
      progress.textContent = "Transcribing, extracting and double-checking… (" + Math.round(converted.seconds) + " s note)";
      const response = await fetch("/v1/memory/voice-note", {
        method: "POST",
        headers: { "Content-Type": "audio/wav", "Authorization": "Bearer " + token() },
        body: converted.wav,
      });
      if (!response.ok) { memResult.hidden = true; show(memError, await readError(response)); return; }
      render(await response.json());
    } catch (e) {
      show(memError, "Could not reach the service.");
    } finally {
      progress.textContent = "";
      rememberBtn.disabled = !audioBlob;
    }
  });
})();
