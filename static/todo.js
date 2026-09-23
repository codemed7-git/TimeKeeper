(function () {
  if (!window.TODO_URLS) return;
  const urls = window.TODO_URLS;
  let draggedCard = null;

  function openModal(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.hidden = false;
    document.body.classList.add("modal-open");
    const focusEl = el.querySelector("input, textarea");
    if (focusEl) {
      setTimeout(() => focusEl.focus(), 0);
    }
  }

  function closeModal(el) {
    const modal = el.closest ? el.closest(".modal") : el;
    if (!modal) return;
    modal.hidden = true;
    if (!document.querySelector(".modal:not([hidden])")) {
      document.body.classList.remove("modal-open");
    }
  }

  document.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-open-modal]");
    if (!btn) return;
    const modalId = btn.dataset.openModal;
    if (modalId === "modal-new-card") {
      const columnId = btn.dataset.columnId;
      const form = document.getElementById("form-new-card");
      if (form && columnId) {
        form.action = (urls.createCard || "").replace("__ID__", columnId);
        form.reset();
      }
    }
    openModal(modalId);
  });

  document.addEventListener("click", (event) => {
    const el = event.target.closest("[data-close-modal]");
    if (el) closeModal(el);
  });

  function openEditColumn(column) {
    const form = document.getElementById("form-edit-column");
    const titleInput = document.getElementById("edit-column-title");
    if (!form || !titleInput || !column) return;
    form.action = (urls.updateColumn || "").replace("__ID__", column.dataset.columnId);
    titleInput.value = column.dataset.columnTitle || "";
    openModal("modal-edit-column");
  }

  function setColumnCollapsed(column, collapsed) {
    const btn = column.querySelector(".kanban-collapse-btn");
    const hidden = column.querySelector('.js-collapse-column input[name="collapsed"]');
    column.classList.toggle("is-collapsed", collapsed);
    if (hidden) hidden.value = collapsed ? "0" : "1";
    if (btn) {
      btn.textContent = collapsed ? "›" : "‹";
      btn.title = collapsed ? "Развернуть" : "Свернуть";
      btn.setAttribute("aria-label", collapsed ? "Развернуть колонку" : "Свернуть колонку");
      btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
    }
  }

  /* ---------- Subtask sub-boards (modal with a mini kanban per card) ---------- */

  const subboardModal = document.getElementById("modal-subboard");
  const subboardWrapper = document.getElementById("subboard-kanban-wrapper");
  const subboardTitle = document.getElementById("subboard-card-title");
  let currentSubboardCardId = null;

  function isSubboardContext() {
    return Boolean(subboardModal && !subboardModal.hidden);
  }

  async function refreshSubboard() {
    if (!currentSubboardCardId || !subboardWrapper) return;
    const url = (urls.subboard || "").replace("__ID__", currentSubboardCardId);
    try {
      const res = await fetch(url, { headers: { "X-Requested-With": "fetch" } });
      if (!res.ok) throw new Error("failed");
      subboardWrapper.innerHTML = await res.text();
    } catch (_) {
      subboardWrapper.innerHTML = '<p class="muted">Не удалось загрузить подзадачи.</p>';
    }
  }

  async function openSubboard(cardId, cardTitle) {
    currentSubboardCardId = cardId;
    if (subboardTitle) subboardTitle.textContent = cardTitle || "";
    if (subboardWrapper) subboardWrapper.innerHTML = '<p class="muted">Загрузка…</p>';
    openModal("modal-subboard");
    await refreshSubboard();
  }

  document.addEventListener("click", (event) => {
    const btn = event.target.closest(".js-open-subboard");
    if (!btn) return;
    openSubboard(btn.dataset.cardId, btn.dataset.cardTitle || "");
  });

  const newSubcolumnForm = document.getElementById("form-new-subcolumn");
  if (newSubcolumnForm) {
    newSubcolumnForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!currentSubboardCardId) return;
      const input = newSubcolumnForm.elements.title;
      const title = (input.value || "").trim();
      if (!title) return;
      const url = (urls.createSubcolumn || "").replace("__ID__", currentSubboardCardId);
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: {
            "X-Requested-With": "fetch",
            "Content-Type": "application/x-www-form-urlencoded",
          },
          body: "title=" + encodeURIComponent(title),
        });
        if (!res.ok) throw new Error("failed");
        input.value = "";
        await refreshSubboard();
      } catch (_) {
        window.alert("Не удалось добавить колонку");
      }
    });
  }

  /* ---------- Shared modals: new/edit card, edit column ---------- */
  /* When the subtasks modal is open, submitting these must refresh the
     sub-board in place instead of navigating the whole page away. */

  async function submitViaFetch(form) {
    const res = await fetch(form.action, {
      method: form.method || "POST",
      headers: { "X-Requested-With": "fetch" },
      body: new FormData(form),
    });
    if (!res.ok) throw new Error("failed");
  }

  ["form-new-card", "form-edit-card", "form-edit-column"].forEach((id) => {
    const form = document.getElementById(id);
    if (!form) return;
    form.addEventListener("submit", (event) => {
      if (!isSubboardContext()) return; // let it submit/navigate normally
      event.preventDefault();
      submitViaFetch(form)
        .then(() => {
          closeModal(form);
          refreshSubboard();
        })
        .catch(() => window.alert("Не удалось сохранить"));
    });
  });

  /* ---------- Inline forms duplicated in both the main board and sub-boards ---------- */

  document.addEventListener("click", (event) => {
    const editBtn = event.target.closest(".js-edit-card");
    if (editBtn) {
      const card = editBtn.closest(".kanban-card");
      if (!card) return;
      const form = document.getElementById("form-edit-card");
      const titleInput = document.getElementById("edit-card-title");
      const noteInput = document.getElementById("edit-card-note");
      if (!form || !titleInput || !noteInput) return;
      form.action = (urls.updateCard || "").replace("__ID__", card.dataset.cardId);
      titleInput.value = card.dataset.title || "";
      noteInput.value = card.dataset.note || "";
      openModal("modal-edit-card");
      return;
    }

    const editCol = event.target.closest(".js-edit-column");
    if (editCol) {
      openEditColumn(editCol.closest(".kanban-column"));
    }
  });

  document.addEventListener("submit", (event) => {
    const collapseForm = event.target.closest(".js-collapse-column");
    if (collapseForm) {
      event.preventDefault();
      const column = collapseForm.closest(".kanban-column");
      if (!column) return;
      const nextCollapsed = !column.classList.contains("is-collapsed");
      setColumnCollapsed(column, nextCollapsed);
      const collapseUrl = (urls.collapseColumn || "").replace("__ID__", column.dataset.columnId);
      fetch(collapseUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "fetch",
        },
        body: JSON.stringify({ collapsed: nextCollapsed }),
      }).catch(() => {
        setColumnCollapsed(column, !nextCollapsed);
      });
      return;
    }

    const deleteCardForm = event.target.closest(".js-delete-card-form");
    if (deleteCardForm) {
      if (!isSubboardContext()) return;
      event.preventDefault();
      submitViaFetch(deleteCardForm)
        .then(refreshSubboard)
        .catch(() => window.alert("Не удалось удалить задачу"));
      return;
    }

    const deleteColumnForm = event.target.closest(".kanban-column__delete");
    if (deleteColumnForm) {
      if (!isSubboardContext()) return;
      event.preventDefault();
      submitViaFetch(deleteColumnForm)
        .then(refreshSubboard)
        .catch(() => window.alert("Не удалось удалить колонку"));
    }
  });

  /* ---------- Drag & drop: fully delegated so it works in dynamically
     inserted sub-board content with zero extra wiring ---------- */

  function syncEmptyState(zone) {
    if (!zone) return;
    const hasCards = zone.querySelector(".kanban-card");
    const empty = zone.querySelector(".kanban-empty");
    if (empty) empty.hidden = Boolean(hasCards);
    const column = zone.closest(".kanban-column");
    const countEl = column && column.querySelector(".kanban-column__count");
    if (countEl) {
      countEl.textContent = String(zone.querySelectorAll(".kanban-card").length);
    }
  }

  function hideIndicators() {
    document.querySelectorAll(".kanban-drop-indicator").forEach((el) => el.remove());
    document.querySelectorAll(".kanban-column.drag-over").forEach((el) => {
      el.classList.remove("drag-over");
    });
  }

  function showIndicator(zone, clientY) {
    hideIndicators();
    const cards = Array.from(zone.querySelectorAll(".kanban-card:not(.is-dragging)"));
    let insertBefore = null;
    for (const card of cards) {
      const rect = card.getBoundingClientRect();
      if (clientY < rect.top + rect.height / 2) {
        insertBefore = card;
        break;
      }
    }
    const indicator = document.createElement("div");
    indicator.className = "kanban-drop-indicator";
    const empty = zone.querySelector(".kanban-empty");
    if (insertBefore) {
      zone.insertBefore(indicator, insertBefore);
    } else if (empty) {
      zone.insertBefore(indicator, empty);
    } else {
      zone.appendChild(indicator);
    }
    zone.closest(".kanban-column")?.classList.add("drag-over");
  }

  function insertAt(zone, card, clientY) {
    const cards = Array.from(zone.querySelectorAll(".kanban-card:not(.is-dragging)"));
    let insertBefore = null;
    for (const other of cards) {
      const rect = other.getBoundingClientRect();
      if (clientY < rect.top + rect.height / 2) {
        insertBefore = other;
        break;
      }
    }
    const empty = zone.querySelector(".kanban-empty");
    if (insertBefore) {
      zone.insertBefore(card, insertBefore);
    } else if (empty) {
      zone.insertBefore(card, empty);
    } else {
      zone.appendChild(card);
    }
  }

  function moveCard(cardId, columnId, order) {
    return fetch(urls.moveCard, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ card_id: cardId, column_id: columnId, order }),
    }).then((res) => {
      if (!res.ok) throw new Error("move failed");
      return res.json();
    });
  }

  function dropOnZone(zone, clientY) {
    if (!draggedCard || !zone) return;
    const fromZone = draggedCard.closest("[data-drop-zone]");
    const collapsed = zone.closest(".kanban-column")?.classList.contains("is-collapsed");
    if (collapsed) {
      const empty = zone.querySelector(".kanban-empty");
      if (empty) zone.insertBefore(draggedCard, empty);
      else zone.appendChild(draggedCard);
    } else {
      insertAt(zone, draggedCard, clientY);
    }
    hideIndicators();
    syncEmptyState(fromZone);
    syncEmptyState(zone);

    const column = zone.closest(".kanban-column");
    const columnId = column && column.dataset.columnId;
    const cards = Array.from(zone.querySelectorAll(".kanban-card"));
    const order = cards.indexOf(draggedCard);
    const cardId = draggedCard.dataset.cardId;
    if (!columnId || cardId == null || order < 0) return;

    moveCard(Number(cardId), Number(columnId), order).catch(() => {
      window.location.reload();
    });
  }

  document.addEventListener("dragstart", (event) => {
    const card = event.target.closest(".kanban-card");
    if (!card) return;
    draggedCard = card;
    card.classList.add("is-dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", card.dataset.cardId || "");
  });

  document.addEventListener("dragend", (event) => {
    const card = event.target.closest(".kanban-card");
    if (!card) return;
    card.classList.remove("is-dragging");
    hideIndicators();
    document.querySelectorAll(".kanban-column__cards").forEach(syncEmptyState);
    draggedCard = null;
  });

  document.addEventListener(
    "dragover",
    (event) => {
      const zone = event.target.closest("[data-drop-zone]");
      if (!zone) return;
      event.preventDefault();
      if (!draggedCard) return;
      const column = zone.closest(".kanban-column");
      if (column && column.classList.contains("is-collapsed")) {
        hideIndicators();
        column.classList.add("drag-over");
        return;
      }
      showIndicator(zone, event.clientY);
    },
    { passive: false }
  );

  document.addEventListener("dragleave", (event) => {
    const zone = event.target.closest("[data-drop-zone]");
    if (!zone) return;
    if (!zone.contains(event.relatedTarget)) {
      hideIndicators();
    }
  });

  document.addEventListener("drop", (event) => {
    const zone = event.target.closest("[data-drop-zone]");
    if (!zone) return;
    event.preventDefault();
    dropOnZone(zone, event.clientY);
  });

  document.addEventListener(
    "dragover",
    (event) => {
      const column = event.target.closest(".kanban-column.is-collapsed");
      if (!column) return;
      event.preventDefault();
      if (!draggedCard) return;
      hideIndicators();
      column.classList.add("drag-over");
    },
    { passive: false }
  );

  /* ---------- Highlight a just-created card after the page reload ---------- */

  (function highlightNewCard() {
    const params = new URLSearchParams(window.location.search);
    const newCardId = params.get("new_card");
    if (!newCardId) return;
    params.delete("new_card");
    const qs = params.toString();
    const cleanUrl = window.location.pathname + (qs ? `?${qs}` : "");
    window.history.replaceState({}, "", cleanUrl);
    const card = document.querySelector(`.kanban-card[data-card-id="${newCardId}"]`);
    if (!card) return;
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.classList.add("is-new-highlight");
    window.setTimeout(() => card.classList.remove("is-new-highlight"), 2600);
  })();

  document.addEventListener("drop", (event) => {
    const column = event.target.closest(".kanban-column.is-collapsed");
    if (!column) return;
    event.preventDefault();
    dropOnZone(column.querySelector("[data-drop-zone]"), event.clientY);
  });
})();
