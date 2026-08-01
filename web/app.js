let currentStatus = null;

function el(id) {
  return document.getElementById(id);
}

function clearNode(node) {
  if (!node) return;
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

function appendOption(select, value, label) {
  if (!select) return;
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label;
  select.appendChild(option);
}

function setSelectOptions(selectId, items, selectedValue, formatter, emptyLabel = "Not Set") {
  const select = el(selectId);
  if (!select) return;
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
  if (!box) return;
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
  if (!list) return;
  clearNode(list);
  clearNode(eventSelect);

  if (!devices?.length) {
    const empty = document.createElement("p");
    empty.className = "feedback";
    empty.textContent = "No devices reported by the runtime.";
    list.appendChild(empty);
    if (eventSelect) appendOption(eventSelect, "", "No devices");
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

    if (eventSelect) appendOption(eventSelect, device.identifier, `${device.name} (${device.identifier})`);
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
  const message = error?.message || "Failed to load runtime status";
  setFeedback("formFeedback", message, "error");
  setFeedback("settingsFeedback", message, "error");
  el("helloStatus").textContent = "offline";
  window.alert(`Failed to load runtime status: ${message}`);
});

function deviceTypeIcon(type) {
  if (type === "printer") return "PR";
  if (type === "scale") return "SC";
  return "IO";
}

async function loadScaleConfig() {
  try {
    const cfg = await getJSON("/api/scale/config");
    el("scaleBrand").value = cfg.brand || "zfoc";
    el("scaleBaudrate").value = cfg.baudrate || 9600;
    el("scaleTimeout").value = cfg.timeout || 1.2;
    const sseToggle = el("scaleSseEnabled");
    if (sseToggle) sseToggle.checked = !!cfg.sse_enabled;
    el("scaleMonitorStatus").textContent = cfg.is_monitor_running ? "监控中" : "未运行";
    el("scaleMonitorStatus").style.color = cfg.is_monitor_running ? "#10B981" : "#94A3B8";
    // 仅当配置开启 SSE 时才启动实时推送流，否则保持按需读取
    if (cfg.sse_enabled) {
      startScaleStream();
    } else {
      stopScaleStream();
    }
    return cfg;
  } catch (_) {
    return null;
  }
}

async function loadScalePorts() {
  try {
    const result = await getJSON("/api/scale/ports");
    const select = el("scalePort");
    clearNode(select);
    appendOption(select, "", "请选择串口");
    for (const p of result.ports || []) {
      appendOption(select, p.device, `${p.device} - ${p.description || ""}`);
    }
    return result.ports;
  } catch (_) {
    return [];
  }
}

async function refreshScaleWeight() {
  try {
    const result = await getJSON("/api/scale/weight");
    if (result.status === "success") {
      el("scaleWeight").textContent = `${result.weight_kg?.toFixed(3)} kg`;
      el("scaleWeight").style.color = "#10B981";
    } else {
      el("scaleWeight").textContent = result.message || "--";
      el("scaleWeight").style.color = "#94A3B8";
    }
  } catch (error) {
    el("scaleWeight").textContent = error.message || "--";
    el("scaleWeight").style.color = "#EF4444";
  }
}

// 实时电子秤重量推送（SSE）
// 重量变化超过阈值时由服务端主动推送，替代旧的 2 秒轮询
let scaleEventSource = null;
let scaleReconnectTimer = null;
let scalePollingFallback = null;

function updateScaleWeightUI(weightKg, stable) {
  el("scaleWeight").textContent = `${Number(weightKg).toFixed(3)} kg`;
  el("scaleWeight").style.color = "#10B981";
  const statusEl = el("scaleMonitorStatus");
  if (statusEl) {
    statusEl.textContent = stable ? "实时稳定" : "实时监控中";
    statusEl.style.color = "#10B981";
  }
}

function startScaleStream() {
  if (typeof EventSource === "undefined") {
    // 浏览器不支持 SSE，降级到 2 秒轮询
    console.warn("EventSource not supported, falling back to polling");
    if (!scalePollingFallback) {
      scalePollingFallback = setInterval(refreshScaleWeight, 2000);
    }
    return;
  }
  if (scaleEventSource) {
    scaleEventSource.close();
  }
  try {
    scaleEventSource = new EventSource("/api/scale/stream");
  } catch (error) {
    console.warn("Scale EventSource init failed, retry in 5s:", error);
    scheduleScaleReconnect();
    return;
  }
  scaleEventSource.onopen = function () {
    const statusEl = el("scaleMonitorStatus");
    if (statusEl) {
      statusEl.textContent = "实时连接已建立";
      statusEl.style.color = "#10B981";
    }
  };
  scaleEventSource.onmessage = function (event) {
    try {
      const data = JSON.parse(event.data);
      if (data && data.weight_kg !== undefined && data.weight_kg !== null) {
        updateScaleWeightUI(data.weight_kg, data.stable);
      }
    } catch (err) {
      console.warn("Scale stream parse error:", err);
    }
  };
  scaleEventSource.onerror = function () {
    el("scaleWeight").style.color = "#94A3B8";
    const statusEl = el("scaleMonitorStatus");
    if (statusEl) {
      statusEl.textContent = "实时连接断开，重连中...";
      statusEl.style.color = "#F59E0B";
    }
    if (scaleEventSource) {
      scaleEventSource.close();
      scaleEventSource = null;
    }
    scheduleScaleReconnect();
  };
}

function scheduleScaleReconnect() {
  if (scaleReconnectTimer) return;
  scaleReconnectTimer = setTimeout(() => {
    scaleReconnectTimer = null;
    startScaleStream();
  }, 5000);
}

function stopScaleStream() {
  if (scaleReconnectTimer) {
    clearTimeout(scaleReconnectTimer);
    scaleReconnectTimer = null;
  }
  if (scaleEventSource) {
    scaleEventSource.close();
    scaleEventSource = null;
  }
  if (scalePollingFallback) {
    clearInterval(scalePollingFallback);
    scalePollingFallback = null;
  }
}

async function saveScaleConfig() {
  const sseToggle = el("scaleSseEnabled");
  const payload = {
    scale_brand: el("scaleBrand").value,
    scale_port: el("scalePort").value,
    scale_baudrate: parseInt(el("scaleBaudrate").value) || 9600,
    scale_timeout: parseFloat(el("scaleTimeout").value) || 1.2,
    scale_sse_enabled: sseToggle ? !!sseToggle.checked : false,
  };
  await getJSON("/api/scale/save_config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  setFeedback("scaleFeedback", "电子秤配置已保存", "success");
  await loadScaleConfig();
}

loadScaleConfig();
loadScalePorts();

el("scaleForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  setFeedback("scaleFeedback", "正在保存...", "warn");
  try {
    await saveScaleConfig();
    await refreshScaleWeight();
  } catch (error) {
    setFeedback("scaleFeedback", error.message || "保存失败", "error");
  }
});

el("scaleRefresh").addEventListener("click", async () => {
  setFeedback("scaleFeedback", "正在刷新...", "warn");
  try {
    await loadScalePorts();
    const result = await getJSON("/api/scale/refresh", { method: "POST" });
    setFeedback("scaleFeedback", `监控${result.is_running ? "已启动" : "未启动"}`, "success");
    await loadScaleConfig();
  } catch (error) {
    setFeedback("scaleFeedback", error.message || "刷新失败", "error");
  }
});

el("scaleReadWeight").addEventListener("click", refreshScaleWeight);

// 电子秤默认按需读取（POS 模式）；仅当用户在配置中开启"实时推送"时
// 才建立 SSE 连接。loadScaleConfig() 会根据配置自动启停 SSE 流。
// 页面隐藏时暂停 SSE，可见时恢复（仅当 SSE 已启用时生效）
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopScaleStream();
  } else {
    // 仅在配置已开启 SSE 时恢复，否则保持按需读取
    if (scaleEventSource || scalePollingFallback) {
      startScaleStream();
    }
  }
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

