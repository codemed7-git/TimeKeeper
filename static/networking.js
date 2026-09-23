(function () {
  function openModal(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.hidden = false;
    document.body.classList.add("modal-open");
  }
  function closeModal(el) {
    const modal = el.closest ? el.closest(".modal") : el;
    if (!modal) return;
    modal.hidden = true;
    if (!document.querySelector(".modal:not([hidden])")) {
      document.body.classList.remove("modal-open");
    }
  }
  document.querySelectorAll("[data-open-modal]").forEach((btn) => {
    btn.addEventListener("click", () => openModal(btn.dataset.openModal));
  });
  document.querySelectorAll("[data-close-modal]").forEach((el) => {
    el.addEventListener("click", () => closeModal(el));
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      document.querySelectorAll(".modal:not([hidden])").forEach((m) => {
        m.hidden = true;
      });
      document.body.classList.remove("modal-open");
    }
  });

  document.querySelectorAll(".js-nw-reveal").forEach((btn) => {
    btn.addEventListener("click", () => {
      const wrap = btn.closest(".nw-secret-wrap");
      if (!wrap) return;
      const span = wrap.querySelector(".nw-secret");
      const hidden = wrap.parentElement && wrap.parentElement.querySelector("textarea[hidden]");
      if (span) span.textContent = wrap.dataset.secret || "";
      if (hidden) hidden.hidden = false;
      btn.remove();
    });
  });

  const hints = window.NW_HINTS || {};
  const talk = document.getElementById("talk-type");
  const talkHint = document.getElementById("talk-hint");
  if (talk && talkHint) {
    talk.addEventListener("change", () => {
      talkHint.textContent = (hints.talk || {})[talk.value] || "";
    });
  }
  const openSel = document.getElementById("openness-model");
  const openHint = document.getElementById("openness-hint");
  if (openSel && openHint) {
    openSel.addEventListener("change", () => {
      openHint.textContent = (hints.openness || {})[openSel.value] || "";
    });
  }
  const dec = document.getElementById("decision-style");
  const decHint = document.getElementById("decision-hint");
  if (dec && decHint && window.NW_ORG && window.NW_ORG.hints) {
    dec.addEventListener("change", () => {
      decHint.textContent = window.NW_ORG.hints[dec.value] || "";
    });
  }

  function sparkline(svg, values) {
    if (!svg) return;
    const w = 120;
    const h = 32;
    const pts = values.length ? values : [0];
    const max = 3;
    const step = pts.length > 1 ? w / (pts.length - 1) : w;
    const d = pts
      .map((v, i) => {
        const x = pts.length > 1 ? i * step : w / 2;
        const y = h - 4 - (Number(v) / max) * (h - 8);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
    svg.innerHTML = `<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="${d}"></polyline>`;
  }
  const sparkRoot = document.querySelector(".nw-sparks");
  if (sparkRoot) {
    let scores = [];
    try {
      scores = JSON.parse(sparkRoot.dataset.scores || "[]");
    } catch (_) {
      scores = [];
    }
    sparkRoot.querySelectorAll(".nw-spark__svg").forEach((svg) => {
      const key = svg.dataset.key;
      sparkline(
        svg,
        scores.map((s) => s[key] || 0)
      );
    });
  }

  const ois = document.getElementById("form-ois");
  if (ois && window.NW_OIS) {
    ois.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(ois);
      const id = fd.get("contact_id");
      if (!id) return;
      const url = String(window.NW_OIS).replace("/0/", `/${id}/`);
      const res = await fetch(url, {
        method: "POST",
        body: fd,
        headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" },
      });
      const data = await res.json().catch(() => ({}));
      if (data.decision === "drop") {
        if (confirm("Отпустить: архивировать контакт?")) {
          await fetch(`/networking/contacts/${id}/archive`, {
            method: "POST",
            headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest" },
          });
        }
      }
      window.location.href = `/networking/contacts/${id}`;
    });
  }

  const conf = document.getElementById("nw-export-conf");
  function withConf(a) {
    if (!a) return;
    const base = a.getAttribute("href").split("?")[0];
    a.setAttribute("href", conf && conf.checked ? base + "?confidential=1" : base);
  }
  if (conf) {
    const csv = document.getElementById("nw-csv");
    const md = document.getElementById("nw-md");
    conf.addEventListener("change", () => {
      withConf(csv);
      withConf(md);
    });
  }

  const importForm = document.getElementById("nw-import-form");
  if (importForm && window.NW_IMPORT) {
    let preview = null;
    importForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(importForm);
      const res = await fetch(window.NW_IMPORT.preview, { method: "POST", body: fd, headers: { Accept: "application/json" } });
      preview = await res.json();
      const box = document.getElementById("nw-import-preview");
      const mapEl = document.getElementById("nw-import-map");
      const tableEl = document.getElementById("nw-import-table");
      if (!preview.ok) {
        alert(preview.error || "Ошибка");
        return;
      }
      box.hidden = false;
      if (preview.kind === "csv") {
        const fields = ["", ...preview.fields];
        mapEl.innerHTML = (preview.headers || [])
          .map((h) => {
            const cur = (preview.mapping || {})[h] || "";
            const opts = fields
              .map((f) => `<option value="${f}"${f === cur ? " selected" : ""}>${f || "— пропустить"}</option>`)
              .join("");
            return `<label class="field">${h}<select class="input js-map" data-h="${h}">${opts}</select></label>`;
          })
          .join("");
      } else {
        mapEl.innerHTML = "<p class='muted'>vCard: фиксированный маппинг FN, N, TEL, EMAIL, ORG, TITLE, BDAY, ADR, NOTE, URL.</p>";
      }
      const rows = preview.rows || [];
      tableEl.innerHTML =
        "<table class='nw-table'><thead><tr><th>#</th><th>Имя</th><th>При дубле</th></tr></thead><tbody>" +
        rows
          .slice(0, 80)
          .map((r, i) => {
            const name = r.display_name || r.FN || Object.values(r)[0] || "";
            return `<tr><td>${i + 1}</td><td>${name}</td><td>
              <select class="input js-dec" data-i="${i}">
                <option value="create">Создать / дубль</option>
                <option value="update">Обновить пустые</option>
                <option value="skip">Пропустить</option>
              </select></td></tr>`;
          })
          .join("") +
        "</tbody></table>";
    });
    const commitBtn = document.getElementById("nw-import-commit");
    if (commitBtn) {
      commitBtn.addEventListener("click", async () => {
        if (!preview) return;
        const mapping = {};
        document.querySelectorAll(".js-map").forEach((sel) => {
          mapping[sel.dataset.h] = sel.value;
        });
        const decisions = {};
        document.querySelectorAll(".js-dec").forEach((sel) => {
          decisions[sel.dataset.i] = sel.value;
        });
        const res = await fetch(window.NW_IMPORT.commit, {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ kind: preview.kind, mapping, decisions }),
        });
        const data = await res.json();
        const out = document.getElementById("nw-import-result");
        if (out) {
          out.textContent = data.ok
            ? `Создано ${data.created}, обновлено ${data.updated}, пропущено ${data.skipped}`
            : data.error || "Ошибка";
        }
      });
    }
  }

  const ring = document.getElementById("nw-goal-ring");
  if (ring) {
    let sectors = [];
    try {
      sectors = JSON.parse(ring.dataset.sectors || "[]");
    } catch (_) {
      sectors = [];
    }
    const n = Math.max(sectors.length, 1);
    const size = 360;
    const cx = size / 2;
    const cy = size / 2;
    const rings = [70, 120, 170];
    let svg = `<svg viewBox="0 0 ${size} ${size}" width="100%" role="img">`;
    rings.forEach((r) => {
      svg += `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="currentColor" opacity="0.25"/>`;
    });
    sectors.forEach((s, i) => {
      const a0 = (i / n) * Math.PI * 2 - Math.PI / 2;
      const a1 = ((i + 1) / n) * Math.PI * 2 - Math.PI / 2;
      const x0 = cx + Math.cos(a0) * 170;
      const y0 = cy + Math.sin(a0) * 170;
      svg += `<line x1="${cx}" y1="${cy}" x2="${x0}" y2="${y0}" stroke="currentColor" opacity="0.2"/>`;
      const am = (a0 + a1) / 2;
      const lx = cx + Math.cos(am) * 188;
      const ly = cy + Math.sin(am) * 188;
      svg += `<text x="${lx}" y="${ly}" text-anchor="middle" font-size="11" fill="currentColor">${escapeHtml(s.name || "")}</text>`;
      (s.goals || []).forEach((g, gi) => {
        const t = a0 + ((gi + 1) / ((s.goals.length || 1) + 1)) * (a1 - a0);
        const gx = cx + Math.cos(t) * 70;
        const gy = cy + Math.sin(t) * 70;
        svg += `<text x="${gx}" y="${gy}" text-anchor="middle" font-size="10" fill="currentColor">${escapeHtml((g.title || "").slice(0, 18))}</text>`;
        (g.links || []).forEach((l, li) => {
          const r = 120 + (li % 2) * 20;
          const lx2 = cx + Math.cos(t) * r;
          const ly2 = cy + Math.sin(t) * r;
          const who = l.display_name || l.external_name || "";
          svg += `<text x="${lx2}" y="${ly2}" text-anchor="middle" font-size="9" opacity="0.85" fill="currentColor">${escapeHtml(who.slice(0, 16))}</text>`;
        });
      });
    });
    svg += `<text x="${cx}" y="${cy + 4}" text-anchor="middle" font-size="12" fill="currentColor">Что / Кто / Как</text></svg>`;
    ring.innerHTML = svg;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }
})();
