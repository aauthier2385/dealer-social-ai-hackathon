// Hackathon (Oct 2026): voice note -> relationship memory, inside a customer's chat (staff only).
// Record or choose a recording, convert it to 16 kHz mono WAV in this browser, send it to this
// site (which forwards it to the Dealer Social AI service), review the facts, then save them.
(function () {
  "use strict";
  var bar = document.querySelector(".ai-bar");
  var openBtn = document.getElementById("aiVoiceOpen");
  var panel = document.getElementById("aiPanel");
  if (!bar || !openBtn || !panel) return;
  var customer = bar.getAttribute("data-customer");
  var hasPlan = bar.getAttribute("data-has-plan") === "1";

  var LABELS = { family: "Family", pets: "Pets", hobbies: "Hobbies", work: "Work", vehicle: "Vehicle",
    life_event: "Life event", contact_preference: "Contact", important_date: "Date", personal: "Personal" };

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function today() {
    var d = new Date();
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }

  var style = el("style");
  style.textContent =
    ".ai-panel{border-bottom:1px solid var(--line);padding:12px;display:grid;gap:10px;background:#fbfcfe;max-height:60vh;overflow:auto}" +
    ".ai-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}.ai-status{font-size:13px;color:var(--muted)}" +
    ".ai-err{color:var(--red);font-weight:600;margin:0}.ai-ok{color:#1d5c27;font-weight:600;margin:0}" +
    ".ai-sum{margin:0;font-weight:600}.ai-people{display:flex;flex-wrap:wrap;gap:8px}" +
    ".ai-person{border:1px solid var(--line);border-radius:10px;padding:6px 10px;font-size:13px;background:#fff}" +
    ".ai-person button{background:none;border:0;padding:0;color:var(--text);font-weight:700;text-decoration:underline dotted;cursor:pointer}" +
    ".ai-person .heard{display:block;color:#9a6700;font-size:11px}.ai-person input{width:12ch;padding:3px 6px}" +
    ".ai-facts{list-style:none;margin:0;padding:0;display:grid;gap:6px}" +
    ".ai-fact{display:flex;gap:8px;align-items:flex-start;background:#fff;border:1px solid var(--line);border-radius:10px;padding:8px 10px}" +
    ".ai-fact input{margin-top:4px}.ai-fact b{display:block}.ai-q{font-size:12px;color:var(--muted)}" +
    ".ai-chip{font-size:10px;letter-spacing:.03em;text-transform:uppercase;border:1px solid var(--line);border-radius:999px;padding:1px 7px;color:var(--muted);margin-right:4px}" +
    ".ai-chip.unsure{border-color:var(--amber);color:#9a6700}" +
    ".ai-fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}" +
    ".ai-fields label{font-size:12px;font-weight:700;display:grid;gap:3px}.ai-fields input{padding:7px 9px;border:1px solid var(--line);border-radius:8px;font:inherit}" +
    ".ai-withheld{border:1px dashed var(--line);border-radius:10px;padding:8px 10px;font-size:13px;color:var(--muted)}";
  document.head.appendChild(style);

  var audio = null, recorder = null, chunks = [], memory = null;

  // Decode any browser-supported audio and re-encode it as 16 kHz mono 16-bit WAV.
  function toWav(blob) {
    var rate = 16000;
    return blob.arrayBuffer().then(function (data) {
      return new OfflineAudioContext(1, 1, rate).decodeAudioData(data);
    }).then(function (decoded) {
      if (decoded.duration > 300) throw new Error("too-long");
      var offline = new OfflineAudioContext(1, Math.ceil(decoded.duration * rate), rate);
      var source = offline.createBufferSource();
      source.buffer = decoded;
      source.connect(offline.destination);
      source.start();
      return offline.startRendering();
    }).then(function (rendered) {
      var samples = rendered.getChannelData(0);
      var out = new DataView(new ArrayBuffer(44 + samples.length * 2));
      function text(o, s) { for (var i = 0; i < s.length; i++) out.setUint8(o + i, s.charCodeAt(i)); }
      text(0, "RIFF"); out.setUint32(4, 36 + samples.length * 2, true); text(8, "WAVE");
      text(12, "fmt "); out.setUint32(16, 16, true); out.setUint16(20, 1, true); out.setUint16(22, 1, true);
      out.setUint32(24, rate, true); out.setUint32(28, rate * 2, true); out.setUint16(32, 2, true);
      out.setUint16(34, 16, true); text(36, "data"); out.setUint32(40, samples.length * 2, true);
      for (var i = 0; i < samples.length; i++) {
        var s = Math.max(-1, Math.min(1, samples[i]));
        out.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
      }
      return new Blob([out.buffer], { type: "audio/wav" });
    });
  }

  function start() {
    panel.replaceChildren();
    var intro = el("p", "ai-status", "Talk about the customer: family, pets, work, the vehicle and anything coming up. The AI keeps the details, ties each one to your words, and withholds anything sensitive.");
    var row = el("div", "ai-row");
    var rec = el("button", "btn sm ghost", "● Record");
    rec.type = "button";
    var pick = el("label", "btn sm ghost", "Choose recording…");
    var file = el("input");
    file.type = "file";
    file.accept = "audio/*,.m4a,.mp3,.wav,.webm";
    file.hidden = true;
    pick.appendChild(file);
    var go = el("button", "btn sm", "Remember this customer");
    go.type = "button";
    go.disabled = true;
    var status = el("span", "ai-status", "No recording yet");
    var err = el("p", "ai-err");
    err.hidden = true;
    row.append(rec, pick, go, status);
    panel.append(intro, row, err);

    function setAudio(blob, label) { audio = blob; status.textContent = label; go.disabled = !blob; }
    file.addEventListener("change", function () {
      var f = file.files && file.files[0];
      if (f) setAudio(f, f.name + " · " + (f.size / 1048576).toFixed(1) + " MB");
    });
    rec.addEventListener("click", function () {
      err.hidden = true;
      if (recorder && recorder.state === "recording") { recorder.stop(); return; }
      navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
        chunks = [];
        recorder = new MediaRecorder(stream);
        recorder.ondataavailable = function (e) { if (e.data.size) chunks.push(e.data); };
        recorder.onstop = function () {
          stream.getTracks().forEach(function (t) { t.stop(); });
          rec.textContent = "● Record";
          setAudio(new Blob(chunks, { type: recorder.mimeType }), "Recorded");
        };
        recorder.start();
        rec.textContent = "■ Stop";
        status.textContent = "Recording… press Stop when you're done";
      }).catch(function () {
        err.textContent = "Microphone not available. Allow mic access, or choose a recording.";
        err.hidden = false;
      });
    });
    go.addEventListener("click", function () {
      if (!audio) return;
      go.disabled = true; rec.disabled = true; err.hidden = true;
      status.textContent = "Converting on this device…";
      toWav(audio).then(function (wav) {
        status.textContent = "Transcribing and checking every fact… (about 30 seconds)";
        return fetch("/ai/voice-note/" + customer, {
          method: "POST", credentials: "same-origin", headers: { "Content-Type": "audio/wav" }, body: wav,
        });
      }).then(function (resp) {
        return resp.json().catch(function () { return {}; }).then(function (data) {
          if (!resp.ok) throw new Error(data.message || "Something went wrong. Try again.");
          memory = data;
          review();
        });
      }).catch(function (e) {
        err.textContent = e.message === "too-long" ? "That recording is over 5 minutes. Keep voice notes shorter."
          : (e.message || "Could not read that recording.");
        err.hidden = false;
        go.disabled = false; rec.disabled = false;
        status.textContent = "";
      });
    });
  }

  // Rename a misheard person everywhere it is used; quotes stay exactly as heard.
  function swapName(text, from, to) {
    if (!text || !from || from === to) return text;
    var esc = from.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return text.replace(new RegExp("(^|[^A-Za-z])" + esc + "(?![A-Za-z])", "g"), function (m, pre) { return pre + to; });
  }
  function rename(from, to) {
    var f1 = from.split(" ")[0], t1 = to.split(" ")[0];
    function swap(t) { var o = swapName(t, from, to); if (f1 !== from && f1 !== t1) o = swapName(o, f1, t1); return o; }
    (memory.people || []).forEach(function (p) {
      if (p.name === from) { p.heard_as = p.heard_as || from; p.name = to; }
      p.related_to = swap(p.related_to);
    });
    (memory.facts || []).forEach(function (f) {
      var before = f.about + "|" + f.fact;
      f.about = swap(f.about); f.fact = swap(f.fact);
      if (before !== f.about + "|" + f.fact && !f.heard_as) f.heard_as = from;
    });
    memory.summary = swap(memory.summary);
  }

  function transcriptBox() {
    var box = el("details", "ai-withheld");
    var lines = memory.transcript || [];
    box.append(el("summary", null, "What the AI heard (" + lines.length + " sentence" + (lines.length === 1 ? "" : "s") +
      ", " + Math.round(memory.audio_seconds || 0) + " s of audio)"));
    var ol = el("ol");
    lines.forEach(function (t) { ol.append(el("li", null, (t.speaker ? t.speaker + ": " : "") + t.text)); });
    box.append(ol);
    return box;
  }

  function review() {
    panel.replaceChildren();
    panel.append(el("p", "ai-sum", memory.summary || "Here's what the AI heard."));
    var w = memory.withheld || [];
    if (w.length) {
      panel.append(el("div", "ai-withheld", "🔒 " + w.length + " sensitive detail" + (w.length > 1 ? "s" : "") +
        " withheld (" + Array.from(new Set(w.map(function (x) { return x.type; }))).join(", ") + "). Not saved."));
    }
    var people = el("div", "ai-people");
    (memory.people || []).forEach(function (p) {
      var box = el("span", "ai-person");
      var b = el("button", null, p.name + " ✎");
      b.type = "button";
      b.title = "Fix this name";
      b.addEventListener("click", function () {
        var input = el("input");
        input.value = p.name; input.maxLength = 60;
        box.replaceChildren(input);
        input.focus(); input.select();
        function done(save) {
          var v = input.value.replace(/\s+/g, " ").trim();
          if (save && v && v !== p.name && /^[A-Za-z][A-Za-z .'’-]{0,59}$/.test(v)) rename(p.name, v);
          review();
        }
        input.addEventListener("keydown", function (e) {
          if (e.key === "Enter") { e.preventDefault(); done(true); }
          if (e.key === "Escape") done(false);
        });
        input.addEventListener("blur", function () { done(true); });
      });
      box.append(b, document.createTextNode(" " + (p.relationship || "").replace("_", " ")));
      if (p.heard_as) box.append(el("span", "heard", "heard as “" + p.heard_as + "”"));
      people.append(box);
    });
    panel.append(el("div", "ai-status", "Tap a name to fix it. Untick anything you don't want kept."), people);

    var list = el("ul", "ai-facts");
    (memory.facts || []).forEach(function (f) {
      var li = el("li", "ai-fact");
      var cb = el("input");
      cb.type = "checkbox"; cb.checked = f.keep !== false;
      cb.setAttribute("aria-label", "Keep: " + f.fact);
      cb.addEventListener("change", function () { f.keep = cb.checked; });
      var body = el("div");
      var chips = el("div");
      chips.append(el("span", "ai-chip", LABELS[f.category] || f.category));
      if (f.about) chips.append(el("span", "ai-chip", f.about));
      if (f.timing) chips.append(el("span", "ai-chip", f.timing));
      if (f.certainty === "uncertain") chips.append(el("span", "ai-chip unsure", "Not sure"));
      body.append(chips, el("b", null, f.fact), el("span", "ai-q", "“" + f.quote + "”"));
      li.append(cb, body);
      list.append(li);
    });
    panel.append(list);
    panel.append(transcriptBox());
    if (!(memory.facts || []).length) {
      // Nothing to save: say what was heard and offer another try instead of an empty form.
      panel.append(el("p", "ai-err", (memory.transcript || []).length
        ? "Nothing to remember from that note. Open \u201cWhat the AI heard\u201d to check it, then try again and mention the customer by name, their vehicle, family, pets or what's coming up."
        : "No speech was heard. Check the microphone and try again."));
      var again = el("button", "btn sm", "Record again");
      again.type = "button";
      again.addEventListener("click", function () { memory = null; audio = null; start(); });
      panel.append(again);
      return;
    }

    var customerP = (memory.people || []).filter(function (p) { return p.relationship === "customer"; })[0] || {};
    var fields = el("div", "ai-fields");
    function field(label, value, attrs) {
      var l = el("label", null, label);
      var i = el("input");
      i.value = value || "";
      Object.keys(attrs || {}).forEach(function (k) { i.setAttribute(k, attrs[k]); });
      l.append(i);
      fields.append(l);
      return i;
    }
    var vehicle = field("Vehicle", customerP.vehicle_purchased, { maxlength: "80" });
    var delivered = field("Delivery date", today(), { type: "date" });
    var birthday = field("Birthday (optional)", "", { placeholder: "MM-DD", maxlength: "5", inputmode: "numeric" });
    panel.append(fields);

    var row = el("div", "ai-row");
    var plan = el("label", "ai-status");
    var planBox = el("input");
    planBox.type = "checkbox"; planBox.checked = !hasPlan;
    plan.append(planBox, document.createTextNode(hasPlan ? " Rebuild the follow-up plan" : " Build the follow-up plan in my voice"));
    var save = el("button", "btn sm green", "Save to customer");
    save.type = "button";
    var cancel = el("button", "btn sm ghost", "Discard");
    cancel.type = "button";
    var msg = el("p", "ai-err");
    msg.hidden = true;
    row.append(save, cancel, plan);
    panel.append(row, msg);
    cancel.addEventListener("click", function () { memory = null; audio = null; panel.hidden = true; });
    save.addEventListener("click", function () {
      save.disabled = true; cancel.disabled = true; msg.hidden = true;
      save.textContent = planBox.checked ? "Saving and planning…" : "Saving…";
      var facts = (memory.facts || []).filter(function (f) { return f.keep !== false; }).map(function (f) {
        return { about: f.about, category: f.category, fact: f.fact, timing: f.timing, certainty: f.certainty,
                 quote: f.quote, heard_as: f.heard_as || "" };
      });
      fetch("/ai/voice-note/" + customer + "/save", {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ vehicle: vehicle.value, delivery_date: delivered.value, birthday: birthday.value,
                               make_plan: planBox.checked, facts: facts }),
      }).then(function (resp) {
        return resp.json().catch(function () { return {}; }).then(function (data) {
          if (!resp.ok) throw new Error(data.message || "Could not save.");
          panel.replaceChildren();
          var ok = el("p", "ai-ok", "Saved " + data.facts_saved + " fact" + (data.facts_saved === 1 ? "" : "s") +
            (data.follow_ups ? " · " + data.follow_ups + " follow-ups scheduled in your voice." : "."));
          panel.append(ok);
          if (data.plan_error) panel.append(el("p", "ai-err", data.plan_error));
          var link = el("a", "btn sm", "Open Follow-ups");
          link.href = "/follow-ups";
          panel.append(link);
          setTimeout(function () { location.reload(); }, 2500);
        });
      }).catch(function (e) {
        msg.textContent = e.message; msg.hidden = false;
        save.disabled = false; cancel.disabled = false; save.textContent = "Save to customer";
      });
    });
  }

  openBtn.addEventListener("click", function () {
    if (panel.hidden) { panel.hidden = false; if (!memory) start(); } else { panel.hidden = true; }
  });
})();
