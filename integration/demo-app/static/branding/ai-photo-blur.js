// Hackathon (Oct 2026): privacy blur before posting a store photo (staff only, SAM 3.1).
// When staff choose a photo in the News Feed composer, offer to blur people in the background
// and license plates. The blurred photo (with no hidden location data) replaces the chosen file,
// so the post uses it. Tap a face to keep or blur it; "Use original" undoes the blur.
(function () {
  "use strict";
  var input = document.getElementById("media");
  var preview = document.getElementById("preview");
  if (!input || !preview || !window.fetch || !window.DataTransfer) return;

  var style = document.createElement("style");
  style.textContent =
    ".pb-panel{display:grid;gap:8px;margin-top:8px}.pb-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}" +
    ".pb-status{font-size:13px;color:var(--muted)}.pb-err{color:var(--red);font-weight:600;font-size:13px}" +
    ".pb-ok{color:#1d5c27;font-weight:600;font-size:13px}.pb-stage{position:relative;display:inline-block;justify-self:start;width:fit-content;max-width:100%}" +
    ".pb-stage img{display:block;max-width:100%;max-height:420px;width:auto;height:auto;border-radius:10px}" +
    ".pb-box{position:absolute;padding:0;background:transparent;border:2px solid #2e9e4f;border-radius:6px;cursor:pointer}" +
    ".pb-box.blurred{border-color:var(--red);border-style:dashed}.pb-box.plate{border-color:var(--amber);cursor:default}" +
    ".pb-box span{position:absolute;left:0;top:100%;margin-top:2px;font:600 10px system-ui;background:#fff;color:#152233;padding:1px 4px;border-radius:4px;white-space:nowrap}";
  document.head.appendChild(style);

  var original = null, overrides = {}, busy = false, current = null;

  // The panel sits just below the composer's own preview. When the composer clears its preview
  // (Remove), the panel goes too.
  new MutationObserver(function () {
    if (current && !preview.firstChild) { current.remove(); current = null; original = null; }
  }).observe(preview, { childList: true });

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // JPEG, PNG and WebP go as they are; anything else (e.g. HEIC) or a very large photo is
  // re-encoded as JPEG in this browser first.
  function photoBody(file) {
    var ok = ["image/jpeg", "image/png", "image/webp"].indexOf(file.type) >= 0;
    if (ok && file.size <= 10 * 1024 * 1024) return Promise.resolve({ blob: file, type: file.type });
    return createImageBitmap(file).then(function (bitmap) {
      var scale = Math.min(1, 3000 / Math.max(bitmap.width, bitmap.height));
      var canvas = document.createElement("canvas");
      canvas.width = Math.round(bitmap.width * scale);
      canvas.height = Math.round(bitmap.height * scale);
      canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      return new Promise(function (resolve) { canvas.toBlob(resolve, "image/jpeg", 0.92); });
    }).then(function (blob) { return { blob: blob, type: "image/jpeg" }; });
  }

  function useFile(file) {
    var dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
  }

  function points(kind) {
    return Object.keys(overrides).filter(function (k) { return overrides[k] === kind; }).join(";");
  }

  function offer(panel) {
    panel.replaceChildren();
    var row = el("div", "pb-row");
    var go = el("button", "btn sm", "🛡 Blur bystanders & plates");
    go.type = "button";
    var all = el("label", "pb-status");
    var allBox = el("input");
    allBox.type = "checkbox";
    all.append(allBox, document.createTextNode(" Blur every face"));
    row.append(go, all);
    panel.append(row, el("span", "pb-status", "SAM 3.1 finds faces and license plates. The customer stays sharp; people in the background and plates are blurred."));
    go.addEventListener("click", function () { run(panel, allBox.checked); });
  }

  function run(panel, blurAll) {
    if (busy || !original) return;
    busy = true;
    var status = el("span", "pb-status", "Finding faces and license plates…");
    panel.replaceChildren(status);
    photoBody(original).then(function (body) {
      var q = new URLSearchParams();
      if (points("kept")) q.set("keep", points("kept"));
      if (points("blurred")) q.set("blur", points("blurred"));
      if (blurAll) q.set("blur_all_faces", "1");
      return fetch("/ai/photo-blur" + (q.toString() ? "?" + q : ""), {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": body.type }, body: body.blob,
      });
    }).then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (!resp.ok) throw new Error(data.message || "Could not blur this photo.");
        // Decoded here rather than fetched: the site's security policy does not fetch data: URLs.
        var bin = atob(String(data.image).split(",")[1] || "");
        var bytes = new Uint8Array(bin.length);
        for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        var name = (original.name || "photo").replace(/\.[^.]+$/, "") + "-blurred.jpg";
        useFile(new File([bytes], name, { type: "image/jpeg" }));
        show(panel, data, blurAll);
      });
    }).catch(function (e) {
      panel.replaceChildren(el("p", "pb-err", e.message || "Could not blur this photo."));
      var again = el("button", "btn sm ghost", "Try again");
      again.type = "button";
      again.addEventListener("click", function () { offer(panel); });
      panel.append(again);
    }).then(function () { busy = false; });
  }

  function show(panel, data, blurAll) {
    preview.hidden = true; // the blurred photo below replaces the original preview
    var c = data.checks || {};
    var parts = [c.faces_kept + " kept", c.faces_blurred + " face" + (c.faces_blurred === 1 ? "" : "s") + " blurred",
      c.plates_blurred + " plate" + (c.plates_blurred === 1 ? "" : "s") + " blurred"];
    if (c.location_removed) parts.push("GPS location removed");
    else if (c.metadata_removed) parts.push("photo metadata removed");
    panel.replaceChildren(el("span", "pb-ok", "🛡 " + parts.join(" · ") + ". This blurred photo will be posted."));

    var stage = el("div", "pb-stage");
    var after = el("img");
    after.src = data.image;
    after.alt = "Photo with bystanders and plates blurred";
    stage.append(after);
    function pct(v, total) { return (100 * v / total) + "%"; }
    function place(node, b) {
      node.style.left = pct(b.left, data.width); node.style.top = pct(b.top, data.height);
      node.style.width = pct(b.right - b.left, data.width); node.style.height = pct(b.bottom - b.top, data.height);
    }
    (data.faces || []).forEach(function (f) {
      var box = el("button", "pb-box " + f.decision);
      box.type = "button";
      box.title = f.reason + ". Tap to " + (f.decision === "kept" ? "blur" : "keep") + ".";
      box.setAttribute("aria-label", (f.decision === "kept" ? "Kept face" : "Blurred face") + ", tap to change");
      box.append(el("span", null, f.decision === "kept" ? "Kept" : "Blurred"));
      place(box, f.box);
      box.addEventListener("click", function () {
        var b = f.box;
        Object.keys(overrides).forEach(function (k) {
          var p = k.split(",").map(Number);
          if (p[0] >= b.left && p[0] < b.right && p[1] >= b.top && p[1] < b.bottom) delete overrides[k];
        });
        overrides[Math.round((b.left + b.right) / 2) + "," + Math.round((b.top + b.bottom) / 2)] =
          f.decision === "kept" ? "blurred" : "kept";
        run(panel, blurAll);
      });
      stage.append(box);
    });
    (data.plates || []).forEach(function (p) {
      var box = el("span", "pb-box plate");
      box.append(el("span", null, "Plate"));
      place(box, p.box);
      stage.append(box);
    });
    var row = el("div", "pb-row");
    var undo = el("button", "btn sm ghost", "Use original");
    undo.type = "button";
    undo.addEventListener("click", function () {
      useFile(original);
      overrides = {};
      preview.hidden = false;
      offer(panel);
    });
    var remove = el("button", "btn sm danger", "Remove photo");
    remove.type = "button";
    remove.addEventListener("click", function () {
      var x = preview.querySelector("button");
      if (x) x.click(); // the composer's own Remove clears the file and its preview
    });
    row.append(el("span", "pb-status", "Tap a face to keep or blur it."), undo, remove);
    panel.append(stage, row);
  }

  input.addEventListener("change", function () {
    var f = input.files && input.files[0];
    overrides = {};
    original = null;
    if (current) { current.remove(); current = null; }
    if (!f || f.type.indexOf("image/") !== 0) return;
    original = f;
    current = el("div", "pb-panel");
    preview.after(current);
    offer(current);
  });
})();
