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

  let lastMemory = null;

  function render(data, rerender) {
    lastMemory = data;
    if (!rerender) preparePlan(data);
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
      const nameBtn = el("button", "name-btn", p.name);
      nameBtn.type = "button";
      nameBtn.title = "Fix this name";
      nameBtn.setAttribute("aria-label", "Fix the name " + p.name);
      nameBtn.addEventListener("click", () => editName(li, p.name));
      li.append(nameBtn);
      const bits = [p.relationship.replace("_", " ")];
      if (p.related_to && (p.relationship === "family_member" || p.relationship === "pet")) bits.push("of " + p.related_to);
      if (p.vehicle_purchased) bits.push("bought " + p.vehicle_purchased);
      if (p.vehicle_traded) bits.push("traded " + p.vehicle_traded);
      li.append(el("span", null, bits.join(" · ")));
      if (p.heard_as) li.append(el("span", "heard", "Fixed · heard as “" + p.heard_as + "”"));
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
      if (f.asked) li.append(el("p", "asked", "Asked: “" + f.asked + "”"));
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

  // ---------- Name corrections ----------
  // Transcription can mishear short names ("Bell" heard as "Bill"). The salesperson fixes the name
  // once and it carries into the people, the facts, the summary and the follow-up plan. Quotes and
  // the transcript stay exactly as heard, so every fact still points at its real source.
  const NAME_OK = /^[A-Za-z][A-Za-z .'’-]{0,59}$/;

  function swapName(text, from, to) {
    if (!text || !from || from === to) return text;
    const esc = from.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return text.replace(new RegExp("(^|[^A-Za-z])" + esc + "(?![A-Za-z])", "g"), (m, pre) => pre + to);
  }

  function renameEverywhere(from, to) {
    const firstFrom = from.split(" ")[0], firstTo = to.split(" ")[0];
    const swap = (t) => {
      let out = swapName(t, from, to);
      if (firstFrom !== from && firstFrom !== firstTo) out = swapName(out, firstFrom, firstTo);
      return out;
    };
    const m = lastMemory;
    (m.people || []).forEach((p) => {
      if (p.name === from) { p.heard_as = p.heard_as || from; p.name = to; }
      p.related_to = swap(p.related_to);
    });
    (m.facts || []).forEach((f) => { f.about = swap(f.about); f.fact = swap(f.fact); });
    m.summary = swap(m.summary);
    ["plan-customer", "plan-salesperson"].forEach((id) => { $(id).value = swap($(id).value); });
    ["plan-result", "ho-result"].forEach((id) => { $(id).hidden = true; }); // old drafts used the misheard name
    ["ho-from", "ho-to"].forEach((id) => { $(id).value = swap($(id).value); });
  }

  function editName(li, current) {
    const input = el("input", "name-input");
    input.value = current;
    input.maxLength = 60;
    input.setAttribute("aria-label", "Correct name for " + current);
    const save = el("button", "copy", "Save");
    const cancel = el("button", "copy", "Cancel");
    save.type = cancel.type = "button";
    const note = el("span", "name-error");
    const box = el("div", "name-edit");
    box.append(input, save, cancel, note);
    li.replaceChildren(box);
    input.focus();
    input.select();
    const done = () => render(lastMemory, true);
    const commit = () => {
      const name = input.value.replace(/\s+/g, " ").trim();
      if (name === current) { done(); return; }
      if (!NAME_OK.test(name)) { note.textContent = "Letters, spaces, - . ' only"; return; }
      if ((lastMemory.people || []).some((p) => p.name === name)) { note.textContent = "Someone already has that name"; return; }
      renameEverywhere(current, name);
      done();
    };
    save.addEventListener("click", commit);
    cancel.addEventListener("click", done);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commit(); }
      if (e.key === "Escape") done();
    });
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

  // ---------- Follow-up plan ----------
  const planCard = $("plan-card"), planForm = $("plan-form"), planBtn = $("plan-btn");
  const planError = $("plan-error"), planResult = $("plan-result"), planProgress = $("plan-progress");

  function today() {
    const d = new Date();
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }

  function preparePlan(data) {
    const people = data.people || [];
    const customer = people.find((p) => p.relationship === "customer") || {};
    const salesperson = people.find((p) => p.relationship === "salesperson") || {};
    $("plan-customer").value = customer.name || "";
    $("plan-vehicle").value = customer.vehicle_purchased || "";
    $("plan-salesperson").value = salesperson.name || "";
    if (!$("plan-date").value) $("plan-date").value = today();
    planResult.hidden = true;
    planError.hidden = true;
    planCard.hidden = false;
    $("ho-from").value = salesperson.name || "";
    $("ho-result").hidden = true;
    $("ho-error").hidden = true;
    $("handoff-card").hidden = false;
  }

  const CHANNELS = { text: "Text", call: "Call", call_or_text: "Call or text", mail: "Mail" };

  function copyButton(text) {
    const b = el("button", "copy", "Copy");
    b.type = "button";
    b.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(text);
        b.textContent = "Copied";
      } catch (e) {
        const range = document.createRange();
        range.selectNodeContents(b.previousSibling);
        const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
        b.textContent = "Press Ctrl+C";
      }
      setTimeout(() => { b.textContent = "Copy"; }, 1600);
    });
    return b;
  }

  function draftBlock(text, cls) {
    const box = el("div", "draft" + (cls ? " " + cls : ""));
    const p = el("p");
    text.split("{booking_link}").forEach((part, i) => {
      if (i) p.append(el("span", "booking", "[booking link]"));
      p.append(document.createTextNode(part));
    });
    box.append(p, copyButton(text));
    return box;
  }

  function niceDate(iso) {
    const d = new Date(iso + "T12:00:00");
    return {
      day: d.toLocaleDateString(undefined, { month: "short", day: "numeric" }),
      rest: d.toLocaleDateString(undefined, { weekday: "short", year: "numeric" }),
    };
  }

  function renderPlan(data) {
    const c = data.checks || {};
    $("plan-checks").replaceChildren(
      el("span", "check good", (c.drafted || 0) + " drafted & checked"),
      el("span", "check" + (c.replaced_with_safe_draft ? " cut" : ""), (c.replaced_with_safe_draft || 0) + " replaced with a safe draft"),
      el("span", "check good", (c.life_events_kept || 0) + " life-event check-ins"),
      el("span", "check" + (c.life_events_dropped ? " cut" : ""), (c.life_events_dropped || 0) + " suggestions dropped"),
      el("span", "check good", data.voice && data.voice.source === "salesperson_samples"
        ? "Voice: " + ($("plan-salesperson").value.trim().split(" ")[0] || "salesperson") + "'s texts (" + data.voice.samples_used + ")"
        : "Voice: store default"),
    );
    $("timeline").replaceChildren(...(data.touchpoints || []).map((tp) => {
      const li = el("li", "tp");
      const nd = niceDate(tp.date);
      const date = el("div", "tp-date");
      date.append(el("b", null, nd.day), document.createTextNode(nd.rest));
      const body = el("div", "tp-body");
      const head = el("div", "tp-head");
      head.append(el("span", "tp-title", tp.title), el("span", "chip channel", CHANNELS[tp.channel] || tp.channel));
      if (tp.kind === "life_event") head.append(el("span", "chip timing", "Life event"));
      if ((tp.safe_draft_used || []).length) head.append(el("span", "chip safe", "Safe draft"));
      body.append(head);
      if (tp.card) body.append(draftBlock(tp.card, "card-msg"));
      if (tp.text) body.append(draftBlock(tp.text));
      if ((tp.call_notes || []).length) {
        const ul = el("ul", "notes");
        tp.call_notes.forEach((n) => ul.append(el("li", null, n)));
        body.append(el("span", "chip", "Call notes"), ul);
      }
      if ((tp.uses_facts || []).length) {
        const uses = el("div", "tp-head");
        tp.uses_facts.forEach((f) => uses.append(el("span", "chip", f.id + " · " + f.fact)));
        body.append(uses);
      }
      if (tp.why) body.append(el("p", "why", tp.why));
      li.append(date, body);
      return li;
    }));
    const u = data.usage || {};
    $("plan-meta").textContent = (data.latency_ms / 1000).toFixed(1) + " s · " + (u.total_tokens || 0) +
      " tokens (" + (u.reasoning_tokens || 0) + " reasoning) · " + (data.model || "");
    planResult.hidden = false;
  }

  planForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    planError.hidden = true;
    if (!token()) { show(planError, "Paste the service token first."); return; }
    if (!lastMemory) return;
    const body = {
      customer_name: $("plan-customer").value.trim(),
      vehicle: $("plan-vehicle").value.trim(),
      salesperson_name: $("plan-salesperson").value.trim(),
      store_name: $("plan-store").value.trim(),
      delivery_date: $("plan-date").value,
      birthday: $("plan-birthday").value.trim(),
      voice_samples: $("plan-voice").value.split("\n").map((t) => t.trim()).filter(Boolean).slice(0, 5),
      facts: (lastMemory.facts || []).map((f) => ({
        id: f.id, about: f.about, category: f.category, fact: f.fact, timing: f.timing, certainty: f.certainty,
      })),
    };
    if (!body.customer_name || !body.salesperson_name) {
      show(planError, "Fill in the customer and salesperson names."); return;
    }
    planBtn.disabled = true;
    planProgress.textContent = "Planning and checking every draft…";
    try {
      const response = await fetch("/v1/plan/touchpoints", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token() },
        body: JSON.stringify(body),
      });
      if (!response.ok) { planResult.hidden = true; show(planError, await readError(response)); return; }
      renderPlan(await response.json());
    } catch (e) {
      show(planError, "Could not reach the service.");
    } finally {
      planBtn.disabled = false;
      planProgress.textContent = "";
    }
  });

  // ---------- Salesperson handoff ----------
  const hoForm = $("handoff-form"), hoBtn = $("ho-btn"), hoError = $("ho-error"), hoResult = $("ho-result");

  function factChips(refs) {
    const row = el("div", "fact-top");
    (refs || []).forEach((f) => row.append(el("span", "chip", f.id + " · " + f.fact)));
    return row;
  }

  function pointList(id, points, empty) {
    $(id).replaceChildren(...(points || []).map((p) => {
      const li = el("li", "fact");
      const text = el("span", "fact-text", p.point);
      li.append(text);
      if (p.not_sure) { const top = el("div", "fact-top"); top.append(el("span", "chip uncertain", "Not sure")); li.prepend(top); }
      li.append(factChips(p.uses_facts));
      return li;
    }));
    if (!(points || []).length) $(id).replaceChildren(el("li", "quote", empty));
  }

  function renderHandoff(data) {
    const c = data.checks || {};
    $("ho-checks").replaceChildren(
      el("span", "check good", (c.points_kept || 0) + " points, each tied to a fact"),
      el("span", "check" + (c.points_dropped ? " cut" : ""), (c.points_dropped || 0) + " dropped"),
      el("span", "check" + (c.intro_replaced_with_safe_draft ? " cut" : " good"),
        c.intro_replaced_with_safe_draft ? "Intro replaced with a safe draft" : "Intro checked"),
      el("span", "check good", data.voice && data.voice.source === "salesperson_samples"
        ? "Voice: " + (data.to_salesperson || "").split(" ")[0] + "'s texts (" + data.voice.samples_used + ")"
        : "Voice: store default"),
    );
    $("ho-headline").textContent = data.headline || "";
    pointList("ho-know", data.know_first, "No verified facts to brief on yet.");
    pointList("ho-talk", data.talk_about, "No conversation starters from the verified facts.");
    const intro = data.intro || {};
    const box = $("ho-intro");
    const head = el("div", "tp-head");
    head.append(el("span", "tp-title", (data.to_salesperson || "").split(" ")[0] + " → " + (data.customer || "").split(" ")[0]),
      el("span", "chip channel", "Text"));
    if ((intro.safe_draft_used || []).length) head.append(el("span", "chip safe", "Safe draft"));
    box.replaceChildren(head, draftBlock(intro.text || ""));
    if ((intro.call_notes || []).length) {
      const ul = el("ul", "notes");
      intro.call_notes.forEach((n) => ul.append(el("li", null, n)));
      box.append(el("span", "chip", "Or call: notes"), ul);
    }
    if ((intro.uses_facts || []).length) box.append(factChips(intro.uses_facts));
    const u = data.usage || {};
    $("ho-meta").textContent = (data.latency_ms / 1000).toFixed(1) + " s · " + (u.total_tokens || 0) +
      " tokens (" + (u.reasoning_tokens || 0) + " reasoning) · " + (data.model || "");
    hoResult.hidden = false;
  }

  hoForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    hoError.hidden = true;
    if (!token()) { show(hoError, "Paste the service token first."); return; }
    if (!lastMemory) return;
    const body = {
      customer_name: $("plan-customer").value.trim(),
      vehicle: $("plan-vehicle").value.trim(),
      store_name: $("plan-store").value.trim(),
      customer_since: $("plan-date").value,
      from_salesperson: $("ho-from").value.trim(),
      to_salesperson: $("ho-to").value.trim(),
      voice_samples: $("ho-voice").value.split("\n").map((t) => t.trim()).filter(Boolean).slice(0, 5),
      facts: (lastMemory.facts || []).map((f) => ({
        id: f.id, about: f.about, category: f.category, fact: f.fact, timing: f.timing, certainty: f.certainty,
      })),
    };
    if (!body.customer_name || !body.from_salesperson || !body.to_salesperson) {
      show(hoError, "Fill in the customer and both salespeople."); return;
    }
    hoBtn.disabled = true;
    $("ho-progress").textContent = "Writing the brief and checking every line…";
    try {
      const response = await fetch("/v1/handoff/brief", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token() },
        body: JSON.stringify(body),
      });
      if (!response.ok) { hoResult.hidden = true; show(hoError, await readError(response)); return; }
      renderHandoff(await response.json());
    } catch (e) {
      show(hoError, "Could not reach the service.");
    } finally {
      hoBtn.disabled = false;
      $("ho-progress").textContent = "";
    }
  });

  // ---------- Relationships at risk ----------
  const riskForm = $("risk-form"), riskBtn = $("risk-btn"), riskError = $("risk-error"), riskResult = $("risk-result");

  function daysAgo(n) {
    const d = new Date();
    d.setDate(d.getDate() - n);
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }
  const MONTH = new Date().toLocaleString("en-US", { month: "long" });

  // Fictional customers, dated relative to today so the demo always works.
  function sampleStore() {
    return [
      { ref: "c1", customer_name: "Mark Stevenson", salesperson: "Joey", salesperson_active: false, vehicle: "26 Ford Ranger Lariat",
        delivery_date: daysAgo(6), last_contact_date: daysAgo(5), overdue: [{ kind: "card", due_date: daysAgo(4) }],
        facts: [{ id: "F1", about: "Bell", category: "pets", fact: "Has a little Frenchie named Bell" },
                { id: "F2", about: "Mark Stevenson", category: "hobbies", fact: "Cuts down a lot of trees" }] },
      { ref: "c2", customer_name: "Madeline Parks", salesperson: "Jayden", vehicle: "2024 Ford Escape Active",
        delivery_date: daysAgo(35), last_contact_date: daysAgo(34), overdue: [{ kind: "day30", due_date: daysAgo(5) }],
        facts: [{ id: "F1", about: "Madeline Parks", category: "family", fact: "Bought a farm next to her kids" }] },
      { ref: "c3", customer_name: "Mike Carter", salesperson: "Andrew", vehicle: "blue F-150",
        delivery_date: daysAgo(345), last_contact_date: daysAgo(240),
        facts: [{ id: "F1", about: "Emma", category: "life_event", fact: "Daughter Emma turns 16", timing: MONTH, certainty: "stated" },
                { id: "F2", about: "Mike Carter", category: "work", fact: "Owns a sawmill" }] },
      { ref: "c4", customer_name: "Carl Jeffers", salesperson: "Tony", vehicle: "2020 Hyundai Elantra",
        delivery_date: daysAgo(6), last_contact_date: daysAgo(5), overdue: [] },
      { ref: "c5", customer_name: "Rosa Delgado", salesperson: "Joey", salesperson_active: false, vehicle: "2025 Ford Bronco Sport",
        delivery_date: daysAgo(400), last_contact_date: daysAgo(200),
        facts: [{ id: "F1", about: "Rosa Delgado", category: "hobbies", fact: "Coaches her son's baseball team" }] },
      { ref: "c6", customer_name: "Dale Whitaker", salesperson: "Tony", vehicle: "2026 Ford Expedition",
        delivery_date: daysAgo(95), last_contact_date: daysAgo(64), overdue: [{ kind: "day90", due_date: daysAgo(12) }],
        facts: [{ id: "F1", about: "Dale Whitaker", category: "hobbies", fact: "Pulls a camper to the lake every summer" }] },
      { ref: "c7", customer_name: "Priya Shah", salesperson: "Andrew", vehicle: "2026 Ford Maverick",
        delivery_date: daysAgo(20), last_contact_date: daysAgo(19), overdue: [] },
      { ref: "c8", customer_name: "Tom Becker", salesperson: "Jayden", vehicle: "2025 Ford Mustang",
        delivery_date: daysAgo(380), last_contact_date: daysAgo(110), overdue: [{ kind: "anniversary", due_date: daysAgo(15) }],
        facts: [{ id: "F1", about: "Tom Becker", category: "hobbies", fact: "Takes the Mustang to car shows" }] },
    ];
  }

  let riskCustomers = sampleStore();
  $("risk-sample").addEventListener("click", () => {
    riskCustomers = sampleStore();
    $("risk-loaded").textContent = riskCustomers.length + " fictional customers loaded, dated from today";
  });

  function renderRisk(data) {
    $("risk-headline").textContent = data.headline || "";
    const t = data.totals || {}, c = data.checks || {};
    const chips = [
      el("span", "check", (t.customers_checked || 0) + " checked by code"),
      el("span", "check" + (t.needs_attention ? " cut" : " good"), (t.needs_attention || 0) + " need attention"),
      el("span", "check" + (t.no_salesperson ? " cut" : ""), (t.no_salesperson || 0) + " with no salesperson"),
      el("span", "check" + (t.overdue_follow_ups ? " cut" : ""), (t.overdue_follow_ups || 0) + " overdue follow-ups"),
    ];
    if (data.model) chips.push(el("span", "check good", (c.explained_by_ai || 0) + " explained by Muse Spark"));
    if (c.replaced_with_plain_explanation) chips.push(el("span", "check cut", c.replaced_with_plain_explanation + " replaced with a plain explanation"));
    $("risk-checks").replaceChildren(...chips);

    $("risk-list").replaceChildren(...(data.customers || []).map((r) => {
      const li = el("li", "tp");
      li.append(el("div", "rank", "#" + r.rank));
      const body = el("div", "tp-body");
      const head = el("div", "tp-head");
      head.append(el("span", "tp-title", r.customer), el("span", "chip " + r.level, r.level === "high" ? "High" : "Medium"));
      head.append(el("span", "chip", r.salesperson ? r.salesperson + (r.salesperson_active ? "" : " (left)") : "No salesperson"));
      if (r.explained_by === "code") head.append(el("span", "chip", "Explained by code"));
      body.append(head);
      const sig = el("ul", "signals");
      (r.signals || []).forEach((s) => sig.append(el("li", s.cited ? "cited" : "", s.text)));
      body.append(sig);
      if (r.why) body.append(el("p", null, r.why));
      const step = el("p", "step");
      step.append(el("b", null, "Next: "), document.createTextNode(r.next_step + " "), el("span", "chip channel", r.owner));
      body.append(step);
      if (r.opener) body.append(draftBlock(r.opener));
      if ((r.uses_facts || []).length) body.append(factChips(r.uses_facts));
      li.append(body);
      return li;
    }));
    if (!(data.customers || []).length) $("risk-list").replaceChildren(el("li", "quote", "Every relationship is on track this week."));
    const u = data.usage || {};
    $("risk-meta").textContent = data.model
      ? (data.latency_ms / 1000).toFixed(1) + " s · " + (u.total_tokens || 0) + " tokens (" + (u.reasoning_tokens || 0) + " reasoning) · " + data.model
      : "Code only · no model call · no cost";
    riskResult.hidden = false;
  }

  riskForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    riskError.hidden = true;
    if (!token()) { show(riskError, "Paste the service token first."); return; }
    const top = parseInt($("risk-top").value, 10);
    const body = { store_name: "Demo Motors", as_of: daysAgo(0), top: top >= 1 && top <= 15 ? top : 5,
      ai: $("risk-ai").checked, customers: riskCustomers };
    riskBtn.disabled = true;
    $("risk-progress").textContent = body.ai ? "Ranking in code, then explaining the top " + body.top + "…" : "Ranking in code…";
    try {
      const response = await fetch("/v1/risk/brief", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": "Bearer " + token() },
        body: JSON.stringify(body),
      });
      if (!response.ok) { riskResult.hidden = true; show(riskError, await readError(response)); return; }
      renderRisk(await response.json());
    } catch (e) {
      show(riskError, "Could not reach the service.");
    } finally {
      riskBtn.disabled = false;
      $("risk-progress").textContent = "";
    }
  });

  // ---------- Privacy blur (SAM 3.1) ----------
  const photoFile = $("photo-file"), photoBtn = $("photo-btn"), photoError = $("photo-error");
  const photoResult = $("photo-result");
  let photoBlob = null, lastPhoto = null, beforeUrl = null;
  // Taps are remembered as points in the photo ("x,y" -> "kept" | "blurred"), so they still
  // apply when the photo is processed again.
  let overrides = {};

  // JPEG, PNG and WebP go as they are; anything else (e.g. HEIC) or a very large photo is
  // re-encoded here as JPEG first. Either way the server strips all metadata.
  async function photoBody(file) {
    const ok = ["image/jpeg", "image/png", "image/webp"].includes(file.type);
    if (ok && file.size <= 10 * 1024 * 1024) return { blob: file, type: file.type };
    const bitmap = await createImageBitmap(file);
    const scale = Math.min(1, 3000 / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", 0.92));
    return { blob, type: "image/jpeg" };
  }

  photoFile.addEventListener("change", () => {
    const f = photoFile.files && photoFile.files[0];
    if (!f) return;
    photoBlob = f;
    overrides = {};
    $("photo-name").textContent = f.name + " · " + (f.size / 1024 / 1024).toFixed(1) + " MB";
    photoBtn.disabled = false;
    photoResult.hidden = true;
  });

  function points(kind) {
    return Object.keys(overrides).filter((k) => overrides[k] === kind).join(";");
  }

  async function runBlur() {
    photoError.hidden = true;
    if (!token()) { show(photoError, "Paste the service token first."); return; }
    if (!photoBlob) return;
    photoBtn.disabled = true;
    $("photo-progress").textContent = "Finding faces and plates with SAM 3.1…";
    try {
      const { blob, type } = await photoBody(photoBlob);
      const query = new URLSearchParams();
      const keep = points("kept"), blurPts = points("blurred");
      if (keep) query.set("keep", keep);
      if (blurPts) query.set("blur", blurPts);
      if ($("photo-all").checked) query.set("blur_all_faces", "1");
      const response = await fetch("/v1/photo/privacy-blur" + (query.toString() ? "?" + query : ""), {
        method: "POST",
        headers: { "Content-Type": type, "Authorization": "Bearer " + token() },
        body: blob,
      });
      if (!response.ok) { photoResult.hidden = true; show(photoError, await readError(response)); return; }
      if (beforeUrl) URL.revokeObjectURL(beforeUrl);
      beforeUrl = URL.createObjectURL(blob);
      renderPhoto(await response.json());
    } catch (e) {
      show(photoError, "Could not read that photo, or the service is unreachable.");
    } finally {
      photoBtn.disabled = false;
      $("photo-progress").textContent = "";
    }
  }
  photoBtn.addEventListener("click", runBlur);

  function renderPhoto(data) {
    lastPhoto = data;
    const c = data.checks || {};
    const chips = [
      el("span", "check good", (c.faces_kept || 0) + " kept"),
      el("span", "check" + (c.faces_blurred ? " cut" : ""), (c.faces_blurred || 0) + " face" + (c.faces_blurred === 1 ? "" : "s") + " blurred"),
      el("span", "check" + (c.plates_blurred ? " cut" : ""), (c.plates_blurred || 0) + " plate" + (c.plates_blurred === 1 ? "" : "s") + " blurred"),
    ];
    if (c.location_removed) chips.push(el("span", "check good", "GPS location removed"));
    else if (c.metadata_removed) chips.push(el("span", "check good", "Photo metadata removed"));
    $("photo-checks").replaceChildren(...chips);
    $("photo-before").src = beforeUrl;
    $("photo-after").src = data.image;
    $("photo-download").href = data.image;

    const stage = $("photo-stage");
    stage.querySelectorAll(".face-box").forEach((n) => n.remove());
    const pct = (v, total) => (100 * v / total) + "%";
    const box = (item, cls, label) => {
      const b = item.box;
      const node = el(cls === "plate" ? "span" : "button", "face-box " + cls);
      node.style.left = pct(b.left, data.width);
      node.style.top = pct(b.top, data.height);
      node.style.width = pct(b.right - b.left, data.width);
      node.style.height = pct(b.bottom - b.top, data.height);
      node.append(el("span", null, label));
      return node;
    };
    (data.faces || []).forEach((f) => {
      const node = box(f, f.decision, f.decision === "kept" ? "Kept" : "Blurred");
      node.type = "button";
      node.title = f.reason + ". Tap to " + (f.decision === "kept" ? "blur" : "keep") + ".";
      node.setAttribute("aria-label", (f.decision === "kept" ? "Kept face" : "Blurred face") + ", " + f.reason + ". Tap to change.");
      node.addEventListener("click", () => {
        const b = f.box;
        Object.keys(overrides).forEach((k) => {
          const [x, y] = k.split(",").map(Number);
          if (x >= b.left && x < b.right && y >= b.top && y < b.bottom) delete overrides[k];
        });
        overrides[Math.round((b.left + b.right) / 2) + "," + Math.round((b.top + b.bottom) / 2)] =
          f.decision === "kept" ? "blurred" : "kept";
        runBlur();
      });
      stage.append(node);
    });
    (data.plates || []).forEach((p) => stage.append(box(p, "plate", "Plate")));
    const u = data.usage || {};
    $("photo-meta").textContent = (data.latency_ms / 1000).toFixed(1) + " s · " + data.width + "×" + data.height +
      " · " + (u.total_tokens || 0) + " tokens · " + (data.model || "");
    photoResult.hidden = false;
  }
})();

