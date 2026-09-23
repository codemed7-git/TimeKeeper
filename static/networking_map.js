(function () {
  const canvas = document.getElementById("nw-map-canvas");
  if (!canvas || !window.NW_MAP) return;
  const ctx = canvas.getContext("2d");
  const cfg = window.NW_MAP;
  const side = document.getElementById("nw-map-side");
  const metricsEl = document.getElementById("nw-map-metrics");

  const CIRCLE_R = { support: 0.32, productivity: 0.58, development: 0.84 };
  const CIRCLE_LABEL = cfg.circles || {};

  let data = { nodes: [], edges: [], sectors: [], groups: [], circles: {}, me: { label: "Я" }, density: 0 };
  let nodes = [];
  let mode = "radial";
  let panX = 0;
  let panY = 0;
  let zoom = 1;
  let dragging = null;
  let panning = null;
  let selected = null;
  let hover = null;
  let showEdges = true;
  let raf = 0;

  function theme() {
    const s = getComputedStyle(document.documentElement);
    return {
      text: s.getPropertyValue("--text").trim() || "#e8eaef",
      muted: s.getPropertyValue("--muted").trim() || "#8b93a7",
      accent: s.getPropertyValue("--accent").trim() || "#5b8cff",
      border: s.getPropertyValue("--border").trim() || "#2a3142",
      danger: s.getPropertyValue("--danger").trim() || "#e45858",
      success: s.getPropertyValue("--success").trim() || "#7dca7d",
      card: s.getPropertyValue("--card").trim() || "#171b24",
    };
  }

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.floor(rect.width * dpr));
    canvas.height = Math.max(1, Math.floor(rect.height * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function radius() {
    const rect = canvas.getBoundingClientRect();
    return (Math.min(rect.width, rect.height) * 0.42 * zoom);
  }

  function toScreen(x, y) {
    const rect = canvas.getBoundingClientRect();
    const r = radius();
    return { sx: rect.width / 2 + panX + x * r, sy: rect.height / 2 + panY + y * r };
  }

  function toNorm(sx, sy) {
    const rect = canvas.getBoundingClientRect();
    const r = radius() || 1;
    return { x: (sx - rect.width / 2 - panX) / r, y: (sy - rect.height / 2 - panY) / r };
  }

  function filters() {
    return {
      circle: (document.getElementById("nw-filter-circle") || {}).value || "",
      role: (document.getElementById("nw-filter-role") || {}).value || "",
      group: (document.getElementById("nw-filter-group") || {}).value || "",
      tag: ((document.getElementById("nw-filter-tag") || {}).value || "").trim().toLowerCase(),
      problem: (document.getElementById("nw-filter-problem") || {}).checked,
      overdue: (document.getElementById("nw-filter-overdue") || {}).checked,
    };
  }

  function visibleNodes() {
    const f = filters();
    const today = new Date().toISOString().slice(0, 10);
    return nodes.filter((n) => {
      if (n.id === "me") return true;
      if (f.circle && n.circle !== f.circle) return false;
      if (f.role && !(n.roles || []).includes(f.role)) return false;
      if (f.group) {
        const g = (data.groups || []).find((x) => String(x.id) === String(f.group));
        if (g && n.group_color !== g.color) return false;
      }
      if (f.problem && !n.has_problem) return false;
      if (f.overdue) {
        if (!n.next_contact_due || n.next_contact_due >= today) return false;
      }
      return true;
    });
  }

  function sectorIndex(node, sectors) {
    if (!sectors.length) return 0;
    const i = sectors.findIndex((s) => s.id === node.sector_id);
    return i >= 0 ? i : sectors.length;
  }

  function radialTarget(node, all, sectors) {
    if (node.id === "me") return { x: 0, y: 0 };
    const nSec = Math.max(sectors.length, 0) + 1;
    const si = sectorIndex(node, sectors);
    const a0 = (si / nSec) * Math.PI * 2 - Math.PI / 2;
    const a1 = ((si + 1) / nSec) * Math.PI * 2 - Math.PI / 2;
    const same = all.filter((o) => o.id !== "me" && o.circle === node.circle && sectorIndex(o, sectors) === si);
    const idx = Math.max(0, same.findIndex((o) => o.id === node.id));
    const t = (idx + 1) / (same.length + 1);
    const ang = a0 + t * (a1 - a0);
    const jitter = ((idx % 5) - 2) * 0.012;
    const rr = (CIRCLE_R[node.circle] || 0.58) + jitter;
    return { x: Math.cos(ang) * rr, y: Math.sin(ang) * rr };
  }

  function applyLayout() {
    const sectors = data.sectors || [];
    nodes.forEach((n) => {
      const t = radialTarget(n, nodes, sectors);
      n.tx = t.x;
      n.ty = t.y;
      if (n.pinned && n.pos) {
        n.x = n.pos.x;
        n.y = n.pos.y;
      } else if (mode === "radial") {
        n.x = t.x;
        n.y = t.y;
      } else if (n.x == null) {
        n.x = (Math.random() - 0.5) * 0.8;
        n.y = (Math.random() - 0.5) * 0.8;
      }
    });
  }

  function tick() {
    const vis = visibleNodes();
    const visSet = new Set(vis.map((n) => n.id));
    if (mode === "radial") {
      vis.forEach((n) => {
        if (n.pinned || n.id === "me") {
          if (n.id === "me") {
            n.x = 0;
            n.y = 0;
          }
          return;
        }
        n.x += ((n.tx || 0) - n.x) * 0.08;
        n.y += ((n.ty || 0) - n.y) * 0.08;
      });
      for (let i = 0; i < vis.length; i++) {
        for (let j = i + 1; j < vis.length; j++) {
          const a = vis[i];
          const b = vis[j];
          if (a.id === "me" || b.id === "me") continue;
          let dx = a.x - b.x;
          let dy = a.y - b.y;
          let d2 = dx * dx + dy * dy || 0.0001;
          if (d2 < 0.012) {
            const f = 0.0008 / d2;
            if (!a.pinned) {
              a.x += dx * f;
              a.y += dy * f;
            }
            if (!b.pinned) {
              b.x -= dx * f;
              b.y -= dy * f;
            }
          }
        }
      }
    } else {
      vis.forEach((n) => {
        if (n.pinned || n.id === "me") {
          if (n.id === "me") {
            n.x = 0;
            n.y = 0;
          }
          n.vx = 0;
          n.vy = 0;
          return;
        }
        n.vx = (n.vx || 0) * 0.82;
        n.vy = (n.vy || 0) * 0.82;
        n.vx += -n.x * 0.01;
        n.vy += -n.y * 0.01;
      });
      for (let i = 0; i < vis.length; i++) {
        for (let j = i + 1; j < vis.length; j++) {
          const a = vis[i];
          const b = vis[j];
          let dx = a.x - b.x;
          let dy = a.y - b.y;
          let d2 = dx * dx + dy * dy || 0.0004;
          const f = 0.002 / d2;
          if (!a.pinned && a.id !== "me") {
            a.vx += dx * f;
            a.vy += dy * f;
          }
          if (!b.pinned && b.id !== "me") {
            b.vx -= dx * f;
            b.vy -= dy * f;
          }
        }
      }
      vis.forEach((n) => {
        if (n.id === "me" || n.pinned) return;
        const me = nodes.find((x) => x.id === "me");
        if (me) {
          const dx = me.x - n.x;
          const dy = me.y - n.y;
          n.vx += dx * 0.015;
          n.vy += dy * 0.015;
        }
      });
      (data.edges || []).forEach((e) => {
        if (!visSet.has(e.a) || !visSet.has(e.b)) return;
        const a = nodes.find((n) => n.id === e.a);
        const b = nodes.find((n) => n.id === e.b);
        if (!a || !b) return;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        if (!a.pinned && a.id !== "me") {
          a.vx += dx * 0.02;
          a.vy += dy * 0.02;
        }
        if (!b.pinned && b.id !== "me") {
          b.vx -= dx * 0.02;
          b.vy -= dy * 0.02;
        }
      });
      vis.forEach((n) => {
        if (n.pinned || n.id === "me") return;
        n.x += n.vx || 0;
        n.y += n.vy || 0;
      });
    }
  }

  function nodeColor(n, t) {
    if (n.id === "me") return t.accent;
    if (n.group_color) return n.group_color;
    if (n.circle === "support") return "#7ec8a3";
    if (n.circle === "productivity") return t.accent;
    return "#c9a06a";
  }

  function drawArrow(x1, y1, x2, y2, color) {
    const ang = Math.atan2(y2 - y1, x2 - x1);
    ctx.beginPath();
    ctx.moveTo(x2, y2);
    ctx.lineTo(x2 - 8 * Math.cos(ang - 0.4), y2 - 8 * Math.sin(ang - 0.4));
    ctx.lineTo(x2 - 8 * Math.cos(ang + 0.4), y2 - 8 * Math.sin(ang + 0.4));
    ctx.closePath();
    ctx.fillStyle = color;
    ctx.fill();
  }

  function render() {
    const rect = canvas.getBoundingClientRect();
    const t = theme();
    ctx.clearRect(0, 0, rect.width, rect.height);
    const vis = visibleNodes();
    const visIds = new Set(vis.map((n) => n.id));
    const c0 = toScreen(0, 0);

    if (mode === "radial") {
      ["support", "productivity", "development"].forEach((key) => {
        const p = toScreen(CIRCLE_R[key], 0);
        const rr = Math.hypot(p.sx - c0.sx, 0);
        ctx.beginPath();
        ctx.arc(c0.sx, c0.sy, rr, 0, Math.PI * 2);
        ctx.strokeStyle = t.border;
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.fillStyle = t.muted;
        ctx.font = "12px Segoe UI, sans-serif";
        ctx.fillText(CIRCLE_LABEL[key] || key, c0.sx + 6, c0.sy - rr + 12);
      });
      const sectors = data.sectors || [];
      const nSec = sectors.length + 1;
      for (let i = 0; i < nSec; i++) {
        const ang = (i / nSec) * Math.PI * 2 - Math.PI / 2;
        const p = toScreen(Math.cos(ang) * 0.95, Math.sin(ang) * 0.95);
        ctx.beginPath();
        ctx.moveTo(c0.sx, c0.sy);
        ctx.lineTo(p.sx, p.sy);
        ctx.strokeStyle = t.border;
        ctx.globalAlpha = 0.45;
        ctx.stroke();
        ctx.globalAlpha = 1;
        const mid = ((i + 0.5) / nSec) * Math.PI * 2 - Math.PI / 2;
        const lp = toScreen(Math.cos(mid) * 0.98, Math.sin(mid) * 0.98);
        const name = i < sectors.length ? sectors[i].name : "Прочее";
        ctx.fillStyle = t.muted;
        ctx.font = "11px Segoe UI, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(name, lp.sx, lp.sy);
        ctx.textAlign = "start";
      }
    }

    vis.forEach((n) => {
      if (n.id === "me") return;
      const a = c0;
      const b = toScreen(n.x, n.y);
      ctx.beginPath();
      ctx.moveTo(a.sx, a.sy);
      ctx.lineTo(b.sx, b.sy);
      ctx.strokeStyle = n.has_problem ? t.danger : t.border;
      ctx.lineWidth = n.stage === "trusted" || n.stage === "stable" ? 1.6 : 1;
      if (n.stage === "new" || n.stage === "stabilizing") ctx.setLineDash([4, 4]);
      else ctx.setLineDash([]);
      ctx.stroke();
      ctx.setLineDash([]);
      const ib = n.initiative_balance || 0;
      if (ib > 0) drawArrow(b.sx, b.sy, a.sx, a.sy, t.muted);
      else if (ib < 0) drawArrow(a.sx, a.sy, b.sx, b.sy, t.muted);
      else {
        drawArrow(a.sx, a.sy, (a.sx + b.sx) / 2, (a.sy + b.sy) / 2, t.muted);
        drawArrow(b.sx, b.sy, (a.sx + b.sx) / 2, (a.sy + b.sy) / 2, t.muted);
      }
    });

    if (showEdges) {
      (data.edges || []).forEach((e) => {
        if (!visIds.has(e.a) || !visIds.has(e.b)) return;
        const a = nodes.find((n) => n.id === e.a);
        const b = nodes.find((n) => n.id === e.b);
        if (!a || !b) return;
        const pa = toScreen(a.x, a.y);
        const pb = toScreen(b.x, b.y);
        ctx.beginPath();
        ctx.moveTo(pa.sx, pa.sy);
        ctx.lineTo(pb.sx, pb.sy);
        ctx.strokeStyle = e.quality === "problem" ? t.danger : e.quality === "positive" ? t.success : t.muted;
        ctx.lineWidth = e.strength === "intense" ? 2.4 : e.strength === "sporadic" ? 0.8 : 1.3;
        if (e.strength === "sporadic") ctx.setLineDash([3, 5]);
        ctx.stroke();
        ctx.setLineDash([]);
        if (e.direction === "a_to_b") drawArrow(pa.sx, pa.sy, pb.sx, pb.sy, ctx.strokeStyle);
        if (e.direction === "b_to_a") drawArrow(pb.sx, pb.sy, pa.sx, pa.sy, ctx.strokeStyle);
        if (e.direction === "mutual") {
          drawArrow(pa.sx, pa.sy, (pa.sx + pb.sx) / 2, (pa.sy + pb.sy) / 2, ctx.strokeStyle);
          drawArrow(pb.sx, pb.sy, (pa.sx + pb.sx) / 2, (pa.sy + pb.sy) / 2, ctx.strokeStyle);
        }
      });
    }

    vis.forEach((n) => {
      const p = toScreen(n.x, n.y);
      const size = n.id === "me" ? 14 : 5 + (n.importance || 2) * 1.6 + (n.is_key ? 3 : 0);
      ctx.beginPath();
      ctx.arc(p.sx, p.sy, size, 0, Math.PI * 2);
      ctx.fillStyle = nodeColor(n, t);
      ctx.fill();
      if (n.is_key || n.id === "me" || selected === n.id) {
        ctx.strokeStyle = n.has_problem ? t.danger : t.text;
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      ctx.fillStyle = t.text;
      ctx.font = n.id === "me" ? "13px Segoe UI, sans-serif" : "11px Segoe UI, sans-serif";
      ctx.fillText(n.name || n.label || "", p.sx + size + 4, p.sy + 4);
    });

    updateMetrics(vis);
  }

  function updateMetrics(vis) {
    if (!metricsEl) return;
    const people = vis.filter((n) => n.id !== "me");
    const counts = { support: 0, productivity: 0, development: 0 };
    people.forEach((n) => {
      if (counts[n.circle] != null) counts[n.circle] += 1;
    });
    const ids = new Set(people.map((n) => n.id));
    const e = (data.edges || []).filter((x) => ids.has(x.a) && ids.has(x.b)).length;
    const n = people.length;
    const dens = n > 1 ? (2 * e / (n * (n - 1))).toFixed(3) : "0.000";
    const bridges = people.filter((p) => (p.roles || []).includes("bridge")).length;
    const conn = people.filter((p) => (p.roles || []).includes("connector")).length;
    const cond = people.filter((p) => (p.roles || []).includes("condenser")).length;
    const tgt = data.circles || {};
    metricsEl.innerHTML = `
      <span>Круг поддержки: ${counts.support} / ${(tgt.support && tgt.support.target) || 5}</span>
      <span>Круг продуктивности: ${counts.productivity} / ${(tgt.productivity && tgt.productivity.target) || 75}</span>
      <span>Круг развития: ${counts.development} / ${(tgt.development && tgt.development.target) || 100}</span>
      <span>Плотность: ${dens}</span>
      <span>Мостов: ${bridges}</span>
      <span>Коннекторов: ${conn}</span>
      <span>Конденсаторов: ${cond}</span>`;
  }

  function hit(sx, sy) {
    const vis = visibleNodes();
    for (let i = vis.length - 1; i >= 0; i--) {
      const n = vis[i];
      const p = toScreen(n.x, n.y);
      const size = n.id === "me" ? 16 : 8 + (n.importance || 2) * 1.6;
      if (Math.hypot(sx - p.sx, sy - p.sy) <= size + 4) return n;
    }
    return null;
  }

  function showSide(n) {
    if (!side) return;
    if (!n || n.id === "me") {
      side.innerHTML = "<p class='muted'>Клик по узлу — мини-досье. Двойной клик по фону — новый контакт.</p>";
      return;
    }
    const url = String(cfg.contactUrl).replace("/0", "/" + n.id);
    const touch = String(cfg.touchUrl).replace("/0/", "/" + n.id + "/");
    side.innerHTML = `
      <h2 class="card-title">${n.name}</h2>
      <p>${(cfg.circles || {})[n.circle] || n.circle} · ${(cfg.stages || {})[n.stage] || n.stage}</p>
      <p>Роли: ${(n.roles || []).map((r) => (cfg.roles || {})[r] || r).join(", ") || "—"}</p>
      <p>Опор: ${n.legs || 0}/3</p>
      <p>Последнее касание: ${n.last_contact_at || "—"}</p>
      <p>Пора связаться: ${n.next_contact_due || "—"}</p>
      <div class="nw-row-actions">
        <a class="btn btn--sm btn--primary" href="${url}">Открыть карточку</a>
        <form method="post" action="${touch}" class="inline-form">
          <input type="hidden" name="channel" value="message">
          <input type="hidden" name="summary" value="Касание с карты">
          <button class="btn btn--sm btn--ghost">Добавить касание</button>
        </form>
        <a class="btn btn--sm btn--ghost" href="/networking/meetings?contact_id=${n.id}">Запланировать встречу</a>
      </div>`;
  }

  let saveTimer = null;
  function savePos(n) {
    if (n.id === "me") return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      const body = new URLSearchParams({
        contact_id: String(n.id),
        x: String(n.x),
        y: String(n.y),
        mode: mode,
      });
      fetch(cfg.posUrl, {
        method: "POST",
        headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" },
        body,
      });
    }, 280);
  }

  function loop() {
    tick();
    render();
    raf = requestAnimationFrame(loop);
  }

  async function load() {
    const res = await fetch(cfg.dataUrl, { headers: { Accept: "application/json" } });
    data = await res.json();
    nodes = (data.nodes || []).map((n) => ({
      ...n,
      pinned: !!(n.pos && n.pos.pinned),
      x: n.pos ? n.pos.x : null,
      y: n.pos ? n.pos.y : null,
    }));
    nodes.unshift({ id: "me", name: (data.me && data.me.label) || "Я", x: 0, y: 0, pinned: true });
    mode = (document.querySelector("input[name=map-mode]:checked") || {}).value || data.mode || "radial";
    applyLayout();
  }

  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    zoom = Math.max(0.4, Math.min(3, zoom * (e.deltaY > 0 ? 0.92 : 1.08)));
  }, { passive: false });

  canvas.addEventListener("pointerdown", (e) => {
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;
    const n = hit(sx, sy);
    if (n && n.id !== "me") {
      dragging = { n, dx: 0, dy: 0 };
      canvas.setPointerCapture(e.pointerId);
    } else {
      panning = { x: e.clientX - panX, y: e.clientY - panY };
      canvas.setPointerCapture(e.pointerId);
    }
  });
  canvas.addEventListener("pointermove", (e) => {
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;
    hover = hit(sx, sy);
    canvas.style.cursor = hover ? "pointer" : panning ? "grabbing" : "default";
    if (dragging) {
      const nn = toNorm(sx, sy);
      dragging.n.x = Math.max(-1, Math.min(1, nn.x));
      dragging.n.y = Math.max(-1, Math.min(1, nn.y));
      dragging.n.pinned = true;
      dragging.n.pos = { x: dragging.n.x, y: dragging.n.y, pinned: true };
    } else if (panning) {
      panX = e.clientX - panning.x;
      panY = e.clientY - panning.y;
    }
  });
  canvas.addEventListener("pointerup", (e) => {
    if (dragging) {
      savePos(dragging.n);
      selected = dragging.n.id;
      showSide(dragging.n);
    } else if (!panning) {
      /* click handled below */
    }
    const rect = canvas.getBoundingClientRect();
    const sx = e.clientX - rect.left;
    const sy = e.clientY - rect.top;
    if (!dragging) {
      const n = hit(sx, sy);
      selected = n ? n.id : null;
      showSide(n);
    }
    dragging = null;
    panning = null;
  });
  canvas.addEventListener("dblclick", (e) => {
    const rect = canvas.getBoundingClientRect();
    const n = hit(e.clientX - rect.left, e.clientY - rect.top);
    if (!n) {
      const modal = document.getElementById("modal-nw-contact");
      if (modal) {
        modal.hidden = false;
        document.body.classList.add("modal-open");
      }
    }
  });

  document.querySelectorAll("input[name=map-mode]").forEach((el) => {
    el.addEventListener("change", () => {
      mode = el.value;
      applyLayout();
      fetch(cfg.modeUrl, {
        method: "POST",
        headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" },
        body: new URLSearchParams({ mode }),
      });
    });
  });
  const edgesCb = document.getElementById("nw-show-edges");
  if (edgesCb) edgesCb.addEventListener("change", () => { showEdges = edgesCb.checked; });

  const pngBtn = document.getElementById("nw-export-png");
  if (pngBtn) {
    pngBtn.addEventListener("click", () => {
      const scale = 2;
      const off = document.createElement("canvas");
      off.width = canvas.width * scale / (window.devicePixelRatio || 1);
      off.height = canvas.height * scale / (window.devicePixelRatio || 1);
      const octx = off.getContext("2d");
      octx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim() || "#0f1218";
      octx.fillRect(0, 0, off.width, off.height);
      octx.drawImage(canvas, 0, 0, off.width, off.height);
      off.toBlob((blob) => {
        if (!blob) return;
        const a = document.createElement("a");
        const d = new Date();
        const ds = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
        a.href = URL.createObjectURL(blob);
        a.download = `сеть-${ds}.png`;
        a.click();
        URL.revokeObjectURL(a.href);
      });
    });
  }

  window.addEventListener("resize", resize);
  resize();
  load().then(() => loop());
})();
