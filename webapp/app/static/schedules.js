// Moduł harmonogramów. Montuje się wyłącznie w #schedules (CONTRACT.md §6) i korzysta
// z klas CSS zdefiniowanych w style.css. Listę urządzeń bierze ze zdarzenia, które
// wystawia app.js, więc nie dubluje strumienia SSE i nie dotyka DOM-u panelu.

const DEVICES_EVENT = "gniazdka:devices";
const API = "/api/schedules";

const DAYS = [
  [1, "Pn"],
  [2, "Wt"],
  [3, "Śr"],
  [4, "Cz"],
  [5, "Pt"],
  [6, "So"],
  [7, "Nd"],
];

const KINDS = [
  ["cron", "Cyklicznie"],
  ["once", "Jednorazowo"],
  ["timer", "Minutnik"],
];

const root = document.getElementById("schedules");

/** "dev|node" -> etykieta czytelna dla człowieka */
const targets = new Map();
let kind = "cron";
let schedules = [];

// ------------------------------------------------------------------- pomocnicze

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function two(value) {
  return String(value).padStart(2, "0");
}

function channelLabel(channel) {
  if (channel.name) return channel.name;
  const dash = channel.node.lastIndexOf("-");
  if (dash > 0 && /^\d+$/.test(channel.node.slice(dash + 1))) {
    return `Gniazdo ${Number(channel.node.slice(dash + 1)) + 1}`;
  }
  return "Gniazdo 1";
}