// ── Visual ESC/POS receipt editor ───────────────────────────────────

let receiptTemplate = null;
let selectedReceiptBlockId = null;
let draggedReceiptBlockId = null;
let receiptHistory = [];
let receiptPreviewTimer = null;

function cloneReceiptTemplate(value) {
  return JSON.parse(JSON.stringify(value));
}

function setReceiptDirty(dirty) {
  const chip = el("receiptSaveState");
  chip.textContent = dirty ? "有未保存修改" : "已保存";
  chip.className = `state-chip ${dirty ? "state-chip-warn" : "state-chip-ok"}`;
}

function rememberReceiptTemplate() {
  if (!receiptTemplate) return;
  receiptHistory.push(cloneReceiptTemplate(receiptTemplate));
  if (receiptHistory.length > 30) receiptHistory.shift();
  el("receiptUndo").disabled = receiptHistory.length === 0;
}

function receiptBlockById(blockId) {
  return receiptTemplate?.blocks?.find((block) => block.id === blockId) || null;
}

function receiptProductHeaderBlock() {
  return receiptBlockById("product_header");
}

function renderReceiptBlockList() {
  const list = el("receiptBlockList");
  clearNode(list);
  for (const block of receiptTemplate?.blocks || []) {
    const item = document.createElement("article");
    item.className = "receipt-block-item";
    if (!block.enabled) item.classList.add("is-disabled");
    if (block.id === selectedReceiptBlockId) item.classList.add("is-selected");
    item.dataset.blockId = block.id;
    item.draggable = true;

    const handle = document.createElement("span");
    handle.className = "receipt-drag-handle";
    handle.textContent = "⋮⋮";
    handle.title = "拖动排序";

    const name = document.createElement("span");
    name.className = "receipt-block-name";
    name.textContent = block.label;

    const visibility = document.createElement("button");
    visibility.type = "button";
    visibility.className = "receipt-visibility";
    visibility.textContent = block.enabled ? "显示" : "隐藏";
    visibility.title = block.enabled ? "点击隐藏" : "点击显示";
    visibility.addEventListener("click", (event) => {
      event.stopPropagation();
      rememberReceiptTemplate();
      block.enabled = !block.enabled;
      receiptTemplateChanged();
    });

    item.append(handle, name, visibility);
    item.addEventListener("click", () => selectReceiptBlock(block.id));
    item.addEventListener("dragstart", (event) => {
      draggedReceiptBlockId = block.id;
      item.classList.add("is-dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", block.id);
    });
    item.addEventListener("dragend", () => {
      draggedReceiptBlockId = null;
      item.classList.remove("is-dragging");
    });
    item.addEventListener("dragover", (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
    });
    item.addEventListener("drop", (event) => {
      event.preventDefault();
      const sourceId = draggedReceiptBlockId || event.dataTransfer.getData("text/plain");
      if (!sourceId || sourceId === block.id) return;
      const sourceIndex = receiptTemplate.blocks.findIndex((entry) => entry.id === sourceId);
      const targetIndex = receiptTemplate.blocks.findIndex((entry) => entry.id === block.id);
      if (sourceIndex < 0 || targetIndex < 0) return;
      rememberReceiptTemplate();
      const [moved] = receiptTemplate.blocks.splice(sourceIndex, 1);
      receiptTemplate.blocks.splice(targetIndex, 0, moved);
      receiptTemplateChanged();
    });
    list.appendChild(item);
  }
}

