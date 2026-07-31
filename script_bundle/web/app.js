let currentStatus = null;

function el(id) {
  return document.getElementById(id);
}

function clearNode(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

function appendOption(select, value, label) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label;
  select.appendChild(option);
}

function setSelectOptions(selectId, items, selectedValue, formatter, emptyLabel = "Not Set") {
  const select = el(selectId);
  clearNode(select);
  appendOption(select, "", emptyLabel);
  for (const item of items || []) {
    const option = document.createElement("option");
    option.value = item.value;
    option.textContent = formatter(item);
    select.appendChild(option);
  }
  select.value = selectedValue || "";
}

async function getJSON(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    throw new Error(await readError(response));
  }
  return response.json();
}

async function readError(response) {
  try {
    const payload = await response.json();
    return payload.detail || `Request failed: ${response.status}`;
  } catch (_) {
    return `Request failed: ${response.status}`;
  }
}

function renderDefinitionList(targetId, rows) {
  const box = el(targetId);
  clearNode(box);
  for (const [label, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value || "-";
    box.appendChild(dt);
    box.appendChild(dd);
  }
}

function prettyJSON(value) {
  return JSON.stringify(value, null, 2);
}

function parseEditorJSON(id, label) {
  try {
    return JSON.parse(el(id).value);
  } catch (error) {
    throw new Error(`${label} is not valid JSON: ${error.message}`);
  }
}

function deviceTypeIcon(type) {
  if (type === "printer") return "PR";
  return "IO";
}

function buildLocalUrl(ip, sslEngine) {
  if (!ip) return "";
  const scheme = sslEngine === "plain_http" ? "http" : "https";
  return `${scheme}://${ip}`;
}

function setFeedback(id, message, kind = "") {
  const node = el(id);
  node.textContent = message;
  if (kind) {
    node.dataset.kind = kind;
  } else {
    delete node.dataset.kind;
  }
}

function updateStepVisibility(connected) {
  el("stepConnect").classList.toggle("hidden", connected);
  el("stepSettings").classList.toggle("hidden", !connected);
}

function renderInfo(iot) {
  renderDefinitionList("iotInfo", [
    ["Identifier", iot?.identifier || "-"],
    ["Address", iot?.ip || "-"],
    ["Version", iot?.version || "-"],
  ]);
}

function renderConnection(connection) {
  renderDefinitionList("serverConnection", [
    ["Paired", connection?.connected ? "yes" : "no"],
    ["Cloud URL", connection?.url || "-"],
    ["Database", connection?.db_name || "-"],
    ["DB UUID", connection?.db_uuid || "-"],
    ["Last Sync", connection?.last_sync_ok ? "ok" : "pending or failed"],
    ["Message", connection?.last_sync_message || "-"],
  ]);
}

function renderCloudBridge(cloudBridge) {
  renderDefinitionList("cloudBridge", [
    ["WebSocket", cloudBridge?.connected ? "connected" : "disconnected"],
    ["Server", cloudBridge?.server_url || "-"],
    ["Channel", cloudBridge?.iot_channel || "-"],
    ["TLS Verify", cloudBridge?.ssl_verify ? "enabled" : "disabled"],
    ["Last Error", cloudBridge?.last_error || "-"],
  ]);
}

function renderCertificates(certificates) {
  const parts = [];
  if (certificates?.crt_ready) parts.push("CRT ready");
  if (certificates?.p12_ready) parts.push(`P12 password: ${certificates.password_hint || "-"}`);
  if (certificates?.startup_error) parts.push(`Startup error: ${certificates.startup_error}`);
  setFeedback("certHint", parts.join(" | ") || "Certificates unavailable", parts.length ? "success" : "");
}

function renderDevices(devices) {
  const list = el("deviceList");
  const eventSelect = el("deviceIdentifier");
  clearNode(list);
  clearNode(eventSelect);

  if (!devices?.length) {
    const empty = document.createElement("p");
    empty.className = "feedback";
    empty.textContent = "No devices reported by the runtime.";
    list.appendChild(empty);
    appendOption(eventSelect, "", "No devices");
    return;
  }

  for (const device of devices) {
    const item = document.createElement("article");
    item.className = "device-item";

    const copy = document.createElement("div");
    copy.className = "device-copy";

    const icon = document.createElement("span");
    icon.className = "device-icon";
    icon.textContent = deviceTypeIcon(device.type);
    icon.title = device.type || "device";

    const textBox = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = device.name || device.identifier;
    const primary = document.createElement("p");
    primary.textContent = `${device.identifier} | ${device.type || "unknown"} | ${device.connection || "unknown"}`;

    const meta = document.createElement("p");
    meta.className = "device-meta";
    const queue = device?.metadata?.cups_queue || device?.metadata?.queue || "";
    meta.textContent = queue
      ? `Queue: ${queue}${device?.metadata?.is_default ? " | default" : ""}`
      : `Subtype: ${device.subtype || "-"}`;

    textBox.appendChild(title);
    textBox.appendChild(primary);
    textBox.appendChild(meta);
    copy.appendChild(icon);
    copy.appendChild(textBox);

    const badge = document.createElement("span");
    badge.className = `device-state ${device.status || "unknown"}`;
    badge.textContent = device.status || "unknown";

    item.appendChild(copy);
    item.appendChild(badge);
    list.appendChild(item);

    appendOption(eventSelect, device.identifier, `${device.name} (${device.identifier})`);
  }
}

function renderRuntimeChips(status) {
  const chip = el("runtimeModeChip");
  const connected = Boolean(status?.server_connection?.connected);
  chip.textContent = connected ? "Bound" : "Local only";
  chip.className = `state-chip ${connected ? "state-chip-ok" : "state-chip-warn"}`;
}

function extractPrinterOptions(devices) {
  return (devices || [])
    .filter((device) => device.type === "printer")
    .map((device) => ({
      value: device.identifier,
      label: device.name || device.identifier,
      queue: device?.metadata?.cups_queue || device?.metadata?.queue || "",
    }));
}

function renderSettings(status) {
  currentStatus = status;
  const localConfig = status.local_config || {};
  const sslEngine = localConfig.ssl_engine || "secure_https";
  const printerOptions = extractPrinterOptions(status.devices);
  const queueOptions = printerOptions
    .filter((item) => item.queue)
    .map((item) => ({ value: item.queue, label: item.queue }));

  el("sslEngine").value = sslEngine;
  el("localUrl").value = localConfig.local_url || buildLocalUrl(status.iot?.ip, sslEngine);
  setSelectOptions(
    "printerIdentifier",
    printerOptions.map((item) => ({ value: item.value, label: item.label })),
    localConfig.printer_identifier || "",
    (item) => item.label,
    "Auto"
  );
  setSelectOptions(
    "primaryPrinterQueue",
    queueOptions,
    localConfig.primary_printer_queue || "",
    (item) => item.label,
    "Auto"
  );
  renderRuntimeChips(status);
  updateStepVisibility(Boolean(status.server_connection?.connected));
}

async function load() {
  const data = await getJSON("/api/status");
  renderInfo(data.iot);
  renderConnection(data.server_connection);
  renderCloudBridge(data.cloud_bridge);
  renderCertificates(data.certificates);
  renderDevices(data.devices);
  renderSettings(data);
  el("syncResult").textContent = data?.server_connection?.last_sync_message || "Waiting for sync";
  el("helloStatus").textContent = "online";
  el("helloStatus").classList.add("status-ok");
}

async function saveSettings() {
  const payload = {
    ssl_engine: el("sslEngine").value,
    local_url: el("localUrl").value.trim(),
    printer_identifier: el("printerIdentifier").value,
    primary_printer_queue: el("primaryPrinterQueue").value,
  };
  await getJSON("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

async function connectWithTokenUrl(tokenUrl) {
  await getJSON("/api/connect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token_url: tokenUrl }),
  });
}

async function disconnectServer() {
  await getJSON("/api/disconnect", { method: "POST" });
}

async function triggerPrintTest(url, successMessage) {
  setFeedback("printFeedback", "Submitting print job to native runtime...", "warn");
  try {
    const result = await getJSON(url, { method: "POST" });
    setFeedback(
      "printFeedback",
      result?.message || successMessage,
      result?.ok ? "success" : "error"
    );
  } catch (error) {
    console.error(error);
    setFeedback("printFeedback", error.message || "Print test failed.", "error");
  }
}

el("sslEngine").addEventListener("change", () => {
  const input = el("localUrl");
  if (!input.value.trim() && currentStatus?.iot?.ip) {
    input.value = buildLocalUrl(currentStatus.iot.ip, el("sslEngine").value);
  }
});

el("connectForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const tokenUrl = el("tokenUrl").value.trim();
  if (!tokenUrl) {
    setFeedback("formFeedback", "Token URL is required.", "error");
    return;
  }

  setFeedback("formFeedback", "Saving local settings and linking to Odoo...");
  try {
    await saveSettings();
    await connectWithTokenUrl(tokenUrl);
    await load();
    setFeedback("formFeedback", "IoT binding saved successfully.", "success");
  } catch (error) {
    console.error(error);
    setFeedback("formFeedback", error.message || "Binding failed.", "error");
  }
});

el("settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await saveSettings();
    await load();
    setFeedback("settingsFeedback", "Settings saved.", "success");
  } catch (error) {
    console.error(error);
    setFeedback("settingsFeedback", error.message || "Failed to save settings.", "error");
  }
});

