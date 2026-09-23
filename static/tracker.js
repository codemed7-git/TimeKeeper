(function () {
  const root = document.getElementById("tracker-root");
  const modal = document.getElementById("tracker-item-modal");
  const form = document.getElementById("tracker-item-form");
  const dataNode = document.getElementById("tracker-items-data");
  if (!root || !modal || !form) return;

  let items = [];
  try {
    items = JSON.parse(dataNode?.textContent || "[]");
  } catch (_) {
    items = [];
  }

  const byId = new Map(items.map((item) => [Number(item.id), item]));
  const replaceId = (url, id) =>
    url.replace(/\/0(?=\/(?:update|entry|start|pause|reset-today|delete)$)/, `/${id}`);

  const deleteBtn = document.getElementById("tracker-delete-btn");
  let currentDeleteUrl = null;
  let currentItemTitle = "";

  if (deleteBtn) {
    deleteBtn.addEventListener("click", () => {
      if (!currentDeleteUrl) return;
      if (!confirm(`Удалить задачу «${currentItemTitle}» вместе со всей историей?`)) return;
      const deleteForm = document.createElement("form");
      deleteForm.method = "post";
      deleteForm.action = currentDeleteUrl;
      document.body.appendChild(deleteForm);
      deleteForm.submit();
    });
  }

  function setModalOpen(open) {
    modal.hidden = !open;
    document.body.classList.toggle("modal-open", open);
    if (open) {
      window.setTimeout(() => form.elements.title?.focus(), 0);
    }
  }

  function syncScheduleFields() {
    const type = form.elements.schedule_type.value;
    form.querySelectorAll("[data-schedule-fields]").forEach((field) => {
      field.hidden = field.dataset.scheduleFields !== type;
    });
    const taskType = form.elements.task_type.value;
    form.querySelectorAll("[data-start-date-field]").forEach((field) => {
      const inVisibleGroup = field.closest("[data-type-fields]")?.dataset.typeFields === taskType;
      field.hidden = type === "once" || !inVisibleGroup;
    });
  }

  function syncTaskTypeFields() {
    const taskType = form.elements.task_type.value;
    form.querySelectorAll("[data-type-fields]").forEach((field) => {
      field.hidden = field.dataset.typeFields !== taskType;
    });
    form.querySelectorAll("[data-type-hint]").forEach((hint) => {
      hint.hidden = hint.dataset.typeHint !== taskType;
    });
    syncScheduleFields();
  }

  function resetForm() {
    form.reset();
    form.action = root.dataset.createUrl;
    form.elements.icon.value = "✓";
    form.elements.color.value = "#5b8cff";
    form.elements.task_type.value = "routine";
    form.elements.target_value.value = "1";
    form.elements.unit.value = "раз";
    form.elements.budget_hours.value = "1";
    form.elements.budget_minutes.value = "0";
    form.elements.start_date.value = root.dataset.date;
    form.elements.due_date.value = root.dataset.date;
    form.elements.interval_days.value = "2";
    form.elements.weekly_target.value = "3";
    form.querySelectorAll('input[name="weekdays"]').forEach((box) => {
      box.checked = true;
    });
    document.getElementById("tracker-modal-title").textContent = "Новая задача";
    document.getElementById("tracker-submit").textContent = "Создать";
    currentDeleteUrl = null;
    currentItemTitle = "";
    if (deleteBtn) deleteBtn.hidden = true;
    syncTaskTypeFields();
  }

  function fillForm(item) {
    resetForm();
    form.action = replaceId(root.dataset.updateUrl, item.id);
    form.elements.title.value = item.title || "";
    form.elements.note.value = item.note || "";
    form.elements.icon.value = item.icon || "✓";
    form.elements.color.value = item.color || "#5b8cff";
    form.elements.task_type.value = item.task_type || "routine";
    form.elements.schedule_type.value = item.schedule_type || "daily";
    form.elements.interval_days.value = item.interval_days || 1;
    form.elements.weekly_target.value = item.weekly_target || 1;
    form.elements.target_value.value = item.target_value || 1;
    form.elements.unit.value = item.unit || "раз";
    const budget = Number(item.budget_seconds || 0);
    form.elements.budget_hours.value = String(Math.floor(budget / 3600));
    form.elements.budget_minutes.value = String(Math.floor((budget % 3600) / 60));
    form.elements.start_date.value = item.start_date || root.dataset.date;
    form.elements.due_date.value = item.due_date || root.dataset.date;
    const selectedDays = new Set((item.weekdays_list || []).map(Number));
    form.querySelectorAll('input[name="weekdays"]').forEach((box) => {
      box.checked = selectedDays.has(Number(box.value));
    });
    document.getElementById("tracker-modal-title").textContent = "Настройка задачи";
    document.getElementById("tracker-submit").textContent = "Сохранить";
    currentDeleteUrl = replaceId(root.dataset.deleteUrl, item.id);
    currentItemTitle = item.title || "";
    if (deleteBtn) deleteBtn.hidden = false;
    syncTaskTypeFields();
  }

  document.querySelectorAll("[data-open-tracker-modal]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.openTrackerModal === "edit") {
        const item = byId.get(Number(button.dataset.itemId));
        if (!item) return;
        fillForm(item);
      } else {
        resetForm();
      }
      setModalOpen(true);
    });
  });

  document.querySelectorAll("[data-close-tracker-modal]").forEach((element) => {
    element.addEventListener("click", () => setModalOpen(false));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !modal.hidden) setModalOpen(false);
  });
  form.querySelectorAll('input[name="schedule_type"]').forEach((radio) => {
    radio.addEventListener("change", syncScheduleFields);
  });
  form.querySelectorAll('input[name="task_type"]').forEach((radio) => {
    radio.addEventListener("change", syncTaskTypeFields);
  });

  /* ---------- Custom calendar popup ---------- */

  (function initCalendar() {
    const calWrap = document.getElementById("tracker-calendar");
    const calTrigger = document.getElementById("tracker-calendar-trigger");
    const calPopup = document.getElementById("tracker-calendar-popup");
    const calDaysEl = document.getElementById("tracker-calendar-days");
    const calMonthLabel = document.getElementById("tracker-calendar-month-label");
    if (!calWrap || !calTrigger || !calPopup || !calDaysEl || !calMonthLabel) return;

    const MONTH_NAMES = [
      "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
      "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
    ];

    const selectedDate = new Date(`${root.dataset.date}T00:00:00`);
    const todayDate = new Date();
    todayDate.setHours(0, 0, 0, 0);
    let calViewYear = selectedDate.getFullYear();
    let calViewMonth = selectedDate.getMonth();

    function isoDate(y, m, d) {
      return `${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    }

    function sameDay(a, b) {
      return (
        a.getFullYear() === b.getFullYear() &&
        a.getMonth() === b.getMonth() &&
        a.getDate() === b.getDate()
      );
    }

    function renderCalendar() {
      calMonthLabel.textContent = `${MONTH_NAMES[calViewMonth]} ${calViewYear}`;
      calDaysEl.innerHTML = "";
      const firstOfMonth = new Date(calViewYear, calViewMonth, 1);
      const leadOffset = (firstOfMonth.getDay() + 6) % 7; // Monday-first
      const daysInMonth = new Date(calViewYear, calViewMonth + 1, 0).getDate();
      const prevMonthDays = new Date(calViewYear, calViewMonth, 0).getDate();

      const cells = [];
      for (let i = leadOffset - 1; i >= 0; i--) {
        cells.push({
          day: prevMonthDays - i,
          outside: true,
          y: calViewMonth === 0 ? calViewYear - 1 : calViewYear,
          m: calViewMonth === 0 ? 11 : calViewMonth - 1,
        });
      }
      for (let d = 1; d <= daysInMonth; d++) {
        cells.push({ day: d, outside: false, y: calViewYear, m: calViewMonth });
      }
      let trailing = 1;
      while (cells.length % 7 !== 0) {
        cells.push({
          day: trailing,
          outside: true,
          y: calViewMonth === 11 ? calViewYear + 1 : calViewYear,
          m: calViewMonth === 11 ? 0 : calViewMonth + 1,
        });
        trailing += 1;
      }

      cells.forEach((cell) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "tracker-calendar-day";
        btn.textContent = String(cell.day);
        const cellDate = new Date(cell.y, cell.m, cell.day);
        if (cell.outside) btn.classList.add("is-outside");
        if (sameDay(cellDate, todayDate)) btn.classList.add("is-today");
        if (sameDay(cellDate, selectedDate)) btn.classList.add("is-selected");
        btn.addEventListener("click", () => {
          const url = new URL(window.location.href);
          url.searchParams.set("date", isoDate(cell.y, cell.m, cell.day));
          window.location.href = url.toString();
        });
        calDaysEl.appendChild(btn);
      });
    }

    function openCalendar() {
      calViewYear = selectedDate.getFullYear();
      calViewMonth = selectedDate.getMonth();
      renderCalendar();
      calPopup.hidden = false;
      calTrigger.setAttribute("aria-expanded", "true");
    }

    function closeCalendar() {
      calPopup.hidden = true;
      calTrigger.setAttribute("aria-expanded", "false");
    }

    calTrigger.addEventListener("click", (event) => {
      event.stopPropagation();
      if (calPopup.hidden) openCalendar();
      else closeCalendar();
    });

    calPopup.querySelectorAll("[data-cal-month]").forEach((btn) => {
      btn.addEventListener("click", (event) => {
        event.stopPropagation();
        const delta = Number(btn.dataset.calMonth);
        calViewMonth += delta;
        if (calViewMonth < 0) {
          calViewMonth = 11;
          calViewYear -= 1;
        }
        if (calViewMonth > 11) {
          calViewMonth = 0;
          calViewYear += 1;
        }
        renderCalendar();
      });
    });

    document.addEventListener("click", (event) => {
      if (!calPopup.hidden && !calWrap.contains(event.target)) closeCalendar();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !calPopup.hidden) closeCalendar();
    });
  })();

  document.querySelectorAll("[data-tracker-tab]").forEach((tab) => {
    tab.addEventListener("click", () => {
      const name = tab.dataset.trackerTab;
      document.querySelectorAll("[data-tracker-tab]").forEach((other) => {
        const active = other === tab;
        other.classList.toggle("is-active", active);
        other.setAttribute("aria-selected", active ? "true" : "false");
      });
      document.querySelectorAll("[data-tracker-panel]").forEach((panel) => {
        const active = panel.dataset.trackerPanel === name;
        panel.classList.toggle("is-active", active);
        panel.hidden = !active;
      });
    });
  });

  function updateSummary(summary) {
    const values = {
      "[data-summary-done]": summary.done,
      "[data-summary-total]": summary.due_count,
      "[data-summary-percent]": summary.percent,
      "[data-summary-streak]": summary.best_streak,
    };
    Object.entries(values).forEach(([selector, value]) => {
      const node = document.querySelector(selector);
      if (node) node.textContent = String(value);
    });
    const bar = document.querySelector("[data-summary-bar]");
    if (bar) bar.style.width = `${summary.percent}%`;
  }

  function updateCard(card, item) {
    card.classList.toggle("is-complete", Boolean(item.complete));
    const value = card.querySelector("[data-entry-value]");
    const streak = card.querySelector("[data-item-streak]");
    const week = card.querySelector("[data-week-progress]");
    const bar = card.querySelector("[data-entry-bar]");
    if (value) value.textContent = item.value;
    if (streak) streak.textContent = item.streak;
    if (week) week.textContent = item.progress_value;
    card.querySelectorAll("[data-entry-complete]").forEach((btn) => {
      btn.setAttribute("aria-pressed", item.complete ? "true" : "false");
    });
    if (bar) {
      const target = item.target_value || 1;
      const pct = Math.min(100, (item.value / target) * 100);
      bar.style.width = `${pct}%`;
    }
    const selectedDot = card.querySelector(".tracker-history__day.is-selected i");
    if (selectedDot) {
      selectedDot.className = item.value >= item.target_value ? "is-done" : "is-due";
    }
  }

  async function setEntry(card, payload) {
    if (card.classList.contains("is-loading")) return;
    const id = Number(card.dataset.trackerCard);
    card.classList.add("is-loading");
    try {
      const response = await fetch(replaceId(root.dataset.entryUrl, id), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "fetch",
        },
        body: JSON.stringify({ date: root.dataset.date, ...payload }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Не удалось сохранить");
      byId.set(id, result.item);
      updateCard(card, result.item);
      updateSummary(result.summary);
    } catch (error) {
      window.alert(error.message || "Не удалось сохранить отметку");
    } finally {
      card.classList.remove("is-loading");
    }
  }

  document.querySelectorAll("[data-tracker-card]").forEach((card) => {
    card.querySelectorAll("[data-entry-delta]").forEach((button) => {
      button.addEventListener("click", () => {
        setEntry(card, { delta: Number(button.dataset.entryDelta) });
      });
    });
    const complete = card.querySelector("[data-entry-complete]");
    complete?.addEventListener("click", () => {
      const value = Number(card.querySelector("[data-entry-value]")?.textContent || 0);
      const target = Number(complete.dataset.entryComplete || 1);
      setEntry(card, { value: value >= target ? 0 : target });
    });
  });

  /* ---------- Hourglass tasks (time-budget timers) ---------- */

  function formatHms(totalSeconds) {
    const s = Math.max(0, Math.round(totalSeconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    return `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
  }

  function liveRemaining(card) {
    const remaining = Number(card.dataset.remaining || 0);
    const since = card.dataset.runningSince || "";
    if (!since) return remaining;
    const started = new Date(since);
    if (isNaN(started.getTime())) return remaining;
    return remaining - (Date.now() - started.getTime()) / 1000;
  }

  function applyHourglassItem(item) {
    const card = document.querySelector(`[data-tracker-card="${item.id}"]`);
    if (!card || card.dataset.taskType !== "hourglass") return;
    byId.set(Number(item.id), item);
    card.classList.toggle("is-complete", Boolean(item.complete));
    card.classList.toggle("is-running", Boolean(item.is_running));
    card.dataset.remaining = String(item.remaining_seconds);
    card.dataset.runningSince = item.running_since || "";
    const toggle = card.querySelector("[data-hourglass-toggle]");
    if (toggle) toggle.textContent = item.is_running ? "Пауза" : "Старт";
    const complete = card.querySelector("[data-hourglass-complete]");
    if (complete) complete.setAttribute("aria-pressed", item.complete ? "true" : "false");
    const timeEl = card.querySelector("[data-hourglass-remaining]");
    if (timeEl) timeEl.textContent = formatHms(item.remaining_seconds);
    const bar = card.querySelector("[data-hourglass-bar]");
    if (bar) {
      const pct = item.budget_seconds ? Math.min(100, (item.value / item.budget_seconds) * 100) : 0;
      bar.style.width = `${pct}%`;
    }
    const streak = card.querySelector("[data-item-streak]");
    if (streak) streak.textContent = item.streak;
  }

  async function hourglassAction(card, url) {
    if (card.classList.contains("is-loading")) return;
    card.classList.add("is-loading");
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
        body: "{}",
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Не удалось сохранить");
      applyHourglassItem(result.item);
      (result.paused_items || []).forEach(applyHourglassItem);
      updateSummary(result.summary);
    } catch (error) {
      window.alert(error.message || "Не удалось сохранить");
    } finally {
      card.classList.remove("is-loading");
    }
  }

  async function toggleHourglassComplete(card) {
    if (card.classList.contains("is-loading")) return;
    const id = Number(card.dataset.trackerCard);
    card.classList.add("is-loading");
    try {
      if (card.classList.contains("is-running")) {
        const pauseRes = await fetch(replaceId(root.dataset.pauseUrl, id), {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
          body: "{}",
        });
        const pauseResult = await pauseRes.json();
        if (!pauseRes.ok) throw new Error(pauseResult.error || "Не удалось поставить на паузу");
        applyHourglassItem(pauseResult.item);
      }
      const budget = Number(card.dataset.budgetSeconds || 0);
      const nowComplete = card.classList.contains("is-complete");
      const response = await fetch(replaceId(root.dataset.entryUrl, id), {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
        body: JSON.stringify({ date: root.dataset.date, value: nowComplete ? 0 : budget }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Не удалось сохранить");
      byId.set(id, result.item);
      applyHourglassItem(result.item);
      updateSummary(result.summary);
    } catch (error) {
      window.alert(error.message || "Не удалось сохранить");
    } finally {
      card.classList.remove("is-loading");
    }
  }

  document.querySelectorAll('.tracker-card--hourglass[data-tracker-card]').forEach((card) => {
    const id = Number(card.dataset.trackerCard);
    card.querySelector("[data-hourglass-toggle]")?.addEventListener("click", () => {
      const url = card.classList.contains("is-running")
        ? replaceId(root.dataset.pauseUrl, id)
        : replaceId(root.dataset.startUrl, id);
      hourglassAction(card, url);
    });
    card.querySelector("[data-hourglass-reset]")?.addEventListener("click", () => {
      if (!confirm("Сбросить сегодняшний расход времени?")) return;
      hourglassAction(card, replaceId(root.dataset.resetUrl, id));
    });
    card.querySelector("[data-hourglass-complete]")?.addEventListener("click", () => {
      toggleHourglassComplete(card);
    });
  });

  let hourglassTickBusy = false;
  setInterval(() => {
    if (hourglassTickBusy) return;
    const running = document.querySelectorAll(".tracker-card--hourglass.is-running[data-tracker-card]");
    running.forEach((card) => {
      const remaining = liveRemaining(card);
      const timeEl = card.querySelector("[data-hourglass-remaining]");
      if (timeEl) timeEl.textContent = formatHms(Math.max(0, remaining));
      const bar = card.querySelector("[data-hourglass-bar]");
      const budget = Number(card.dataset.budgetSeconds || 0);
      if (bar && budget) {
        const spent = budget - remaining;
        bar.style.width = `${Math.min(100, Math.max(0, (spent / budget) * 100))}%`;
      }
      if (remaining <= 0) {
        hourglassTickBusy = true;
        const id = Number(card.dataset.trackerCard);
        hourglassAction(card, replaceId(root.dataset.pauseUrl, id)).finally(() => {
          hourglassTickBusy = false;
        });
      }
    });
  }, 1000);
})();