function selectReceiptBlock(blockId) {
  selectedReceiptBlockId = blockId;
  const block = receiptBlockById(blockId);
  el("receiptInspectorEmpty").classList.toggle("hidden", Boolean(block));
  el("receiptInspectorForm").classList.toggle("hidden", !block);
  if (block) {
    const kind = block.kind || "builtin";
    const canOverrideContent = kind === "builtin" && ["company", "invoice", "order_info", "product_header", "footer"].includes(block.id);
    el("receiptInspectorHint").textContent = block.label;
    el("receiptBlockEnabled").checked = Boolean(block.enabled);
    el("receiptBlockAlign").value = block.align || "inherit";
    el("receiptBlockBold").value = String(block.bold ?? "inherit");
    el("receiptHorizontalOffsetRange").value = Number(block.horizontal_offset || 0);
    el("receiptHorizontalOffset").value = Number(block.horizontal_offset || 0);
    el("receiptBlockSpacing").value = Number(block.spacing_after || 0);
    el("receiptBlockContentField").classList.toggle("hidden", !canOverrideContent);
    const usesProductColumns = ["product_header", "products"].includes(block.id);
    const headerBlock = receiptProductHeaderBlock() || block;
    el("receiptHorizontalOffsetField").classList.toggle("hidden", usesProductColumns);
    el("receiptProductHeaderFields").classList.toggle("hidden", !usesProductColumns);
    el("receiptCustomTextField").classList.toggle("hidden", kind !== "text");
    el("receiptSeparatorField").classList.toggle("hidden", kind !== "separator");
    el("receiptSpacerField").classList.toggle("hidden", kind !== "spacer");
    el("receiptDoubleSizeField").classList.toggle("hidden", kind !== "text");
    el("receiptDeleteBlock").classList.toggle("hidden", kind === "builtin");
    el("receiptBlockContent").value = block.content || "";
    el("receiptQtyLabel").value = headerBlock.qty_label || "Uds.";
    el("receiptProductLabel").value = headerBlock.product_label || "Producto";
    el("receiptAmountLabel").value = headerBlock.amount_label || "Importe";
    el("receiptQtyColumns").value = Number(headerBlock.qty_columns || 6);
    el("receiptAmountColumns").value = Number(headerBlock.amount_columns || 10);
    el("receiptProductColumns").value = Number(headerBlock.product_columns || 28);
    el("receiptColumnGutter").value = Math.max(
      0,
      48 - Number(headerBlock.qty_columns || 6)
        - Number(headerBlock.product_columns || 28)
        - Number(headerBlock.amount_columns || 10)
    );
    el("receiptCustomText").value = block.text || "";
    el("receiptSeparatorCharacter").value = block.character || "-";
    el("receiptSpacerLines").value = Number(block.lines || 1);
    el("receiptDoubleSize").checked = Boolean(block.double_size);
  }
  renderReceiptBlockList();
}

