(function () {
  const ROOT = document.documentElement;
  const THEMES = ["dark", "light"];
  const STYLES = ["midnight", "forest", "sunset", "graphite", "ocean"];
  const THEME_NAMES = { dark: "Тёмная тема", light: "Светлая тема" };
  const STYLE_NAMES = {
    midnight: "Полночь",
    forest: "Лес",
    sunset: "Закат",
    graphite: "Графит",
    ocean: "Океан",
  };
  const settingsModal = document.getElementById("modal-app-settings");
  const settingsOpener = document.getElementById("app-settings-open");
  const sidebarEl = document.getElementById("app-sidebar");
  const sidebarPinBtn = document.getElementById("app-sidebar-pin");
  const NAV_KEYS = ["work_days", "todo", "timer", "knowledge", "tracker", "networking"];
  let lastFocused = null;

  function applySidebarPinState(pinned) {
    if (!sidebarEl || !sidebarPinBtn) return;
    sidebarEl.classList.toggle("is-pinned", pinned);
    sidebarPinBtn.setAttribute("aria-pressed", pinned ? "true" : "false");
    sidebarPinBtn.title = pinned ? "Открепить панель" : "Закрепить панель";
    sidebarPinBtn.setAttribute(
      "aria-label",
      pinned ? "Открепить панель" : "Закрепить панель открытой"
    );
  }

  function setSidebarPinned(pinned) {
    try {
      localStorage.setItem("tk-sidebar-pinned", pinned ? "1" : "0");
    } catch (_) {}
    applySidebarPinState(pinned);
  }

  if (sidebarPinBtn) {
    applySidebarPinState(sidebarEl.classList.contains("is-pinned"));
    sidebarPinBtn.addEventListener("click", () => {
      setSidebarPinned(!sidebarEl.classList.contains("is-pinned"));
    });
  }

  function readHiddenNavKeys() {
    try {
      const arr = JSON.parse(localStorage.getItem("tk-nav-hidden") || "[]");
      return Array.isArray(arr) ? arr.filter((k) => NAV_KEYS.includes(k)) : [];
    } catch (_) {
      return [];
    }
  }

  function applyNavVisibility() {
    const hidden = readHiddenNavKeys();
    if (hidden.length) {
      ROOT.setAttribute("data-nav-hidden", hidden.join(" "));
    } else {
      ROOT.removeAttribute("data-nav-hidden");
    }
    document.querySelectorAll("[data-nav-toggle]").forEach((input) => {
      input.checked = !hidden.includes(input.getAttribute("data-nav-toggle"));
    });
  }

  function setNavHidden(key, isHidden) {
    let hidden = readHiddenNavKeys();
    if (isHidden && !hidden.includes(key)) hidden.push(key);
    if (!isHidden) hidden = hidden.filter((k) => k !== key);
    try {
      localStorage.setItem("tk-nav-hidden", JSON.stringify(hidden));
    } catch (_) {}
    applyNavVisibility();
  }

  function notifyAppearance(message) {
    const note = document.getElementById("settings-save-note");
    if (note && message) note.textContent = `${message} · сохранено`;
    window.dispatchEvent(
      new CustomEvent("timekeeperappearancechange", {
        detail: { theme: readTheme(), style: readStyle() },
      })
    );
  }

  function readTheme() {
    const t = ROOT.getAttribute("data-theme") || localStorage.getItem("tk-theme") || "dark";
    return THEMES.includes(t) ? t : "dark";
  }

  function readStyle() {
    const s = ROOT.getAttribute("data-style") || localStorage.getItem("tk-style") || "midnight";
    return STYLES.includes(s) ? s : "midnight";
  }

  function restoreStoredAppearance() {
    try {
      const storedTheme = localStorage.getItem("tk-theme");
      const storedStyle = localStorage.getItem("tk-style");
      const theme = THEMES.includes(storedTheme) ? storedTheme : "dark";
      const style = STYLES.includes(storedStyle) ? storedStyle : "midnight";
      ROOT.setAttribute("data-theme", theme);
      ROOT.setAttribute("data-style", style);
      if (theme !== storedTheme) localStorage.setItem("tk-theme", theme);
      if (style !== storedStyle) localStorage.setItem("tk-style", style);
    } catch (_) {}
    syncUi();
  }

  function applyTheme(theme) {
    if (!THEMES.includes(theme)) theme = "dark";
    ROOT.setAttribute("data-theme", theme);
    try {
      localStorage.setItem("tk-theme", theme);
    } catch (_) {}
    syncUi();
    notifyAppearance(THEME_NAMES[theme]);
  }

  function applyStyle(style) {
    if (!STYLES.includes(style)) style = "midnight";
    ROOT.setAttribute("data-style", style);
    try {
      localStorage.setItem("tk-style", style);
    } catch (_) {}
    syncUi();
    notifyAppearance(`Стиль «${STYLE_NAMES[style]}»`);
  }

  function syncUi() {
    const theme = readTheme();
    const style = readStyle();
    document.querySelectorAll("[data-theme-value]").forEach((btn) => {
      const on = btn.getAttribute("data-theme-value") === theme;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
    document.querySelectorAll("[data-style-value]").forEach((btn) => {
      const on = btn.getAttribute("data-style-value") === style;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function setBackgroundInert(inert) {
    [document.querySelector(".page"), document.getElementById("timer-float")]
      .filter(Boolean)
      .forEach((element) => {
        element.inert = inert;
      });
  }

  function openModal(id) {
    const el = document.getElementById(id);
    if (!el) return;
    lastFocused = document.activeElement;
    el.hidden = false;
    document.body.classList.add("modal-open");
    setBackgroundInert(true);
    syncUi();
    const selected =
      el.querySelector("[data-theme-value].is-active") ||
      el.querySelector(".modal__dialog");
    window.requestAnimationFrame(() => selected?.focus());
  }

  function closeModal(fromEl) {
    const modal = fromEl && fromEl.closest ? fromEl.closest(".modal") : fromEl;
    if (!modal) return;
    modal.hidden = true;
    setBackgroundInert(false);
    if (!document.querySelector(".modal:not([hidden])")) {
      document.body.classList.remove("modal-open");
    }
    if (lastFocused && typeof lastFocused.focus === "function") {
      lastFocused.focus();
    }
    lastFocused = null;
  }

  if (settingsOpener) {
    settingsOpener.addEventListener(
      "click",
      (event) => {
        event.preventDefault();
        event.stopImmediatePropagation();
        openModal("modal-app-settings");
      },
      true
    );
  }

  if (settingsModal) {
    settingsModal.querySelectorAll("[data-close-modal]").forEach((el) => {
      el.addEventListener(
        "click",
        (event) => {
          event.preventDefault();
          event.stopImmediatePropagation();
          closeModal(el);
        },
        true
      );
    });
    document.addEventListener("keydown", (event) => {
      if (settingsModal.hidden) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        closeModal(settingsModal);
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        settingsModal.querySelectorAll(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        )
      ).filter((element) => !element.hidden && element.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }, true);
  }

  document.querySelectorAll("[data-theme-value]").forEach((btn) => {
    btn.addEventListener("click", () => applyTheme(btn.getAttribute("data-theme-value")));
  });
  document.querySelectorAll("[data-style-value]").forEach((btn) => {
    btn.addEventListener("click", () => applyStyle(btn.getAttribute("data-style-value")));
  });

  document.querySelectorAll("[data-nav-toggle]").forEach((input) => {
    input.addEventListener("change", (event) => {
      setNavHidden(input.getAttribute("data-nav-toggle"), !event.target.checked);
    });
  });

  window.addEventListener("storage", (event) => {
    if (event.key === "tk-theme" || event.key === "tk-style" || event.key === null) {
      restoreStoredAppearance();
      notifyAppearance("Оформление обновлено");
    }
    if (event.key === "tk-nav-hidden") {
      applyNavVisibility();
    }
    if (event.key === "tk-sidebar-pinned") {
      applySidebarPinState(localStorage.getItem("tk-sidebar-pinned") === "1");
    }
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) restoreStoredAppearance();
  });

  applyNavVisibility();
  syncUi();
  window.requestAnimationFrame(() => ROOT.classList.add("appearance-ready"));
})();