function formatDateTime(iso) {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("pl-PL", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function localInputValue(date) {
  return `${date.getFullYear()}-${two(date.getMonth() + 1)}-${two(date.getDate())}T${two(
    date.getHours(),
  )}:${two(date.getMinutes())}`;
}

function describe(schedule) {
  const action = schedule.action === "on" ? "włącz" : "wyłącz";
  if (schedule.kind === "cron") {
    const time = `${two(schedule.hour ?? 0)}:${two(schedule.minute ?? 0)}`;
    const days = (schedule.days || "")
      .split(",")
      .map((d) => DAYS.find(([iso]) => String(iso) === d.trim()))
      .filter(Boolean)
      .map(([, name]) => name);
    const when = days.length === 0 || days.length === 7 ? "codziennie" : days.join(", ");
    return `${action} · ${when} o ${time}`;
  }
  if (schedule.kind === "timer") {
    return `${action} · minutnik do ${formatDateTime(schedule.run_at)}`;
  }
  return `${action} · ${formatDateTime(schedule.run_at)}`;
}

function targetLabel(schedule) {
  return targets.get(`${schedule.dev}|${schedule.node}`) || `${schedule.dev} / ${schedule.node}`;
}

// -------------------------------------------------------------------- szkielet

const head = el("div", "row section-title");
head.append(el("h2", null, "Harmonogramy"));
const countBadge = el("span", "badge plain", "0 zadań");
head.append(countBadge);

const tabs = el("div", "tabs");
const tabButtons = new Map();
for (const [value, label] of KINDS) {
  const button = el("button", "tab", label);
  button.type = "button";
  button.setAttribute("aria-selected", String(value === kind));
  button.addEventListener("click", () => {
    kind = value;
    for (const [other, btn] of tabButtons) btn.setAttribute("aria-selected", String(other === kind));
    syncForm();
  });
  tabButtons.set(value, button);
  tabs.append(button);
}

const form = el("form", "form-grid");
form.noValidate = true;

function field(labelText, control, extraClass) {
  const wrap = el("label", extraClass);
  wrap.append(el("span", null, labelText), control);
  return wrap;
}

const labelInput = el("input", "input");
labelInput.type = "text";
labelInput.placeholder = "np. Lampa w salonie — wieczorem";
labelInput.maxLength = 120;
const labelField = field("Etykieta", labelInput, "full");

const targetSelect = el("select", "select");
const targetField = field("Gniazdo", targetSelect);

const actionSelect = el("select", "select");
for (const [value, text] of [
  ["on", "Włącz"],
  ["off", "Wyłącz"],
]) {
  const option = el("option", null, text);
  option.value = value;
  actionSelect.append(option);
}
const actionField = field("Akcja", actionSelect);

const timeInput = el("input", "input");
timeInput.type = "time";
timeInput.value = "06:30";
timeInput.step = 60;
const timeField = field("Godzina", timeInput);

const daysWrap = el("div", "days");
const dayInputs = new Map();
for (const [iso, name] of DAYS) {
  const wrap = el("label", "day");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.value = String(iso);
  wrap.append(input, el("span", null, name));
  dayInputs.set(iso, input);
  daysWrap.append(wrap);
}
const daysField = field("Dni tygodnia (nic = codziennie)", daysWrap, "full");

const dateInput = el("input", "input");
dateInput.type = "datetime-local";
const dateField = field("Data i godzina", dateInput);

const minutesInput = el("input", "input");
minutesInput.type = "number";
minutesInput.min = "1";
minutesInput.max = "10080";
minutesInput.value = "45";
const minutesField = field("Na ile minut", minutesInput);

const submit = el("button", "btn btn-primary", "Dodaj zadanie");
submit.type = "submit";
const submitField = el("div", null);
submitField.append(submit);

form.append(
  labelField,
  targetField,
  actionField,
  timeField,
  dateField,
  minutesField,
  daysField,
  submitField,
);

const errorBox = el("div", "error hidden");
const list = el("div", "schedule-list");

root.append(head, tabs, form, errorBox, list);

// ----------------------------------------------------------------------- formularz

function syncForm() {
  timeField.classList.toggle("hidden", kind !== "cron");
  daysField.classList.toggle("hidden", kind !== "cron");
  dateField.classList.toggle("hidden", kind !== "once");
  minutesField.classList.toggle("hidden", kind !== "timer");
  actionField.querySelector("span").textContent = kind === "timer" ? "Ustaw w stan" : "Akcja";
  submit.textContent = kind === "timer" ? "Uruchom minutnik" : "Dodaj zadanie";
  if (kind === "once" && !dateInput.value) {
    const soon = new Date(Date.now() + 60 * 60 * 1000);
    dateInput.value = localInputValue(soon);
  }
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.remove("hidden");
}

function clearError() {
  errorBox.textContent = "";
  errorBox.classList.add("hidden");
}

function selectedTarget() {
  const value = targetSelect.value;
  if (!value) return null;
  const separator = value.lastIndexOf("|");
  return { dev: value.slice(0, separator), node: value.slice(separator + 1) };
}

async function readError(res) {
  try {
    const body = await res.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail) && body.detail.length) {
      return body.detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
    }
  } catch (err) {
    /* pusta albo nie-JSON-owa odpowiedź */
  }
  return `HTTP ${res.status}`;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();

  const target = selectedTarget();
  if (!target) {
    showError("Wybierz gniazdo. Jeśli lista jest pusta, panel nie widzi jeszcze urządzeń.");
    return;
  }

  submit.disabled = true;
  try {
    if (kind === "timer") {
      const minutes = Number(minutesInput.value);
      if (!Number.isFinite(minutes) || minutes < 1) {
        showError("Podaj liczbę minut (co najmniej 1).");
        return;
      }
      const body = {
        minutes: Math.round(minutes),
        on: actionSelect.value === "on",
      };
      if (labelInput.value.trim()) body.label = labelInput.value.trim();
      const res = await fetch(
        `/api/devices/${encodeURIComponent(target.dev)}/${encodeURIComponent(target.node)}/timer`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      if (!res.ok) throw new Error(await readError(res));
    } else {
      const body = {
        label: labelInput.value.trim() || defaultLabel(target),
        dev: target.dev,
        node: target.node,
        action: actionSelect.value,
        kind,
        enabled: true,
      };
      if (kind === "cron") {
        const [hour, minute] = (timeInput.value || "06:30").split(":");
        body.hour = Number(hour);
        body.minute = Number(minute);
        const days = [...dayInputs.entries()]
          .filter(([, input]) => input.checked)
          .map(([iso]) => iso);
        body.days = days.length ? days.join(",") : null;
      } else {
        if (!dateInput.value) {
          showError("Podaj datę i godzinę.");
          return;
        }
        body.run_at = dateInput.value;
      }
      const res = await fetch(API, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(await readError(res));
    }
    labelInput.value = "";
    await refresh();
  } catch (err) {
    showError(`Nie udało się zapisać zadania: ${err.message}`);
  } finally {
    submit.disabled = false;
  }
});

function defaultLabel(target) {
  const action = actionSelect.value === "on" ? "Włącz" : "Wyłącz";
  return `${action} — ${targets.get(`${target.dev}|${target.node}`) || target.node}`;
}

// ---------------------------------------------------------------------- lista

function renderList() {
  list.textContent = "";
  countBadge.textContent = `${schedules.length} ${schedules.length === 1 ? "zadanie" : "zadań"}`;

  if (schedules.length === 0) {
    list.append(el("p", "muted", "Brak zadań. Dodaj pierwsze powyżej."));
    return;
  }

  for (const schedule of schedules) {
    const item = el("div", `schedule${schedule.enabled ? "" : " disabled"}`);

    const top = el("div", "row wrap");
    const info = el("div", "col");
    info.append(
      el("span", "schedule-label", schedule.label),
      el("span", "muted", `${targetLabel(schedule)} · ${describe(schedule)}`),
    );

    const controls = el("div", "row start");

    const toggle = el("label", "switch");
    const toggleInput = document.createElement("input");
    toggleInput.type = "checkbox";
    toggleInput.checked = Boolean(schedule.enabled);
    toggleInput.setAttribute("aria-label", `Włącz zadanie ${schedule.label}`);
    toggle.append(toggleInput, el("span", "slider"));
    toggleInput.addEventListener("change", async () => {
      toggleInput.disabled = true;
      try {
        const res = await fetch(`${API}/${schedule.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: toggleInput.checked }),
        });
        if (!res.ok) throw new Error(await readError(res));
        clearError();
        await refresh();
      } catch (err) {
        toggleInput.checked = !toggleInput.checked;
        showError(`Nie udało się zmienić zadania: ${err.message}`);
      } finally {
        toggleInput.disabled = false;
      }
    });

    const remove = el("button", "btn btn-danger btn-sm", "Usuń");
    remove.type = "button";
    remove.addEventListener("click", async () => {
      if (!window.confirm(`Usunąć zadanie „${schedule.label}”?`)) return;
      remove.disabled = true;
      try {
        const res = await fetch(`${API}/${schedule.id}`, { method: "DELETE" });
        if (!res.ok && res.status !== 204) throw new Error(await readError(res));
        clearError();
        await refresh();
      } catch (err) {
        showError(`Nie udało się usunąć zadania: ${err.message}`);
      } finally {
        remove.disabled = false;
      }
    });

    controls.append(toggle, remove);
    top.append(info, controls);

    const next = schedule.next_run
      ? `Najbliższe uruchomienie: ${formatDateTime(schedule.next_run)}`
      : schedule.enabled
        ? "Nie zaplanowano (termin minął)"
        : "Wyłączone";
    item.append(top, el("p", "muted", next));
    list.append(item);
  }
}

function renderTargets(snapshot) {
  const previous = targetSelect.value;
  targets.clear();
  targetSelect.textContent = "";

  const devices = Array.isArray(snapshot?.devices) ? snapshot.devices : [];
  for (const device of devices) {
    for (const channel of device.channels || []) {
      const key = `${device.id}|${channel.node}`;
      const deviceName = device.name || device.id;
      const label =
        (device.channels || []).length > 1
          ? `${deviceName} — ${channelLabel(channel)}`
          : deviceName;
      targets.set(key, label);
      const option = el("option", null, label);
      option.value = key;
      targetSelect.append(option);
    }
  }

  if (targets.size === 0) {
    const option = el("option", null, "brak urządzeń");
    option.value = "";
    targetSelect.append(option);
    targetSelect.disabled = true;
  } else {
    targetSelect.disabled = false;
    if (previous && targets.has(previous)) targetSelect.value = previous;
  }

  // Etykiety celów mogły się dopiero pojawić — lista pokazuje wtedy nazwy zamiast UUID-ów.
  if (schedules.length) renderList();
}

async function refresh() {
  const res = await fetch(API);
  if (!res.ok) throw new Error(await readError(res));
  const body = await res.json();
  schedules = Array.isArray(body.schedules) ? body.schedules : [];
  renderList();
}

// ------------------------------------------------------------------------ start

document.addEventListener(DEVICES_EVENT, (event) => renderTargets(event.detail));

syncForm();
renderTargets({ devices: [] });
renderList();

refresh().catch((err) => showError(`Nie udało się wczytać harmonogramów: ${err.message}`));

// app.js może wystartować przed nami — dociągamy listę urządzeń samodzielnie.
fetch("/api/devices")
  .then((res) => res.json())
  .then(renderTargets)
  .catch(() => {});

// `next_run` liczy serwer, więc odświeżamy co minutę, żeby podglądy nie zastygały.
setInterval(() => {
  refresh().catch(() => {});
}, 60000);
