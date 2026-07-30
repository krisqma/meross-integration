// Panel urządzeń: strumień SSE -> kafelki, przełącznik -> POST /api/devices/.../power.
// Bez build-stepu i bez zależności. Renderowanie jest kluczowane po (dev, node), więc
// przyjście nowej migawki nie kasuje elementów i nie gubi stanu przełącznika.

const DEVICES_EVENT = "gniazdka:devices";

// Most odpytuje gniazdka co ~20-30 s, więc po wysłaniu polecenia prawdziwy stan wraca
// z opóźnieniem. Do tego czasu pokazujemy stan życzeniowy, żeby przełącznik nie odskakiwał.
const PENDING_MS = 45000;

const STATE_LABELS = {
  ready: { text: "online", cls: "badge-ok" },
  init: { text: "inicjalizacja", cls: "badge-warn" },
  lost: { text: "brak łączności", cls: "badge-bad" },
  disconnected: { text: "rozłączone", cls: "badge-bad" },
  sleeping: { text: "uśpione", cls: "badge-warn" },
  alert: { text: "alarm", cls: "badge-bad" },
};

const devicesEl = document.getElementById("devices");
const summaryEl = document.getElementById("devices-summary");
const connectionEl = document.getElementById("connection");

/** devId -> { root, signature, channels: Map<node, refs> } */
const tiles = new Map();
/** "dev|node" -> { desired: boolean, until: number } */
const pending = new Map();

let lastSnapshot = { devices: [] };

// ------------------------------------------------------------------- pomocnicze

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function fmt(value, digits) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return Number(value).toFixed(digits);
}

function channelLabel(channel) {
  if (channel.name) return channel.name;
  const dash = channel.node.lastIndexOf("-");
  if (dash > 0) {
    const suffix = channel.node.slice(dash + 1);
    if (/^\d+$/.test(suffix)) return `Gniazdo ${Number(suffix) + 1}`;
  }
  return "Gniazdo 1";
}

function setBadge(node, text, cls) {
  node.className = `badge ${cls}`;
  node.textContent = text;
}

function isOffline(state) {
  return state !== "ready";
}

// ------------------------------------------------------------------- połączenie

function setConnection(text, cls) {
  setBadge(connectionEl, text, cls);
}

// ---------------------------------------------------------------------- kafelki

function buildChannel(devId, channel) {
  const wrap = el("div", "channel");

  const head = el("div", "row channel-head");
  const nameCol = el("div", "col");
  const name = el("span", "channel-name", channelLabel(channel));
  const hint = el("span", "muted", "");
  nameCol.append(name, hint);

  const label = el("label", "switch big");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.setAttribute("aria-label", `Przełącz ${channelLabel(channel)}`);
  label.append(input, el("span", "slider"));

  head.append(nameCol, label);

  const metrics = el("div", "metrics");
  const refs = { wrap, name, hint, input, metrics, cells: {} };
  for (const [key, unit, digits] of [
    ["power_w", "W", 1],
    ["voltage_v", "V", 0],
    ["current_a", "A", 2],
    ["energy_today_kwh", "kWh dziś", 3],
  ]) {
    const cell = el("div", "metric");
    const value = el("span", "metric-value", "—");
    cell.append(value, el("span", "metric-label", unit));
    metrics.append(cell);
    refs.cells[key] = { cell, value, digits };
  }

  const noMetrics = el("p", "muted", "Bez pomiaru energii");
  refs.noMetrics = noMetrics;
  wrap.append(head, metrics, noMetrics);

  input.addEventListener("change", () => {
    sendPower(devId, channel.node, input.checked, refs);
  });

  return refs;
}

function buildTile(device, signature) {
  const root = el("article", "card device");

  const head = el("div", "row device-head");
  const name = el("h3", null, device.name || device.id);
  const state = el("span", "badge");
  head.append(name, state);

  const meta1 = el("p", "muted device-meta", "");
  const meta2 = el("p", "muted device-meta", "");

  root.append(head, meta1, meta2);

  const channels = new Map();
  for (const channel of device.channels) {
    const refs = buildChannel(device.id, channel);
    channels.set(channel.node, refs);
    root.append(refs.wrap);
  }
  if (device.channels.length === 0) {
    root.append(el("p", "muted", "Urządzenie nie zgłosiło żadnego gniazda."));
  }

  return { root, signature, name, state, meta1, meta2, channels };
}

function updateChannel(devId, refs, channel) {
  const key = `${devId}|${channel.node}`;
  const wait = pending.get(key);
  let shown = channel.on;

  if (wait) {
    if (channel.on === wait.desired || Date.now() > wait.until) {
      pending.delete(key);
    } else {
      shown = wait.desired;
    }
  }

  refs.input.checked = shown === true;
  refs.input.indeterminate = shown === null || shown === undefined;
  refs.input.disabled = channel.settable === false;

  if (pending.has(key)) {
    refs.hint.textContent = "wysłano, czekam na potwierdzenie…";
  } else if (channel.settable === false) {
    refs.hint.textContent = "tylko podgląd (niesterowalne)";
  } else if (shown === null || shown === undefined) {
    refs.hint.textContent = "stan nieznany";
  } else {
    refs.hint.textContent = shown ? "włączone" : "wyłączone";
  }

  let hasAny = false;
  for (const [key2, cell] of Object.entries(refs.cells)) {
    const value = channel[key2];
    const present = value !== null && value !== undefined;
    cell.cell.classList.toggle("hidden", !present);
    cell.value.textContent = fmt(value, cell.digits);
    if (present) hasAny = true;
  }
  refs.metrics.classList.toggle("hidden", !hasAny);
  refs.noMetrics.classList.toggle("hidden", hasAny);
}