function receiptTemplateChanged() {
  setReceiptDirty(true);
  el("receiptUndo").disabled = receiptHistory.length === 0;
  renderReceiptBlockList();
  if (selectedReceiptBlockId) selectReceiptBlock(selectedReceiptBlockId);
  scheduleReceiptPreview();
}

function updateSelectedReceiptBlock(field, value) {
  const block = receiptBlockById(selectedReceiptBlockId);
  if (!block || block[field] === value) return;
  rememberReceiptTemplate();
  block[field] = value;
  receiptTemplateChanged();
}

function addReceiptBlock(kind) {
  const suffix = `${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
  const defaults = {
    text: {label: "自定义文字", text: "在这里输入文字", align: "center", double_size: false},
    separator: {label: "自定义分隔线", character: "-", align: "left"},
    spacer: {label: "自定义空行", lines: 1, align: "left"},
  };
  rememberReceiptTemplate();
  const block = {
    id: `custom_${suffix}`,
    kind,
    enabled: true,
    bold: "inherit",
    horizontal_offset: 0,
    spacing_after: 0,
    ...defaults[kind],
  };
  const selectedIndex = receiptTemplate.blocks.findIndex((item) => item.id === selectedReceiptBlockId);
  receiptTemplate.blocks.splice(selectedIndex >= 0 ? selectedIndex + 1 : receiptTemplate.blocks.length, 0, block);
  selectedReceiptBlockId = block.id;
  receiptTemplateChanged();
  el(kind === "text" ? "receiptCustomText" : kind === "separator" ? "receiptSeparatorCharacter" : "receiptSpacerLines").focus();
}

function captureTextEditHistory(event) {
  if (event.target.dataset.historyCaptured === "yes") return;
  rememberReceiptTemplate();
  event.target.dataset.historyCaptured = "yes";
}

function finishTextEditHistory(event) {
  delete event.target.dataset.historyCaptured;
}

function scheduleReceiptPreview() {
  window.clearTimeout(receiptPreviewTimer);
  receiptPreviewTimer = window.setTimeout(previewReceiptTemplate, 120);
}

function receiptLineText(line, width) {
  if (line.type === "image") return `[ 图片 · ${line.image_kind || "image"} ]`;
  if (line.type === "header_meta_line") {
    const left = String(line.left_text || "");
    const right = String(line.right_text || "");
    return `${left}${" ".repeat(Math.max(1, width - left.length - right.length))}${right}`;
  }
  if (line.type === "product_line") {
    const left = `${line.qty || ""} x ${line.name || ""}`.trim();
    const right = String(line.total || "");
    return `${left}${" ".repeat(Math.max(1, width - left.length - right.length))}${right}`;
  }
  return String(line.text || "");
}

function renderReceiptPreview(lines, width) {
  const paper = el("receiptPaper");
  clearNode(paper);
  paper.style.setProperty("--paper-chars", width);
  el("receiptPreviewWidth").textContent = `${width} 字符`;
  for (const line of lines || []) {
    const row = document.createElement("div");
    row.className = "receipt-preview-line";
    if (line.align === "center") row.classList.add("align-center");
    if (line.align === "right") row.classList.add("align-right");
    if (line.bold) row.classList.add("is-bold");
    if (line.double_width || line.double_height) row.classList.add("is-double");
    if (line.type === "image") row.classList.add("is-image");
    row.textContent = receiptLineText(line, width) || " ";
    paper.appendChild(row);
    for (const option of line.combo_items || []) {
      const optionRow = document.createElement("div");
      optionRow.className = "receipt-preview-line";
      optionRow.textContent = `  + ${option}`;
      paper.appendChild(optionRow);
    }
  }
}

async function previewReceiptTemplate() {
  if (!receiptTemplate) return;
  const requested = cloneReceiptTemplate(receiptTemplate);
  requested.name = el("receiptTemplateName").value.trim() || "自定义小票";
  requested.paper_width = Number(el("receiptPaperWidth").value) || 48;
  try {
    const result = await getJSON("/api/receipt-template/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requested),
    });
    renderReceiptPreview(result.lines, requested.paper_width);
    setFeedback("receiptFeedback", "预览已更新。", "success");
  } catch (error) {
    setFeedback("receiptFeedback", error.message || "预览失败", "error");
  }
}

async function loadReceiptEditor() {
  try {
    const result = await getJSON("/api/receipt-template");
    receiptTemplate = result.template;
    receiptHistory = [];
    el("receiptTemplateName").value = receiptTemplate.name;
    el("receiptPaperWidth").value = String(receiptTemplate.paper_width);
    selectedReceiptBlockId = receiptTemplate.blocks?.[0]?.id || null;
    selectReceiptBlock(selectedReceiptBlockId);
    setReceiptDirty(false);
    el("receiptUndo").disabled = true;
    await previewReceiptTemplate();
  } catch (error) {
    setFeedback("receiptFeedback", error.message || "无法加载小票模板", "error");
  }
}

el("receiptBlockEnabled").addEventListener("change", (event) => {
  updateSelectedReceiptBlock("enabled", event.target.checked);
});
el("receiptBlockAlign").addEventListener("change", (event) => {
  updateSelectedReceiptBlock("align", event.target.value);
});
el("receiptBlockBold").addEventListener("change", (event) => {
  const value = event.target.value === "inherit" ? "inherit" : event.target.value === "true";
  updateSelectedReceiptBlock("bold", value);
});
function updateReceiptHorizontalOffset(value) {
  const offset = Math.max(-12, Math.min(12, Number(value) || 0));
  el("receiptHorizontalOffsetRange").value = offset;
  el("receiptHorizontalOffset").value = offset;
  updateSelectedReceiptBlock("horizontal_offset", offset);
}
el("receiptHorizontalOffsetRange").addEventListener("change", (event) => {
  updateReceiptHorizontalOffset(event.target.value);
});
el("receiptHorizontalOffset").addEventListener("change", (event) => {
  updateReceiptHorizontalOffset(event.target.value);
});
el("receiptBlockSpacing").addEventListener("change", (event) => {
  updateSelectedReceiptBlock("spacing_after", Math.max(0, Math.min(4, Number(event.target.value) || 0)));
});
el("receiptBlockContent").addEventListener("input", (event) => {
  const block = receiptBlockById(selectedReceiptBlockId);
  if (!block) return;
  captureTextEditHistory(event);
  block.content = event.target.value;
  setReceiptDirty(true);
  scheduleReceiptPreview();
});
el("receiptBlockContent").addEventListener("blur", finishTextEditHistory);
function updateProductHeaderColumns(changedField, rawValue) {
  const block = receiptProductHeaderBlock();
  if (!block || !["product_header", "products"].includes(selectedReceiptBlockId)) return;
  rememberReceiptTemplate();
  let qty = Math.max(5, Math.min(12, Number(block.qty_columns || 6)));
  let product = Math.max(12, Math.min(32, Number(block.product_columns || 28)));
  let amount = Math.max(8, Math.min(16, Number(block.amount_columns || 10)));
  if (changedField === "qty_columns") qty = Math.max(5, Math.min(12, Number(rawValue) || 6));
  if (changedField === "product_columns") product = Math.max(12, Math.min(32, Number(rawValue) || 28));
  if (changedField === "amount_columns") amount = Math.max(8, Math.min(16, Number(rawValue) || 10));
  product = Math.min(product, 48 - qty - amount);
  block.qty_columns = qty;
  block.product_columns = product;
  block.amount_columns = amount;
  block.gutter_columns = 48 - qty - product - amount;
  receiptTemplateChanged();
}
el("receiptQtyColumns").addEventListener("change", (event) => {
  updateProductHeaderColumns("qty_columns", event.target.value);
});
el("receiptAmountColumns").addEventListener("change", (event) => {
  updateProductHeaderColumns("amount_columns", event.target.value);
});
el("receiptProductColumns").addEventListener("change", (event) => {
  updateProductHeaderColumns("product_columns", event.target.value);
});
for (const [elementId, field] of [
  ["receiptQtyLabel", "qty_label"],
  ["receiptProductLabel", "product_label"],
  ["receiptAmountLabel", "amount_label"],
]) {
  el(elementId).addEventListener("input", (event) => {
    const block = receiptProductHeaderBlock();
    if (!block || !["product_header", "products"].includes(selectedReceiptBlockId)) return;
    captureTextEditHistory(event);
    block[field] = event.target.value;
    setReceiptDirty(true);
    scheduleReceiptPreview();
  });
  el(elementId).addEventListener("blur", finishTextEditHistory);
}
el("receiptCustomText").addEventListener("input", (event) => {
  const block = receiptBlockById(selectedReceiptBlockId);
  if (!block) return;
  captureTextEditHistory(event);
  block.text = event.target.value;
  block.label = event.target.value.split("\n").find((line) => line.trim())?.trim().slice(0, 24) || "自定义文字";
  setReceiptDirty(true);
  renderReceiptBlockList();
  scheduleReceiptPreview();
});
el("receiptCustomText").addEventListener("blur", finishTextEditHistory);
el("receiptSeparatorCharacter").addEventListener("change", (event) => {
  updateSelectedReceiptBlock("character", event.target.value);
});
el("receiptSpacerLines").addEventListener("change", (event) => {
  updateSelectedReceiptBlock("lines", Math.max(1, Math.min(6, Number(event.target.value) || 1)));
});
el("receiptDoubleSize").addEventListener("change", (event) => {
  updateSelectedReceiptBlock("double_size", event.target.checked);
});
el("receiptAddText").addEventListener("click", () => addReceiptBlock("text"));
el("receiptAddSeparator").addEventListener("click", () => addReceiptBlock("separator"));
el("receiptAddSpacer").addEventListener("click", () => addReceiptBlock("spacer"));
el("receiptDeleteBlock").addEventListener("click", () => {
  const index = receiptTemplate.blocks.findIndex((block) => block.id === selectedReceiptBlockId);
  if (index < 0 || receiptTemplate.blocks[index].kind === "builtin") return;
  rememberReceiptTemplate();
  receiptTemplate.blocks.splice(index, 1);
  selectedReceiptBlockId = receiptTemplate.blocks[Math.min(index, receiptTemplate.blocks.length - 1)]?.id || null;
  receiptTemplateChanged();
});
el("receiptTemplateName").addEventListener("change", () => {
  const value = el("receiptTemplateName").value.trim() || "自定义小票";
  if (receiptTemplate.name === value) return;
  rememberReceiptTemplate();
  receiptTemplate.name = value;
  receiptTemplateChanged();
});
el("receiptPaperWidth").addEventListener("change", () => {
  const value = Number(el("receiptPaperWidth").value) || 48;
  if (receiptTemplate.paper_width === value) return;
  rememberReceiptTemplate();
  receiptTemplate.paper_width = value;
  receiptTemplateChanged();
});
el("receiptUndo").addEventListener("click", () => {
  const previous = receiptHistory.pop();
  if (!previous) return;
  receiptTemplate = previous;
  el("receiptTemplateName").value = receiptTemplate.name;
  el("receiptPaperWidth").value = String(receiptTemplate.paper_width);
  receiptTemplateChanged();
  el("receiptUndo").disabled = receiptHistory.length === 0;
});
el("receiptSave").addEventListener("click", async () => {
  receiptTemplate.name = el("receiptTemplateName").value.trim() || "自定义小票";
  receiptTemplate.paper_width = Number(el("receiptPaperWidth").value) || 48;
  setFeedback("receiptFeedback", "正在保存模板…", "warn");
  try {
    const result = await getJSON("/api/receipt-template", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(receiptTemplate),
    });
    receiptTemplate = result.template;
    receiptHistory = [];
    el("receiptUndo").disabled = true;
    setReceiptDirty(false);
    setFeedback("receiptFeedback", "模板已保存，下一张小票会使用新布局。", "success");
  } catch (error) {
    setFeedback("receiptFeedback", error.message || "保存失败", "error");
  }
});
el("receiptReset").addEventListener("click", async () => {
  if (!window.confirm("恢复默认小票布局？当前已保存的模板会被替换。")) return;
  rememberReceiptTemplate();
  try {
    const result = await getJSON("/api/receipt-template", { method: "DELETE" });
    receiptTemplate = result.template;
    el("receiptTemplateName").value = receiptTemplate.name;
    el("receiptPaperWidth").value = String(receiptTemplate.paper_width);
    selectedReceiptBlockId = receiptTemplate.blocks?.[0]?.id || null;
    selectReceiptBlock(selectedReceiptBlockId);
    setReceiptDirty(false);
    await previewReceiptTemplate();
    setFeedback("receiptFeedback", "已恢复默认模板。", "success");
  } catch (error) {
    setFeedback("receiptFeedback", error.message || "恢复失败", "error");
  }
});

loadReceiptEditor();
