(function () {
  const board = document.getElementById("kanban-board");
  if (!board) return;

  const urls = window.TODO_URLS || {};
  let draggedCard = null;

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
    btn.addEventListener("click", () => {
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
  });

  document.querySelectorAll("[data-close-modal]").forEach((el) => {
    el.addEventListener("click", () => closeModal(el));
  });

  board.addEventListener("click", (event) => {
    const editBtn = event.target.closest(".js-edit-card");
    if (!editBtn) return;
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
  });

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
    board.querySelectorAll(".kanban-drop-indicator").forEach((el) => el.remove());
    board.querySelectorAll(".kanban-column.drag-over").forEach((el) => {
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

  board.querySelectorAll(".kanban-card").forEach((card) => {
    card.addEventListener("dragstart", (event) => {
      draggedCard = card;
      card.classList.add("is-dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", card.dataset.cardId || "");
    });
    card.addEventListener("dragend", () => {
      card.classList.remove("is-dragging");
      hideIndicators();
      board.querySelectorAll(".kanban-column__cards").forEach(syncEmptyState);
      draggedCard = null;
    });
  });

  board.querySelectorAll("[data-drop-zone]").forEach((zone) => {
    zone.addEventListener("dragover", (event) => {
      event.preventDefault();
      if (!draggedCard) return;
      showIndicator(zone, event.clientY);
    });
    zone.addEventListener("dragleave", (event) => {
      if (!zone.contains(event.relatedTarget)) {
        hideIndicators();
      }
    });
    zone.addEventListener("drop", (event) => {
      event.preventDefault();
      if (!draggedCard) return;
      const fromZone = draggedCard.closest("[data-drop-zone]");
      insertAt(zone, draggedCard, event.clientY);
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
    });
  });
})();
