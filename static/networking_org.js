(function () {
  const canvas = document.getElementById("nw-org-canvas");
  if (!canvas || !window.NW_ORG) return;
  const ctx = canvas.getContext("2d");
  const RING_R = { core: 0.28, secondary: 0.55, outer: 0.82 };
  let data = { members: [], units: [], edges: [] };
  let nodes = [];
  let panX = 0;
  let panY = 0;
  let zoom = 1;
  let dragging = null;
  let panning = null;

  function theme() {
    const s = getComputedStyle(document.documentElement);
    return {
      text: s.getPropertyValue("--text").trim() || "#e8eaef",
      muted: s.getPropertyValue("--muted").trim() || "#8b93a7",
      accent: s.getPropertyValue("--accent").trim() || "#5b8cff",
      border: s.getPropertyValue("--border").trim() || "#2a3142",
      danger: s.getPropertyValue("--danger").trim() || "#e45858",
      warn: s.getPropertyValue("--warn").trim() || "#e6a23c",
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
    return Math.min(rect.width, rect.height) * 0.42 * zoom;
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

  function layout() {
    const units = data.units || [];
    const nSec = Math.max(units.length, 1);
    nodes.forEach((n, idx) => {
      const ui = Math.max(0, units.findIndex((u) => u.id === n.unit_id));
      const same = nodes.filter((o) => (o.unit_id || 0) === (n.unit_id || 0) && o.ring === n.ring);
      const i = Math.max(0, same.findIndex((o) => o.id === n.id));
      const a0 = (ui / nSec) * Math.PI * 2 - Math.PI / 2;
      const a1 = ((ui + 1) / nSec) * Math.PI * 2 - Math.PI / 2;
      const t = (i + 1) / (same.length + 1);
      const ang = a0 + t * (a1 - a0);
      const rr = RING_R[n.ring] || 0.55;
      n.tx = Math.cos(ang) * rr;
      n.ty = Math.sin(ang) * rr;
      if (n.pinned) {
        n.x = n.x || 0;
        n.y = n.y || 0;
      } else {
        n.x = n.tx;
        n.y = n.ty;
      }
    });
  }

  function tick() {
    nodes.forEach((n) => {
      if (n.pinned) return;
      n.x += (n.tx - n.x) * 0.08;
      n.y += (n.ty - n.y) * 0.08;
    });
  }

  function render() {
    const rect = canvas.getBoundingClientRect();
    const t = theme();
    ctx.clearRect(0, 0, rect.width, rect.height);
    const c0 = toScreen(0, 0);
    ["core", "secondary", "outer"].forEach((key) => {
      const p = toScreen(RING_R[key], 0);
      ctx.beginPath();
      ctx.arc(c0.sx, c0.sy, Math.abs(p.sx - c0.sx), 0, Math.PI * 2);
      ctx.strokeStyle = t.border;
      ctx.stroke();
    });
    const labels = { core: "Ядро власти", secondary: "Второй круг", outer: "Периферия" };
    ctx.fillStyle = t.muted;
    ctx.font = "12px Segoe UI, sans-serif";
    ctx.fillText(labels.core, c0.sx + 8, c0.sy - Math.abs(toScreen(RING_R.core, 0).sx - c0.sx) + 12);
    (data.units || []).forEach((u, i, arr) => {
      const nSec = arr.length || 1;
      const ang = ((i + 0.5) / nSec) * Math.PI * 2 - Math.PI / 2;
      const lp = toScreen(Math.cos(ang) * 0.95, Math.sin(ang) * 0.95);
      ctx.fillStyle = t.muted;
      ctx.textAlign = "center";
      ctx.fillText(u.name, lp.sx, lp.sy);
      ctx.textAlign = "start";
    });
    (data.edges || []).forEach((e) => {
      const a = nodes.find((n) => n.id === e.a);
      const b = nodes.find((n) => n.id === e.b);
      if (!a || !b) return;
      const pa = toScreen(a.x, a.y);
      const pb = toScreen(b.x, b.y);
      ctx.beginPath();
      ctx.moveTo(pa.sx, pa.sy);
      ctx.lineTo(pb.sx, pb.sy);
      ctx.strokeStyle = e.quality === "problem" ? t.danger : e.kind === "power" ? t.accent : t.muted;
      ctx.lineWidth = e.kind === "power" ? 2.4 : e.kind === "bureaucratic" ? 0.8 : 1.5;
      ctx.stroke();
    });
    nodes.forEach((n) => {
      const p = toScreen(n.x, n.y);
      const size = 6 + (n.power || 1) * 2.5;
      ctx.beginPath();
      ctx.arc(p.sx, p.sy, size, 0, Math.PI * 2);
      ctx.fillStyle = n.color || t.accent;
      ctx.fill();
      if (n.is_candidate) {
        ctx.strokeStyle = t.warn;
        ctx.lineWidth = 3;
        ctx.stroke();
      } else if (n.is_visionary || n.is_rising_star) {
        ctx.strokeStyle = t.text;
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      ctx.fillStyle = t.text;
      ctx.font = "11px Segoe UI, sans-serif";
      ctx.fillText(n.name, p.sx + size + 4, p.sy + 4);
    });
  }

  function hit(sx, sy) {
    for (let i = nodes.length - 1; i >= 0; i--) {
      const n = nodes[i];
      const p = toScreen(n.x, n.y);
      const size = 8 + (n.power || 1) * 2.5;
      if (Math.hypot(sx - p.sx, sy - p.sy) <= size + 4) return n;
    }
    return null;
  }

  function loop() {
    tick();
    render();
    requestAnimationFrame(loop);
  }

  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    zoom = Math.max(0.4, Math.min(3, zoom * (e.deltaY > 0 ? 0.92 : 1.08)));
  }, { passive: false });
  canvas.addEventListener("pointerdown", (e) => {
    const rect = canvas.getBoundingClientRect();
    const n = hit(e.clientX - rect.left, e.clientY - rect.top);
    if (n) {
      dragging = n;
      canvas.setPointerCapture(e.pointerId);
    } else {
      panning = { x: e.clientX - panX, y: e.clientY - panY };
    }
  });
  canvas.addEventListener("pointermove", (e) => {
    const rect = canvas.getBoundingClientRect();
    if (dragging) {
      const nn = toNorm(e.clientX - rect.left, e.clientY - rect.top);
      dragging.x = nn.x;
      dragging.y = nn.y;
      dragging.pinned = true;
    } else if (panning) {
      panX = e.clientX - panning.x;
      panY = e.clientY - panning.y;
    }
  });
  canvas.addEventListener("pointerup", () => {
    if (dragging) {
      const url = String(window.NW_ORG.posUrl).replace("/0", "/" + dragging.id);
      fetch(url, {
        method: "POST",
        headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" },
        body: new URLSearchParams({ x: String(dragging.x), y: String(dragging.y) }),
      });
    }
    dragging = null;
    panning = null;
  });

  window.addEventListener("resize", resize);
  resize();
  fetch(window.NW_ORG.dataUrl, { headers: { Accept: "application/json" } })
    .then((r) => r.json())
    .then((d) => {
      data = d;
      nodes = (d.members || []).map((m) => ({ ...m }));
      layout();
      loop();
    });
})();