function updateTile(tile, device) {
  tile.name.textContent = device.name || device.id;
  const label = STATE_LABELS[device.state] || { text: device.state || "nieznany", cls: "badge-warn" };
  setBadge(tile.state, label.text, label.cls);
  tile.root.classList.toggle("offline", isOffline(device.state));

  const line1 = [device.ip, device.mac].filter(Boolean).join(" · ");
  const line2 = [device.model, device.fw ? `firmware ${device.fw}` : null].filter(Boolean).join(" · ");
  tile.meta1.textContent = line1 || device.id;
  tile.meta2.textContent = line2 || "";

  for (const channel of device.channels) {
    const refs = tile.channels.get(channel.node);
    if (refs) updateChannel(device.id, refs, channel);
  }
}

function render(snapshot) {
  const devices = Array.isArray(snapshot.devices) ? snapshot.devices : [];
  const seen = new Set();

  for (const device of devices) {
    seen.add(device.id);
    const signature = device.channels.map((c) => c.node).join(",");
    let tile = tiles.get(device.id);
    if (!tile || tile.signature !== signature) {
      // Zmieniła się lista kanałów (nowe urządzenie albo most dopiero dosypał węzły).
      const fresh = buildTile(device, signature);
      if (tile) tile.root.remove();
      tiles.set(device.id, fresh);
      tile = fresh;
    }
    updateTile(tile, device);
    devicesEl.append(tile.root); // appendChild przenosi istniejący element, nie tworzy go od nowa
  }

  for (const [devId, tile] of tiles) {
    if (!seen.has(devId)) {
      tile.root.remove();
      tiles.delete(devId);
    }
  }

  const empty = devicesEl.querySelector(".empty");
  if (devices.length === 0) {
    if (!empty) {
      devicesEl.append(
        el(
          "p",
          "empty",
          "Brak urządzeń. Sprawdź, czy most meross2mqtt jest podniesiony i publikuje tematy homie/#.",
        ),
      );
    }
  } else if (empty) {
    empty.remove();
  }

  const channels = devices.reduce((sum, d) => sum + d.channels.length, 0);
  summaryEl.textContent = devices.length
    ? `${devices.length} urządz. · ${channels} gniazd`
    : "";
}

// -------------------------------------------------------------------- polecenia

async function sendPower(dev, node, on, refs) {
  const key = `${dev}|${node}`;
  pending.set(key, { desired: on, until: Date.now() + PENDING_MS });
  refs.hint.textContent = "wysyłam…";
  refs.input.disabled = true;
  try {
    const res = await fetch(`/api/devices/${encodeURIComponent(dev)}/${encodeURIComponent(node)}/power`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    refs.hint.textContent = "wysłano, czekam na potwierdzenie…";
  } catch (err) {
    pending.delete(key);
    refs.hint.textContent = `nie udało się wysłać (${err.message})`;
    refs.input.checked = !on;
  } finally {
    refs.input.disabled = false;
  }
}

// ------------------------------------------------------------------------- SSE

function apply(snapshot) {
  lastSnapshot = snapshot;
  render(snapshot);
  // Moduł harmonogramów potrzebuje listy urządzeń do selectów, ale nie ma dotykać
  // naszego DOM-u — dostaje ją zdarzeniem.
  document.dispatchEvent(new CustomEvent(DEVICES_EVENT, { detail: snapshot }));
}

function connect() {
  const source = new EventSource("/api/stream");

  source.addEventListener("open", () => {
    setConnection("Na żywo", "badge-ok");
  });

  source.addEventListener("state", (event) => {
    try {
      apply(JSON.parse(event.data));
      setConnection("Na żywo", "badge-ok");
    } catch (err) {
      console.error("Nie udało się sparsować zdarzenia SSE", err);
    }
  });

  source.addEventListener("error", () => {
    // EventSource sam ponawia połączenie; pokazujemy tylko, że dane są nieaktualne.
    if (source.readyState === EventSource.CLOSED) {
      setConnection("Rozłączono — ponawiam…", "badge-bad");
      setTimeout(connect, 3000);
    } else {
      setConnection("Brak połączenia — ponawiam…", "badge-bad");
    }
  });
}

// Pierwsza migawka po HTTP: panel pokazuje dane, nawet gdy SSE dopiero się podnosi.
fetch("/api/devices")
  .then((res) => res.json())
  .then(apply)
  .catch(() => render(lastSnapshot));

connect();

export { DEVICES_EVENT };