el("backToConnect").addEventListener("click", () => {
  updateStepVisibility(false);
  setFeedback("formFeedback", "Paste a new token URL to pair another service.");
});

el("disconnectServer").addEventListener("click", async () => {
  const confirmed = window.confirm("Unbind the current Odoo server from this runtime?");
  if (!confirmed) return;

  try {
    await disconnectServer();
    el("tokenUrl").value = "";
    await load();
    updateStepVisibility(false);
    setFeedback("settingsFeedback", "Current server unbound.", "success");
    setFeedback("formFeedback", "Runtime is ready for a new token URL.", "success");
  } catch (error) {
    console.error(error);
    setFeedback("settingsFeedback", error.message || "Failed to unbind server.", "error");
  }
});

load().catch((error) => {
  console.error(error);
  window.alert("Failed to load runtime status");
});

const query = new URLSearchParams(window.location.search);
if (query.get("token") && query.get("db_uuid")) {
  const autoTokenUrl = `${window.location.origin}?${query.toString()}`;
  const sourceOrigin = query.get("source") || query.get("origin") || "";
  const tokenUrl =
    sourceOrigin && sourceOrigin.startsWith("http")
      ? `${sourceOrigin}?${query.toString()}`
      : autoTokenUrl;
  el("tokenUrl").value = tokenUrl;
  connectWithTokenUrl(tokenUrl)
    .then(load)
    .then(() => {
      setFeedback("formFeedback", "IoT binding saved successfully.", "success");
      updateStepVisibility(true);
    })
    .catch((error) => {
      console.error(error);
      setFeedback("formFeedback", error.message || "Auto connect failed.", "error");
    });
}
