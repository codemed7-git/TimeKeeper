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

  const els = {
    list: document.getElementById("kg-note-list"),
    search: document.getElementById("kg-search"),
    canvas: document.getElementById("kg-canvas"),
    empty: document.getElementById("kg-graph-empty"),
    stats: document.getElementById("kg-graph-stats"),
    editor: document.getElementById("kg-editor"),
    title: document.getElementById("kg-note-title"),
    body: document.getElementById("kg-note-body"),
    status: document.getElementById("kg-save-status"),
    outgoing: document.getElementById("kg-outgoing"),
    incoming: document.getElementById("kg-incoming"),
    showUnresolved: document.getElementById("kg-show-unresolved"),
  };

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
        const create = document.createElement("button");
        create.type = "button";
        create.className = "btn btn--ghost btn--xs";
        create.textContent = "Создать";
        create.addEventListener("click", () => createNote(item.title));
        wrap.appendChild(label);
        wrap.appendChild(create);
        li.appendChild(wrap);
      }
      listEl.appendChild(li);
    });
  }

  function applyNoteToEditor(note) {
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
  }

  async function openNote(id) {
    if (dirty && selectedId && selectedId !== id) {
      await saveNote(true);
    }
    try {
      const note = await api(noteUrl(id, "note"));
      applyNoteToEditor(note);
      const idx = notes.findIndex((n) => n.id === id);
      if (idx >= 0) {
        notes[idx] = {
          ...notes[idx],
          title: note.title,
          body: note.body,
          updated_at: note.updated_at,
          out_count: (note.outgoing || []).length,
          in_count: (note.incoming || []).length,
        };
      }
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
        notes.unshift({
          id: data.note.id,
          title: data.note.title,
          body: data.note.body,
          created_at: data.note.created_at,
          updated_at: data.note.updated_at,
          out_count: (data.note.outgoing || []).length,
          in_count: (data.note.incoming || []).length,
        });
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
        const idx = notes.findIndex((n) => n.id === data.note.id);
        const entry = {
          id: data.note.id,
          title: data.note.title,
          body: data.note.body,
          created_at: data.note.created_at,
          updated_at: data.note.updated_at,
          out_count: (data.note.outgoing || []).length,
          in_count: (data.note.incoming || []).length,
        };
        if (idx >= 0) notes[idx] = entry;
        else notes.unshift(entry);
        applyNoteToEditor(data.note);
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

  function draw() {
    const canvas = els.canvas;
    const ctx = canvas.getContext("2d");
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
      const active =
        !dimming || (neighborIds.has(e.source.id) && neighborIds.has(e.target.id));
      ctx.beginPath();
      ctx.moveTo(e.source.x, e.source.y);
      ctx.lineTo(e.target.x, e.target.y);
      ctx.strokeStyle = active ? "rgba(91, 140, 255, 0.45)" : "rgba(42, 49, 66, 0.35)";
      ctx.lineWidth = (active ? 1.4 : 1) / sim.transform.k;
      ctx.stroke();
    });

    sim.nodes.forEach((n) => {
      const selected = hlId && n.note_id === hlId;
      const active = !dimming || neighborIds.has(n.id);
      const r = n.resolved ? (selected ? 9 : 7) : 5;

      ctx.beginPath();
      ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      if (!n.resolved) {
        ctx.fillStyle = active ? "rgba(139, 147, 167, 0.35)" : "rgba(139, 147, 167, 0.12)";
        ctx.fill();
        ctx.strokeStyle = active ? "rgba(139, 147, 167, 0.9)" : "rgba(139, 147, 167, 0.3)";
        ctx.lineWidth = 1.2 / sim.transform.k;
        ctx.setLineDash([3 / sim.transform.k, 3 / sim.transform.k]);
        ctx.stroke();
        ctx.setLineDash([]);
      } else {
        ctx.fillStyle = selected
          ? "#5b8cff"
          : active
            ? "#3d5fb8"
            : "rgba(61, 95, 184, 0.25)";
        ctx.fill();
        if (selected || (hover && hover.id === n.id)) {
          ctx.strokeStyle = "#e8eaef";
          ctx.lineWidth = 1.5 / sim.transform.k;
          ctx.stroke();
        }
      }

      const showLabel =
        sim.transform.k > 0.55 || selected || (hover && hover.id === n.id) || sim.nodes.length < 18;
      if (showLabel && active) {
        ctx.font = `${12 / sim.transform.k}px "Segoe UI", system-ui, sans-serif`;
        ctx.fillStyle = n.resolved ? "rgba(232, 234, 239, 0.92)" : "rgba(139, 147, 167, 0.85)";
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
    const size = { w: els.canvas.clientWidth, h: els.canvas.clientHeight };
    const p = canvasPos(evt);
    const node = findNodeAt(p.x, p.y, size);
    if (!node) createNote("");
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
