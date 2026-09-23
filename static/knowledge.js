(function () {
  const root = document.getElementById("knowledge-app");
  if (!root || !window.KG_BOOT) return;

  const urls = JSON.parse(root.dataset.urls || "{}");
  const noteUrl = (id, kind) => {
    const key = kind || "note";
    return (urls[key] || "").replace(/\/0(\/|$)/, `/${id}$1`);
  };

  let notes = Array.isArray(window.KG_BOOT.notes) ? window.KG_BOOT.notes.slice() : [];
  let graph = window.KG_BOOT.graph || { nodes: [], edges: [] };
  let selectedId = null;
  let saveTimer = null;
  let dirty = false;
  let saving = false;
  let mode = "edit";

  const els = {
    list: document.getElementById("kg-note-list"),
    search: document.getElementById("kg-search"),
    canvas: document.getElementById("kg-canvas"),
    empty: document.getElementById("kg-graph-empty"),
    stats: document.getElementById("kg-graph-stats"),
    editor: document.getElementById("kg-editor"),
    title: document.getElementById("kg-note-title"),
    body: document.getElementById("kg-note-body"),
    preview: document.getElementById("kg-note-preview"),
    toggleModeBtn: document.getElementById("kg-toggle-mode"),
    status: document.getElementById("kg-save-status"),
    outgoing: document.getElementById("kg-outgoing"),
    incoming: document.getElementById("kg-incoming"),
    showUnresolved: document.getElementById("kg-show-unresolved"),
    linkModeBtn: document.getElementById("kg-link-mode"),
    linkHint: document.getElementById("kg-link-hint"),
  };

  function graphTheme() {
    const styles = getComputedStyle(document.documentElement);
    return {
      text: styles.getPropertyValue("--text").trim() || "#e8eaef",
      muted: styles.getPropertyValue("--muted").trim() || "#8b93a7",
      accent: styles.getPropertyValue("--accent").trim() || "#5b8cff",
      accentDim: styles.getPropertyValue("--accent-dim").trim() || "#3d5fb8",
      border: styles.getPropertyValue("--border").trim() || "#2a3142",
      danger: styles.getPropertyValue("--danger").trim() || "#e45858",
    };
  }

  function escapeRegExp(s) {
    return (s || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function wikilinkPattern(title, flags) {
    const esc = escapeRegExp((title || "").trim());
    return new RegExp(`\\[\\[\\s*${esc}\\s*(#[^\\]|]*)?(\\|[^\\]]*)?\\s*\\]\\]`, flags || "i");
  }

  function hasWikilink(body, title) {
    return wikilinkPattern(title).test(body || "");
  }

  function syncNoteCache(note) {
    if (!note) return;
    const idx = notes.findIndex((n) => n.id === note.id);
    const entry = {
      id: note.id,
      title: note.title,
      body: note.body,
      created_at: note.created_at,
      updated_at: note.updated_at,
      out_count: (note.outgoing || []).length,
      in_count: (note.incoming || []).length,
    };
    if (idx >= 0) notes[idx] = entry;
    else notes.unshift(entry);
  }

  function withAlpha(color, alpha) {
    const match = /^#([0-9a-f]{6})$/i.exec(color);
    if (!match) return color;
    const value = parseInt(match[1], 16);
    return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`;
  }

  function setStatus(text) {
    if (els.status) els.status.textContent = text || "";
  }

  async function api(url, options) {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.error || "Ошибка запроса");
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  function renderNoteList() {
    const query = (els.search.value || "").trim().toLowerCase();
    const filtered = notes.filter((n) => {
      if (!query) return true;
      return (
        n.title.toLowerCase().includes(query) ||
        (n.body || "").toLowerCase().includes(query)
      );
    });

    els.list.innerHTML = "";
    if (!filtered.length) {
      const li = document.createElement("li");
      li.className = "kg-note-list__empty muted";
      li.textContent = notes.length ? "Ничего не найдено" : "Нет заметок";
      els.list.appendChild(li);
      return;
    }

    filtered.forEach((n) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "kg-note-list__item" + (n.id === selectedId ? " is-active" : "");
      btn.innerHTML =
        `<span class="kg-note-list__name"></span>` +
        `<span class="kg-note-list__meta muted"></span>`;
      btn.querySelector(".kg-note-list__name").textContent = n.title;
      const links = (n.in_count || 0) + (n.out_count || 0);
      btn.querySelector(".kg-note-list__meta").textContent = links
        ? `${n.out_count || 0}→ · ←${n.in_count || 0}`
        : "без связей";
      btn.addEventListener("click", () => openNote(n.id));
      li.appendChild(btn);
      els.list.appendChild(li);
    });
  }

  function renderLinks(listEl, items, emptyText) {
    listEl.innerHTML = "";
    if (!items || !items.length) {
      const li = document.createElement("li");
      li.className = "muted";
      li.textContent = emptyText;
      listEl.appendChild(li);
      return;
    }
    items.forEach((item) => {
      const li = document.createElement("li");
      if (item.id) {
        const a = document.createElement("button");
        a.type = "button";
        a.className = "kg-links__link";
        a.textContent = item.title;
        a.addEventListener("click", () => openNote(item.id));
        li.appendChild(a);
      } else {
        const wrap = document.createElement("span");
        wrap.className = "kg-links__unresolved";
        const label = document.createElement("span");
        label.textContent = item.title;
        const actions = document.createElement("span");
        actions.className = "kg-links__unresolved-actions";
        const create = document.createElement("button");
        create.type = "button";
        create.className = "btn btn--ghost btn--xs";
        create.textContent = "Создать";
        create.addEventListener("click", () => createNote(item.title));
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "kg-links__remove";
        remove.title = "Убрать ссылку";
        remove.setAttribute("aria-label", "Убрать ссылку «" + item.title + "»");
        remove.textContent = "✕";
        remove.addEventListener("click", () =>
          removeDirectLink({ resolved: true, note_id: selectedId }, { title: item.title })
        );
        actions.appendChild(create);
        actions.appendChild(remove);
        wrap.appendChild(label);
        wrap.appendChild(actions);
        li.appendChild(wrap);
      }
      listEl.appendChild(li);
    });
  }

  function applyNoteToEditor(note, opts) {
    const preserveMode = !!(opts && opts.preserveMode);
    selectedId = note.id;
    els.editor.hidden = false;
    root.classList.add("has-editor");
    els.title.value = note.title || "";
    els.body.value = note.body || "";
    dirty = false;
    setStatus("");
    renderLinks(els.outgoing, note.outgoing || [], "Нет исходящих ссылок");
    renderLinks(els.incoming, note.incoming || [], "Нет обратных ссылок");
    renderNoteList();
    sim.highlightNoteId = note.id;
    if (preserveMode) {
      if (mode === "read") renderPreview(els.body.value);
    } else {
      setEditorMode(note.body && note.body.trim() ? "read" : "edit");
    }
  }

  async function openNote(id) {
    if (dirty && selectedId && selectedId !== id) {
      await saveNote(true);
    }
    try {
      const note = await api(noteUrl(id, "note"));
      applyNoteToEditor(note);
      syncNoteCache(note);
      renderNoteList();
    } catch (_) {
      setStatus("Не удалось открыть");
    }
  }

  async function createNote(titleHint) {
    if (dirty && selectedId) await saveNote(true);
    let title = (titleHint || "").trim();
    if (!title) {
      const base = "Новая заметка";
      const taken = new Set(notes.map((n) => n.title.toLowerCase()));
      title = base;
      let i = 2;
      while (taken.has(title.toLowerCase())) {
        title = `${base} ${i}`;
        i += 1;
      }
    }
    try {
      const data = await api(urls.create, {
        method: "POST",
        body: JSON.stringify({ title, body: "" }),
      });
      if (data.graph) setGraph(data.graph);
      if (data.note) {
        syncNoteCache(data.note);
        applyNoteToEditor(data.note);
        els.title.focus();
        els.title.select();
      }
      renderNoteList();
    } catch (err) {
      if (err.status === 409 && err.data && err.data.id) {
        openNote(err.data.id);
        return;
      }
      setStatus(err.message || "Ошибка");
    }
  }

  async function saveNote(silent) {
    if (!selectedId || saving) return;
    const title = els.title.value.trim();
    const body = els.body.value;
    if (!title) {
      setStatus("Нужно название");
      return;
    }
    saving = true;
    if (!silent) setStatus("Сохранение…");
    try {
      const data = await api(noteUrl(selectedId, "update"), {
        method: "POST",
        body: JSON.stringify({ title, body }),
      });
      dirty = false;
      if (data.graph) setGraph(data.graph);
      if (data.note) {
        syncNoteCache(data.note);
        applyNoteToEditor(data.note, { preserveMode: true });
      }
      setStatus(silent ? "" : "Сохранено");
      renderNoteList();
    } catch (err) {
      setStatus(err.message || "Ошибка сохранения");
    } finally {
      saving = false;
    }
  }

  function scheduleSave() {
    dirty = true;
    setStatus("Изменено");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => saveNote(true), 700);
  }

  async function deleteNote() {
    if (!selectedId) return;
    const title = els.title.value.trim() || "заметку";
    if (!confirm(`Удалить «${title}»?`)) return;
    try {
      const data = await api(noteUrl(selectedId, "delete"), { method: "POST", body: "{}" });
      notes = notes.filter((n) => n.id !== selectedId);
      selectedId = null;
      dirty = false;
      els.editor.hidden = true;
      root.classList.remove("has-editor");
      if (data.graph) setGraph(data.graph);
      sim.highlightNoteId = null;
      renderNoteList();
    } catch (_) {
      setStatus("Не удалось удалить");
    }
  }

  function closeEditor() {
    if (dirty) saveNote(true);
    selectedId = null;
    els.editor.hidden = true;
    root.classList.remove("has-editor");
    sim.highlightNoteId = null;
    renderNoteList();
  }

  /* ---------- Read mode (rendered preview without raw [[brackets]]) ---------- */

  const WIKILINK_RE = /\[\[\s*([^\]|#]+?)\s*(?:#[^|\]]*)?(?:\|([^\]]+))?\s*\]\]/g;

  function buildTitleLookup() {
    const map = new Map();
    notes.forEach((n) => map.set(n.title.toLowerCase().trim(), n));
    return map;
  }

  function appendLineWithLinks(container, line, lookup) {
    WIKILINK_RE.lastIndex = 0;
    let lastIndex = 0;
    let match;
    while ((match = WIKILINK_RE.exec(line))) {
      if (match.index > lastIndex) {
        container.appendChild(document.createTextNode(line.slice(lastIndex, match.index)));
      }
      const rawTitle = match[1].trim();
      const alias = (match[2] || "").trim();
      const found = lookup.get(rawTitle.toLowerCase());
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "kg-wikilink" + (found ? "" : " kg-wikilink--unresolved");
      chip.textContent = alias || rawTitle;
      if (found) {
        chip.title = rawTitle;
        chip.addEventListener("click", (ev) => {
          ev.stopPropagation();
          openNote(found.id);
        });
      } else {
        chip.title = "Создать «" + rawTitle + "»";
        chip.addEventListener("click", (ev) => {
          ev.stopPropagation();
          createNote(rawTitle);
        });
      }
      container.appendChild(chip);
      lastIndex = WIKILINK_RE.lastIndex;
    }
    if (lastIndex < line.length) {
      container.appendChild(document.createTextNode(line.slice(lastIndex)));
    }
  }

  function renderPreview(body) {
    els.preview.innerHTML = "";
    const text = body || "";
    if (!text.trim()) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "Пусто. Нажмите, чтобы написать.";
      els.preview.appendChild(empty);
      return;
    }
    const lookup = buildTitleLookup();
    text.split(/\n{2,}/).forEach((para) => {
      const p = document.createElement("p");
      const lines = para.split("\n");
      lines.forEach((line, i) => {
        appendLineWithLinks(p, line, lookup);
        if (i < lines.length - 1) p.appendChild(document.createElement("br"));
      });
      els.preview.appendChild(p);
    });
  }

  function setEditorMode(next) {
    mode = next;
    if (mode === "read") {
      renderPreview(els.body.value);
      els.body.hidden = true;
      els.preview.hidden = false;
      if (els.toggleModeBtn) els.toggleModeBtn.textContent = "Править";
    } else {
      els.preview.hidden = true;
      els.body.hidden = false;
      if (els.toggleModeBtn) els.toggleModeBtn.textContent = "Читать";
    }
  }

  if (els.toggleModeBtn) {
    els.toggleModeBtn.addEventListener("click", () => {
      if (mode === "edit") {
        if (dirty) saveNote(true);
        setEditorMode("read");
      } else {
        setEditorMode("edit");
        window.requestAnimationFrame(() => els.body.focus());
      }
    });
  }

  if (els.preview) {
    els.preview.addEventListener("click", (ev) => {
      if (ev.target.closest(".kg-wikilink")) return;
      setEditorMode("edit");
      window.requestAnimationFrame(() => els.body.focus());
    });
  }

  /* ---------- Direct linking (drag point to point on the graph) ---------- */

  async function createDirectLink(source, target) {
    if (!source || !source.resolved || !source.note_id) {
      setStatus("Сначала создайте заметку для этой точки");
      return;
    }
    const targetTitle = (target && target.title || "").trim();
    if (!targetTitle) return;
    try {
      const fresh = await api(noteUrl(source.note_id, "note"));
      const body = fresh.body || "";
      if (hasWikilink(body, targetTitle)) {
        setStatus("Уже связано");
        return;
      }
      const addition = `[[${targetTitle}]]`;
      const newBody = body.trim() ? `${body}\n\n${addition}` : addition;
      const data = await api(noteUrl(source.note_id, "update"), {
        method: "POST",
        body: JSON.stringify({ body: newBody }),
      });
      if (data.graph) setGraph(data.graph);
      if (data.note) syncNoteCache(data.note);
      if (selectedId === source.note_id && data.note) {
        applyNoteToEditor(data.note, { preserveMode: true });
      }
      renderNoteList();
      setStatus(`Связано с «${targetTitle}»`);
    } catch (err) {
      setStatus(err.message || "Не удалось связать");
    }
  }

  async function removeDirectLink(source, target) {
    if (!source || !source.resolved || !source.note_id) return;
    const targetTitle = (target && target.title || "").trim();
    if (!targetTitle) return;
    try {
      const fresh = await api(noteUrl(source.note_id, "note"));
      const body = fresh.body || "";
      const pattern = wikilinkPattern(targetTitle, "gi");
      if (!pattern.test(body)) {
        setStatus("Ссылка не найдена");
        return;
      }
      const newBody = body.replace(wikilinkPattern(targetTitle, "gi"), "").replace(/\n{3,}/g, "\n\n").trim();
      const data = await api(noteUrl(source.note_id, "update"), {
        method: "POST",
        body: JSON.stringify({ body: newBody }),
      });
      if (data.graph) setGraph(data.graph);
      if (data.note) syncNoteCache(data.note);
      if (selectedId === source.note_id && data.note) {
        applyNoteToEditor(data.note, { preserveMode: true });
      }
      renderNoteList();
      setStatus(`Связь с «${targetTitle}» убрана`);
    } catch (err) {
      setStatus(err.message || "Не удалось убрать связь");
    }
  }

  /* ---------- Force-directed graph ---------- */
  const sim = {
    nodes: [],
    edges: [],
    highlightNoteId: null,
    alpha: 1,
    showUnresolved: true,
    transform: { x: 0, y: 0, k: 1 },
    dragNode: null,
    panning: false,
    lastX: 0,
    lastY: 0,
    hovered: null,
    linkMode: false,
    linkFrom: null,
    linkPointer: { x: 0, y: 0 },
    hoveredEdge: null,
  };

  function setGraph(next) {
    graph = next || { nodes: [], edges: [] };
    rebuildSimulation(true);
  }

  function rebuildSimulation(preservePositions) {
    const prev = new Map(sim.nodes.map((n) => [n.id, n]));
    const showU = !!els.showUnresolved.checked;
    sim.showUnresolved = showU;

    const rawNodes = (graph.nodes || []).filter((n) => n.resolved || showU);
    const allowed = new Set(rawNodes.map((n) => n.id));
    const nodes = rawNodes.map((n) => {
      const old = preservePositions ? prev.get(n.id) : null;
      return {
        id: n.id,
        note_id: n.note_id,
        title: n.title,
        resolved: !!n.resolved,
        x: old ? old.x : (Math.random() - 0.5) * 240,
        y: old ? old.y : (Math.random() - 0.5) * 240,
        vx: old ? old.vx * 0.2 : 0,
        vy: old ? old.vy * 0.2 : 0,
        fx: null,
        fy: null,
      };
    });

    const idToNode = new Map(nodes.map((n) => [n.id, n]));
    const edges = [];
    (graph.edges || []).forEach((e) => {
      if (!allowed.has(e.source) || !allowed.has(e.target)) return;
      const s = idToNode.get(e.source);
      const t = idToNode.get(e.target);
      if (!s || !t) return;
      edges.push({ source: s, target: t });
    });

    sim.nodes = nodes;
    sim.edges = edges;
    sim.alpha = 1;
    updateStats();
    els.empty.hidden = nodes.length > 0;
  }

  function updateStats() {
    const resolved = sim.nodes.filter((n) => n.resolved).length;
    const unresolved = sim.nodes.length - resolved;
    els.stats.textContent = sim.nodes.length
      ? `${resolved} зам.${unresolved ? ` · ${unresolved} несозд.` : ""} · ${sim.edges.length} связей`
      : "";
  }

  function stepSimulation() {
    const nodes = sim.nodes;
    const edges = sim.edges;
    if (!nodes.length) return;

    const alpha = sim.alpha;
    const n = nodes.length;
    const charge = -180 - Math.min(n, 40) * 4;
    const linkDist = 90 + Math.min(n, 30) * 1.5;

    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const a = nodes[i];
        const b = nodes[j];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let dist2 = dx * dx + dy * dy || 0.01;
        let dist = Math.sqrt(dist2);
        let force = (charge * alpha) / dist2;
        let fx = (dx / dist) * force;
        let fy = (dy / dist) * force;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
    }

    for (let i = 0; i < edges.length; i++) {
      const e = edges[i];
      const a = e.source;
      const b = e.target;
      let dx = b.x - a.x;
      let dy = b.y - a.y;
      let dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
      let diff = dist - linkDist;
      let force = diff * 0.04 * alpha;
      let fx = (dx / dist) * force;
      let fy = (dy / dist) * force;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }

    for (let i = 0; i < n; i++) {
      const node = nodes[i];
      node.vx += (-node.x) * 0.01 * alpha;
      node.vy += (-node.y) * 0.01 * alpha;
      if (node.fx != null) {
        node.x = node.fx;
        node.vx = 0;
      } else {
        node.vx *= 0.85;
        node.x += node.vx;
      }
      if (node.fy != null) {
        node.y = node.fy;
        node.vy = 0;
      } else {
        node.vy *= 0.85;
        node.y += node.vy;
      }
    }

    sim.alpha = Math.max(0.002, alpha * 0.985);
  }

  function resizeCanvas() {
    const canvas = els.canvas;
    const parent = canvas.parentElement;
    const dpr = window.devicePixelRatio || 1;
    const w = parent.clientWidth;
    const h = parent.clientHeight;
    canvas.width = Math.max(1, Math.floor(w * dpr));
    canvas.height = Math.max(1, Math.floor(h * dpr));
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    return { w, h, dpr };
  }

  function screenToWorld(sx, sy, size) {
    const t = sim.transform;
    return {
      x: (sx - size.w / 2 - t.x) / t.k,
      y: (sy - size.h / 2 - t.y) / t.k,
    };
  }

  function findNodeAt(sx, sy, size) {
    const p = screenToWorld(sx, sy, size);
    let best = null;
    let bestDist = 18 / sim.transform.k;
    for (let i = sim.nodes.length - 1; i >= 0; i--) {
      const n = sim.nodes[i];
      const dx = n.x - p.x;
      const dy = n.y - p.y;
      const d = Math.sqrt(dx * dx + dy * dy);
      const r = (n.resolved ? 10 : 7) / sim.transform.k + 4 / sim.transform.k;
      if (d < r && d < bestDist + 4) {
        best = n;
        bestDist = d;
      }
    }
    return best;
  }

  function distToSegment(px, py, ax, ay, bx, by) {
    const dx = bx - ax;
    const dy = by - ay;
    const lenSq = dx * dx + dy * dy;
    let t = lenSq ? ((px - ax) * dx + (py - ay) * dy) / lenSq : 0;
    t = Math.max(0, Math.min(1, t));
    const cx = ax + t * dx;
    const cy = ay + t * dy;
    const ddx = px - cx;
    const ddy = py - cy;
    return Math.sqrt(ddx * ddx + ddy * ddy);
  }

  function findEdgeAt(sx, sy, size) {
    const p = screenToWorld(sx, sy, size);
    let best = null;
    let bestDist = 8 / sim.transform.k;
    for (const e of sim.edges) {
      const d = distToSegment(p.x, p.y, e.source.x, e.source.y, e.target.x, e.target.y);
      if (d < bestDist) {
        best = e;
        bestDist = d;
      }
    }
    return best;
  }

  function draw() {
    const canvas = els.canvas;
    const ctx = canvas.getContext("2d");
    const colors = graphTheme();
    const size = resizeCanvas();
    const { w, h, dpr } = size;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    if (sim.alpha > 0.003) stepSimulation();

    ctx.save();
    ctx.translate(w / 2 + sim.transform.x, h / 2 + sim.transform.y);
    ctx.scale(sim.transform.k, sim.transform.k);

    const hlId = sim.highlightNoteId;
    const hover = sim.hovered;
    const neighborIds = new Set();
    if (hlId || hover) {
      const focusIds = new Set();
      if (hover) focusIds.add(hover.id);
      sim.nodes.forEach((n) => {
        if (hlId && n.note_id === hlId) focusIds.add(n.id);
      });
      sim.edges.forEach((e) => {
        if (focusIds.has(e.source.id) || focusIds.has(e.target.id)) {
          neighborIds.add(e.source.id);
          neighborIds.add(e.target.id);
        }
      });
      focusIds.forEach((id) => neighborIds.add(id));
    }
    const dimming = neighborIds.size > 0;

    sim.edges.forEach((e) => {
      const removable = sim.linkMode && !sim.linkFrom && sim.hoveredEdge === e;
      const active =
        removable || !dimming || (neighborIds.has(e.source.id) && neighborIds.has(e.target.id));
      ctx.beginPath();
      ctx.moveTo(e.source.x, e.source.y);
      ctx.lineTo(e.target.x, e.target.y);
      if (removable) {
        ctx.strokeStyle = withAlpha(colors.danger, 0.9);
        ctx.lineWidth = 2.2 / sim.transform.k;
      } else {
        ctx.strokeStyle = active ? withAlpha(colors.accent, 0.52) : withAlpha(colors.border, 0.58);
        ctx.lineWidth = (active ? 1.4 : 1) / sim.transform.k;
      }
      ctx.stroke();
    });

    if (sim.linkMode && sim.linkFrom) {
      ctx.beginPath();
      ctx.moveTo(sim.linkFrom.x, sim.linkFrom.y);
      ctx.lineTo(sim.linkPointer.x, sim.linkPointer.y);
      ctx.strokeStyle = withAlpha(colors.accent, 0.85);
      ctx.lineWidth = 1.8 / sim.transform.k;
      ctx.setLineDash([5 / sim.transform.k, 4 / sim.transform.k]);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    sim.nodes.forEach((n) => {
      const selected = hlId && n.note_id === hlId;
      const active = !dimming || neighborIds.has(n.id);
      const r = n.resolved ? (selected ? 9 : 7) : 5;

      ctx.beginPath();
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      if (!n.resolved) {
        ctx.fillStyle = withAlpha(colors.muted, active ? 0.35 : 0.12);
        ctx.fill();
        ctx.strokeStyle = withAlpha(colors.muted, active ? 0.9 : 0.3);
        ctx.lineWidth = 1.2 / sim.transform.k;
        ctx.setLineDash([3 / sim.transform.k, 3 / sim.transform.k]);
        ctx.stroke();
        ctx.setLineDash([]);
      } else {
        ctx.fillStyle = selected
          ? colors.accent
          : active
            ? colors.accentDim
            : withAlpha(colors.accentDim, 0.25);
        ctx.fill();
        if (selected || (hover && hover.id === n.id)) {
          ctx.strokeStyle = colors.text;
          ctx.lineWidth = 1.5 / sim.transform.k;
          ctx.stroke();
        }
      }

      const showLabel =
        sim.transform.k > 0.55 || selected || (hover && hover.id === n.id) || sim.nodes.length < 18;
      if (showLabel && active) {
        ctx.font = `${12 / sim.transform.k}px "Segoe UI", system-ui, sans-serif`;
        ctx.fillStyle = n.resolved ? withAlpha(colors.text, 0.94) : withAlpha(colors.muted, 0.9);
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        const label =
          n.title.length > 28 ? n.title.slice(0, 27) + "…" : n.title;
        ctx.fillText(label, n.x, n.y + r + 4 / sim.transform.k);
      }
    });

    ctx.restore();
    requestAnimationFrame(draw);
  }

  function fitView() {
    if (!sim.nodes.length) {
      sim.transform = { x: 0, y: 0, k: 1 };
      return;
    }
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    sim.nodes.forEach((n) => {
      minX = Math.min(minX, n.x);
      maxX = Math.max(maxX, n.x);
      minY = Math.min(minY, n.y);
      maxY = Math.max(maxY, n.y);
    });
    const bw = Math.max(40, maxX - minX);
    const bh = Math.max(40, maxY - minY);
    const parent = els.canvas.parentElement;
    const pad = 80;
    const k = Math.min(
      1.8,
      Math.max(0.35, Math.min((parent.clientWidth - pad) / bw, (parent.clientHeight - pad) / bh))
    );
    sim.transform.k = k;
    sim.transform.x = (-(minX + maxX) / 2) * k;
    sim.transform.y = (-(minY + maxY) / 2) * k;
  }

  function canvasPos(evt) {
    const rect = els.canvas.getBoundingClientRect();
    return { x: evt.clientX - rect.left, y: evt.clientY - rect.top };
  }

  els.canvas.addEventListener("wheel", (evt) => {
    evt.preventDefault();
    const size = { w: els.canvas.clientWidth, h: els.canvas.clientHeight };
    const p = canvasPos(evt);
    const before = screenToWorld(p.x, p.y, size);
    const factor = evt.deltaY < 0 ? 1.1 : 0.9;
    sim.transform.k = Math.min(3.5, Math.max(0.25, sim.transform.k * factor));
    const after = screenToWorld(p.x, p.y, size);
    sim.transform.x += (after.x - before.x) * sim.transform.k;
    sim.transform.y += (after.y - before.y) * sim.transform.k;
  }, { passive: false });

  els.canvas.addEventListener("pointerdown", (evt) => {
    const size = { w: els.canvas.clientWidth, h: els.canvas.clientHeight };
    const p = canvasPos(evt);
    const node = findNodeAt(p.x, p.y, size);
    els.canvas.setPointerCapture(evt.pointerId);
    if (sim.linkMode) {
      if (node) {
        if (!node.resolved) {
          setStatus("Сначала создайте заметку для этой точки");
          return;
        }
        sim.linkFrom = node;
        sim.linkPointer = screenToWorld(p.x, p.y, size);
      } else {
        const edge = findEdgeAt(p.x, p.y, size);
        if (edge) {
          removeDirectLink(edge.source, edge.target);
        } else {
          sim.panning = true;
          sim.lastX = evt.clientX;
          sim.lastY = evt.clientY;
        }
      }
      return;
    }
    if (node) {
      sim.dragNode = node;
      node.fx = node.x;
      node.fy = node.y;
      sim.alpha = Math.max(sim.alpha, 0.3);
    } else {
      sim.panning = true;
      sim.lastX = evt.clientX;
      sim.lastY = evt.clientY;
    }
  });

  els.canvas.addEventListener("pointermove", (evt) => {
    const size = { w: els.canvas.clientWidth, h: els.canvas.clientHeight };
    const p = canvasPos(evt);
    if (sim.linkMode) {
      if (sim.linkFrom) {
        sim.linkPointer = screenToWorld(p.x, p.y, size);
        sim.hovered = findNodeAt(p.x, p.y, size);
        sim.hoveredEdge = null;
      } else if (sim.panning) {
        sim.transform.x += evt.clientX - sim.lastX;
        sim.transform.y += evt.clientY - sim.lastY;
        sim.lastX = evt.clientX;
        sim.lastY = evt.clientY;
      } else {
        sim.hovered = findNodeAt(p.x, p.y, size);
        sim.hoveredEdge = sim.hovered ? null : findEdgeAt(p.x, p.y, size);
      }
      els.canvas.style.cursor = sim.panning ? "grabbing" : "crosshair";
      return;
    }
    if (sim.dragNode) {
      const world = screenToWorld(p.x, p.y, size);
      sim.dragNode.fx = world.x;
      sim.dragNode.fy = world.y;
      sim.alpha = Math.max(sim.alpha, 0.2);
    } else if (sim.panning) {
      sim.transform.x += evt.clientX - sim.lastX;
      sim.transform.y += evt.clientY - sim.lastY;
      sim.lastX = evt.clientX;
      sim.lastY = evt.clientY;
    } else {
      sim.hovered = findNodeAt(p.x, p.y, size);
      els.canvas.style.cursor = sim.hovered ? "pointer" : "grab";
    }
  });

  function endPointer(evt) {
    if (sim.linkMode && sim.linkFrom) {
      const size = { w: els.canvas.clientWidth, h: els.canvas.clientHeight };
      const p = canvasPos(evt);
      const target = findNodeAt(p.x, p.y, size);
      const source = sim.linkFrom;
      sim.linkFrom = null;
      if (target && target.id !== source.id) createDirectLink(source, target);
    }
    if (sim.dragNode) {
      const node = sim.dragNode;
      node.fx = null;
      node.fy = null;
      sim.dragNode = null;
    }
    sim.panning = false;
    try {
      els.canvas.releasePointerCapture(evt.pointerId);
    } catch (_) {}
  }

  els.canvas.addEventListener("pointerup", endPointer);
  els.canvas.addEventListener("pointercancel", endPointer);

  els.canvas.addEventListener("click", (evt) => {
    if (sim.linkMode) return;
    const size = { w: els.canvas.clientWidth, h: els.canvas.clientHeight };
    const p = canvasPos(evt);
    const node = findNodeAt(p.x, p.y, size);
    if (!node) return;
    if (node.resolved && node.note_id) {
      openNote(node.note_id);
    } else {
      createNote(node.title);
    }
  });

  els.canvas.addEventListener("dblclick", (evt) => {
    if (sim.linkMode) return;
    const size = { w: els.canvas.clientWidth, h: els.canvas.clientHeight };
    const p = canvasPos(evt);
    const node = findNodeAt(p.x, p.y, size);
    if (!node) createNote("");
  });

  function setLinkMode(next) {
    sim.linkMode = next;
    sim.linkFrom = null;
    sim.hoveredEdge = null;
    if (els.linkModeBtn) {
      els.linkModeBtn.classList.toggle("is-active", next);
      els.linkModeBtn.setAttribute("aria-pressed", next ? "true" : "false");
    }
    if (els.linkHint) els.linkHint.hidden = !next;
    els.canvas.style.cursor = next ? "crosshair" : "grab";
    setStatus(next ? "Режим связи включён" : "");
  }

  if (els.linkModeBtn) {
    els.linkModeBtn.addEventListener("click", () => setLinkMode(!sim.linkMode));
  }

  document.addEventListener("keydown", (evt) => {
    if (evt.key !== "Escape") return;
    if (sim.linkFrom) {
      sim.linkFrom = null;
      return;
    }
    if (sim.linkMode) setLinkMode(false);
  });

  document.getElementById("kg-new-note").addEventListener("click", () => createNote(""));
  document.getElementById("kg-close-editor").addEventListener("click", closeEditor);
  document.getElementById("kg-delete-note").addEventListener("click", deleteNote);
  const importBtn = document.getElementById("kg-import-todo");
  if (importBtn && urls.importTodo) {
    importBtn.addEventListener("click", async () => {
      if (
        !confirm(
          "Импортировать проекты и задачи из TO-DO в граф знаний?\nСуществующие заметки с теми же названиями будут обновлены."
        )
      ) {
        return;
      }
      importBtn.disabled = true;
      try {
        const res = await fetch(urls.importTodo, {
          method: "POST",
          headers: {
            Accept: "application/json",
            "X-Requested-With": "fetch",
          },
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || "Ошибка импорта");
        if (data.graph) setGraph(data.graph);
        if (Array.isArray(data.notes_list)) notes = data.notes_list;
        else {
          const listed = await api(urls.notes);
          notes = listed.notes || [];
        }
        renderNoteList();
        setTimeout(fitView, 200);
        setStatus(
          `Импорт: ${data.projects || 0} проектов, ${data.cards || 0} задач`
        );
      } catch (err) {
        setStatus(err.message || "Ошибка импорта");
      } finally {
        importBtn.disabled = false;
      }
    });
  }
  document.getElementById("kg-fit").addEventListener("click", () => {
    fitView();
  });
  document.getElementById("kg-reheat").addEventListener("click", () => {
    rebuildSimulation(false);
    sim.alpha = 1;
    setTimeout(fitView, 400);
  });
  els.showUnresolved.addEventListener("change", () => rebuildSimulation(true));
  els.search.addEventListener("input", renderNoteList);
  els.title.addEventListener("input", scheduleSave);
  els.body.addEventListener("input", scheduleSave);
  els.title.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      if (mode === "read") setEditorMode("edit");
      els.body.focus();
    }
  });

  window.addEventListener("beforeunload", () => {
    if (dirty) {
      navigator.sendBeacon?.(
        noteUrl(selectedId, "update"),
        new Blob([JSON.stringify({ title: els.title.value, body: els.body.value })], {
          type: "application/json",
        })
      );
    }
  });

  rebuildSimulation(false);
  renderNoteList();
  requestAnimationFrame(draw);
  setTimeout(fitView, 50);
  window.addEventListener("resize", () => {
    /* canvas resizes each frame */
  });
})();
