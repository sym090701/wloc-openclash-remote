(() => {
  "use strict";

  const POLL_INTERVAL_MS = 5000;
  const MAX_DEVICES = 8;
  const DEFAULT_TARGET = Object.freeze({
    name: "东京站",
    latitude: 35.681236,
    longitude: 139.767125,
    accuracy: 25
  });
  const MAP_LAYERS = Object.freeze({
    "esri-proxy": {
      url: "/api/map-tile?source=esri&z={z}&x={x}&y={y}",
      maxZoom: 19,
      attribution: 'Tiles &copy; <a href="https://www.esri.com/">Esri</a> — Source: Esri, OpenStreetMap contributors'
    },
    esri: {
      url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
      maxZoom: 19,
      attribution: 'Tiles &copy; <a href="https://www.esri.com/">Esri</a> — Source: Esri, OpenStreetMap contributors'
    },
    osm: {
      url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    },
    "osm-proxy": {
      url: "/api/map-tile?source=osm&z={z}&x={x}&y={y}",
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    }
  });

  const appState = {
    authenticated: false,
    server: null,
    draft: null,
    baseRevision: null,
    dirty: false,
    polling: false,
    map: null,
    marker: null,
    accuracyCircle: null,
    circleRenderer: null,
    tileErrorShown: false,
    tileLayer: null,
    pollTimer: null,
    selectedDeviceId: null
  };

  const ui = {};

  class ApiError extends Error {
    constructor(message, status, payload) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.payload = payload;
    }
  }

  class InputError extends Error {
    constructor(message, element = null) {
      super(message);
      this.name = "InputError";
      this.element = element;
    }
  }

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    collectElements();
    bindEvents();
    updateConnection("connecting", "正在连接");
    void loadState({ initial: true });

    appState.pollTimer = window.setInterval(() => {
      if (appState.authenticated && !document.hidden) {
        void loadState({ preserveDraft: true, quiet: true });
      }
    }, POLL_INTERVAL_MS);
  }

  function collectElements() {
    const ids = [
      "login-view", "login-form", "login-password", "login-button", "login-error",
      "app-view", "connection-state", "service-status-dot", "service-status",
      "service-detail", "current-location-name", "current-location-coords",
      "enabled-device-count", "device-count-meta", "revision-value", "last-updated",
      "dirty-badge", "selected-device-label", "location-empty", "location-editor",
      "map", "map-layer-select", "target-form", "target-name", "target-latitude",
      "target-longitude", "target-accuracy", "target-accuracy-range", "center-map-button",
      "save-as-preset", "add-device-button", "devices-body", "devices-empty",
      "add-preset-button", "presets-list", "presets-empty", "events-list", "events-empty",
      "refresh-button", "save-summary", "save-detail", "rollback-button", "discard-button",
      "apply-button", "password-open", "logout-button", "password-dialog", "password-form",
      "password-close", "password-cancel", "current-password", "new-password",
      "confirm-password", "password-error", "password-submit", "confirm-dialog",
      "confirm-title", "confirm-message", "confirm-cancel", "confirm-accept", "toast-region",
      "device-dialog", "device-form", "device-close", "device-cancel", "device-name",
      "device-ip", "device-mac", "device-location-copy", "device-error", "device-submit"
    ];

    for (const id of ids) {
      ui[toCamel(id)] = document.getElementById(id);
    }
  }

  function bindEvents() {
    ui.loginForm.addEventListener("submit", handleLogin);
    ui.logoutButton.addEventListener("click", handleLogout);
    ui.refreshButton.addEventListener("click", () => void loadState({ preserveDraft: true }));
    ui.mapLayerSelect.addEventListener("change", () => switchMapLayer(ui.mapLayerSelect.value));

    ui.targetName.addEventListener("input", () => {
      const device = selectedDraftDevice();
      if (!device) return;
      device.location.name = ui.targetName.value;
      renderSelectedOverview();
      renderDeviceLocationSummary(device.id);
      markDirty();
    });

    ui.targetLatitude.addEventListener("input", handleCoordinateInput);
    ui.targetLongitude.addEventListener("input", handleCoordinateInput);
    ui.targetAccuracy.addEventListener("input", handleAccuracyInput);
    ui.targetAccuracyRange.addEventListener("input", () => {
      ui.targetAccuracy.value = ui.targetAccuracyRange.value;
      handleAccuracyInput();
    });

    ui.targetForm.addEventListener("submit", event => {
      event.preventDefault();
      void applyConfig();
    });

    ui.centerMapButton.addEventListener("click", () => {
      const target = targetFromInputs(false);
      if (target && appState.map) {
        updateMapTarget(target, true);
      }
    });

    ui.saveAsPreset.addEventListener("click", saveCurrentAsPreset);
    ui.addPresetButton.addEventListener("click", saveCurrentAsPreset);
    ui.addDeviceButton.addEventListener("click", openDeviceDialog);
    ui.applyButton.addEventListener("click", () => void applyConfig());
    ui.discardButton.addEventListener("click", discardChanges);
    ui.rollbackButton.addEventListener("click", () => void rollbackLatest());

    ui.passwordOpen.addEventListener("click", openPasswordDialog);
    ui.passwordClose.addEventListener("click", closePasswordDialog);
    ui.passwordCancel.addEventListener("click", closePasswordDialog);
    ui.passwordForm.addEventListener("submit", handlePasswordChange);
    ui.deviceClose.addEventListener("click", closeDeviceDialog);
    ui.deviceCancel.addEventListener("click", closeDeviceDialog);
    ui.deviceForm.addEventListener("submit", addDevice);

    window.addEventListener("online", () => {
      if (appState.authenticated) void loadState({ preserveDraft: true, quiet: true });
    });
    window.addEventListener("offline", () => updateConnection("offline", "浏览器已离线"));
  }

  async function request(path, options = {}) {
    const requestOptions = {
      method: options.method || "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...(options.headers || {})
      }
    };

    if (Object.prototype.hasOwnProperty.call(options, "body")) {
      requestOptions.headers["Content-Type"] = "application/json";
      requestOptions.body = JSON.stringify(options.body);
    }

    let response;
    try {
      response = await fetch(path, requestOptions);
    } catch (error) {
      throw new ApiError("无法连接到 WLOC 管理服务，请检查路由器网络和服务状态。", 0, error);
    }

    const contentType = response.headers.get("content-type") || "";
    let payload = null;
    if (response.status !== 204) {
      if (contentType.includes("application/json")) {
        try {
          payload = await response.json();
        } catch (_error) {
          payload = null;
        }
      } else {
        payload = await response.text();
      }
    }

    if (!response.ok) {
      throw new ApiError(apiErrorMessage(payload, response.status), response.status, payload);
    }

    return payload;
  }

  function apiErrorMessage(payload, status) {
    if (payload && typeof payload === "object") {
      if (typeof payload.error === "string" && payload.error.trim()) return payload.error;
      if (payload.error && typeof payload.error.message === "string") return payload.error.message;
      if (typeof payload.message === "string" && payload.message.trim()) return payload.message;
      if (typeof payload.detail === "string" && payload.detail.trim()) return payload.detail;
    }
    if (typeof payload === "string" && payload.trim()) return payload.trim();

    const messages = {
      400: "请求内容有误，请检查填写的信息。",
      401: "登录状态已失效，请重新登录。",
      403: "当前账号无权执行此操作。",
      404: "管理接口不存在，请确认服务版本。",
      409: "配置已被其他操作修改，请刷新后重试。",
      422: "配置校验失败，请检查坐标和设备地址。",
      429: "操作过于频繁，请稍后再试。",
      500: "路由器应用配置时发生内部错误。",
      503: "WLOC 服务暂时不可用。"
    };
    return messages[status] || `请求失败（HTTP ${status}）`;
  }

  async function loadState({ initial = false, preserveDraft = false, quiet = false } = {}) {
    if (appState.polling) return;
    appState.polling = true;
    if (!quiet && appState.authenticated) setButtonBusy(ui.refreshButton, true);

    try {
      const payload = await request("/api/state");
      receiveServerState(payload, preserveDraft);
      showApp();
      updateConnection("online", "已连接");
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        showLogin(initial ? "" : "登录状态已失效，请重新登录。");
      } else {
        updateConnection("offline", "连接中断");
        if (initial) showLogin(error.message);
        if (!quiet || initial) showToast(error.message, "error");
      }
    } finally {
      appState.polling = false;
      setButtonBusy(ui.refreshButton, false);
    }
  }

  function receiveServerState(payload, preserveDraft = false) {
    const raw = unwrapPayload(payload);
    const incoming = normalizeState(raw);
    const keepDraft = preserveDraft && appState.dirty && appState.draft;

    appState.server = incoming;
    appState.authenticated = true;

    if (!keepDraft) {
      appState.draft = cloneValue(incoming.config);
      appState.baseRevision = incoming.config.revision;
      appState.dirty = false;
      ensureSelectedDevice();
      renderEditableConfig();
    }

    renderRuntime();
    renderDirtyState();
  }

  function unwrapPayload(payload) {
    if (!payload || typeof payload !== "object") return {};
    if (payload.data && typeof payload.data === "object") return payload.data;
    if (payload.state && typeof payload.state === "object") return payload.state;
    return payload;
  }

  function normalizeState(raw) {
    const config = raw.config && typeof raw.config === "object" ? raw.config : raw;
    const targetRaw = config.target && typeof config.target === "object" ? config.target : {};
    const legacyTarget = normalizeLocation(targetRaw, DEFAULT_TARGET);
    const devices = Array.isArray(config.devices)
      ? config.devices.slice(0, MAX_DEVICES).map((device, index) => normalizeDevice(device, index, legacyTarget))
      : [];
    const presets = Array.isArray(config.presets) ? config.presets.map(normalizePreset) : [];

    return {
      config: {
        schema: 2,
        revision: config.revision ?? 0,
        updated_at: config.updated_at || config.updatedAt || null,
        devices,
        presets
      },
      service: raw.service && typeof raw.service === "object" ? raw.service : {},
      events: Array.isArray(raw.events) ? raw.events : [],
      canRollback: Boolean(raw.can_rollback ?? raw.canRollback)
    };
  }

  function normalizeLocation(value, fallback = DEFAULT_TARGET) {
    return {
      name: stringValue(value.name ?? value.label, fallback.name),
      latitude: finiteValue(value.latitude ?? value.lat, fallback.latitude),
      longitude: finiteValue(value.longitude ?? value.lng ?? value.lon, fallback.longitude),
      accuracy: finiteValue(value.accuracy, fallback.accuracy)
    };
  }

  function normalizeDevice(value, index, fallbackLocation = DEFAULT_TARGET) {
    value = value && typeof value === "object" ? value : {};
    const device = {
      id: stringValue(value.id, makeId(`device-${index}`)),
      name: stringValue(value.name, "未命名设备"),
      ip: stringValue(value.ip ?? value.address, ""),
      mac: typeof value.mac === "string" ? value.mac.trim() : "",
      enabled: value.enabled !== false,
      location: normalizeLocation(value.location && typeof value.location === "object" ? value.location : fallbackLocation, fallbackLocation)
    };
    if (Object.prototype.hasOwnProperty.call(value, "slot")) device.slot = value.slot;
    return device;
  }

  function normalizePreset(value, index) {
    value = value && typeof value === "object" ? value : {};
    const normalized = normalizeLocation(value, DEFAULT_TARGET);
    return {
      id: stringValue(value.id, makeId(`preset-${index}`)),
      ...normalized
    };
  }

  function renderRuntime() {
    if (!appState.server) return;
    const { config, service, events, canRollback } = appState.server;
    const status = serviceStatus(service.status);
    ui.serviceStatus.textContent = status.label;
    ui.serviceStatusDot.className = `status-dot ${status.className}`;

    const provider = service.provider && typeof service.provider === "object" ? service.provider : {};
    const blockedSlots = Array.isArray(service.blocked_slots) ? service.blocked_slots : [];
    if (blockedSlots.length > 0) {
      ui.serviceDetail.textContent = `路由保护性阻断：槽位 ${blockedSlots.join(", ")}`;
    } else if (provider.available === false) {
      ui.serviceDetail.textContent = provider.message || "OpenClash 规则提供器不可用";
    } else if (provider.rule_count !== undefined) {
      ui.serviceDetail.textContent = `${provider.name || "设备规则"} · ${provider.rule_count} 条`;
    } else if (service.started_at) {
      ui.serviceDetail.textContent = `启动于 ${formatDateTime(service.started_at)}`;
    } else {
      ui.serviceDetail.textContent = status.detail;
    }

    const enabledCount = config.devices.filter(device => device.enabled).length;
    ui.enabledDeviceCount.textContent = String(enabledCount);
    ui.deviceCountMeta.textContent = `共 ${config.devices.length} 台设备`;
    ui.revisionValue.textContent = `r${String(config.revision)}`;
    ui.lastUpdated.textContent = config.updated_at ? `更新于 ${formatDateTime(config.updated_at)}` : "已从路由器同步";
    ui.rollbackButton.disabled = !canRollback;

    renderSelectedOverview();
    renderEvents(events);
  }

  function renderEditableConfig() {
    if (!appState.draft) return;
    ensureSelectedDevice();
    renderDevices();
    renderPresets();
    renderSelectedDeviceEditor(false);
  }

  function ensureSelectedDevice() {
    const devices = appState.draft ? appState.draft.devices : [];
    if (!devices.some(device => device.id === appState.selectedDeviceId)) {
      appState.selectedDeviceId = devices.length ? devices[0].id : null;
    }
  }

  function selectedDraftDevice() {
    if (!appState.draft || !appState.selectedDeviceId) return null;
    return appState.draft.devices.find(device => device.id === appState.selectedDeviceId) || null;
  }

  function selectedServerDevice() {
    if (!appState.server) return null;
    return appState.server.config.devices.find(device => device.id === appState.selectedDeviceId)
      || appState.server.config.devices[0]
      || null;
  }

  function renderSelectedDeviceEditor(centerMap = false) {
    const device = selectedDraftDevice();
    const hasDevice = Boolean(device);
    ui.locationEmpty.hidden = hasDevice;
    ui.locationEditor.hidden = !hasDevice;
    ui.selectedDeviceLabel.textContent = hasDevice
      ? `正在编辑：${device.name || "未命名设备"} · ${device.ip || "尚未填写 IPv4"}`
      : "尚未选择设备";

    if (!device) {
      renderSelectedOverview();
      renderPresets();
      return;
    }

    const location = device.location;
    ui.targetName.value = location.name;
    ui.targetLatitude.value = compactNumber(location.latitude);
    ui.targetLongitude.value = compactNumber(location.longitude);
    ui.targetAccuracy.value = compactNumber(location.accuracy);
    ui.targetAccuracyRange.value = String(clamp(location.accuracy, 1, 500));
    clearTargetInvalidState();
    ensureMap();
    updateMapTarget(location, centerMap);
    window.requestAnimationFrame(() => {
      if (appState.map) appState.map.invalidateSize();
    });
    renderSelectedOverview();
  }

  function selectDevice(id, focusEditor = false) {
    if (!findDraftDevice(id)) return;
    appState.selectedDeviceId = id;
    renderDevices();
    renderPresets();
    renderSelectedDeviceEditor(true);
    if (focusEditor) {
      ui.locationEditor.closest(".panel").scrollIntoView({ behavior: "smooth", block: "start" });
      window.setTimeout(() => ui.targetName.focus(), 250);
    }
  }

  function renderSelectedOverview() {
    const device = selectedDraftDevice() || selectedServerDevice();
    if (!device) {
      ui.currentLocationName.textContent = "尚未添加设备";
      ui.currentLocationCoords.textContent = "—";
      return;
    }
    ui.currentLocationName.textContent = `${device.name || "未命名设备"} · ${device.location.name || "未命名位置"}`;
    ui.currentLocationCoords.textContent = `${formatCoordinate(device.location.latitude)}, ${formatCoordinate(device.location.longitude)} · ±${formatAccuracy(device.location.accuracy)}`;
  }

  function renderDevices() {
    ui.devicesBody.replaceChildren();
    const devices = appState.draft ? appState.draft.devices : [];
    ui.devicesEmpty.hidden = devices.length !== 0;
    ui.addDeviceButton.disabled = devices.length >= MAX_DEVICES;
    ui.addDeviceButton.title = devices.length >= MAX_DEVICES ? `最多支持 ${MAX_DEVICES} 台设备` : "";

    for (const device of devices) {
      const row = document.createElement("tr");
      row.dataset.id = device.id;
      row.classList.toggle("is-selected", device.id === appState.selectedDeviceId);
      row.setAttribute("aria-selected", String(device.id === appState.selectedDeviceId));

      const nameCell = document.createElement("td");
      const nameInput = document.createElement("input");
      nameInput.type = "text";
      nameInput.value = device.name;
      nameInput.maxLength = 64;
      nameInput.placeholder = "例如：客厅 iPad";
      nameInput.setAttribute("aria-label", "设备名称");
      nameInput.addEventListener("input", () => {
        const item = findDraftDevice(device.id);
        if (!item) return;
        item.name = nameInput.value;
        nameInput.removeAttribute("aria-invalid");
        if (item.id === appState.selectedDeviceId) {
          renderSelectedOverview();
          ui.selectedDeviceLabel.textContent = `正在编辑：${item.name || "未命名设备"} · ${item.ip || "尚未填写 IPv4"}`;
        }
        renderDeviceLocationSummary(item.id);
        markDirty();
      });
      nameCell.append(nameInput);

      const ipCell = document.createElement("td");
      const ipInput = document.createElement("input");
      ipInput.type = "text";
      ipInput.value = device.ip;
      ipInput.maxLength = 15;
      ipInput.inputMode = "decimal";
      ipInput.placeholder = "192.168.1.100";
      ipInput.className = "mono";
      ipInput.setAttribute("aria-label", "设备内网 IPv4");
      ipInput.addEventListener("input", () => {
        const item = findDraftDevice(device.id);
        if (!item) return;
        item.ip = ipInput.value.trim();
        setAriaInvalid(ipInput, ipInput.value.length > 0 && !isIPv4(ipInput.value.trim()));
        if (item.id === appState.selectedDeviceId) {
          ui.selectedDeviceLabel.textContent = `正在编辑：${item.name || "未命名设备"} · ${item.ip || "尚未填写 IPv4"}`;
        }
        markDirty();
      });
      ipCell.append(ipInput);

      const macCell = document.createElement("td");
      const macInput = document.createElement("input");
      macInput.type = "text";
      macInput.value = device.mac || "";
      macInput.maxLength = 17;
      macInput.placeholder = "02:11:22:33:44:55";
      macInput.className = "mono";
      macInput.setAttribute("aria-label", "设备固定 WLAN 地址");
      macInput.addEventListener("input", () => {
        const item = findDraftDevice(device.id);
        if (!item) return;
        item.mac = macInput.value.trim();
        setAriaInvalid(macInput, item.mac.length > 0 && !isMacAddress(item.mac));
        markDirty();
      });
      macInput.addEventListener("blur", () => {
        if (!isMacAddress(macInput.value.trim())) return;
        const item = findDraftDevice(device.id);
        if (!item) return;
        item.mac = normalizeMacAddress(macInput.value);
        macInput.value = item.mac;
      });
      macCell.append(macInput);
      const identityHint = makeIdentityHint(device.id);
      if (identityHint) macCell.append(identityHint);

      const locationCell = document.createElement("td");
      const locationButton = document.createElement("button");
      locationButton.type = "button";
      locationButton.className = "location-edit-button";
      locationButton.dataset.locationFor = device.id;
      locationButton.addEventListener("click", () => selectDevice(device.id, true));
      locationCell.append(locationButton);
      fillLocationButton(locationButton, device);

      const enabledCell = document.createElement("td");
      const switchLabel = document.createElement("label");
      switchLabel.className = "switch";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = device.enabled;
      checkbox.setAttribute("aria-label", `启用 ${device.name || "设备"}`);
      checkbox.addEventListener("change", () => {
        const item = findDraftDevice(device.id);
        if (!item) return;
        item.enabled = checkbox.checked;
        markDirty();
      });
      const switchTrack = document.createElement("span");
      switchTrack.className = "switch-track";
      switchTrack.setAttribute("aria-hidden", "true");
      switchLabel.append(checkbox, switchTrack);
      enabledCell.append(switchLabel);

      const actionCell = document.createElement("td");
      const actionWrap = document.createElement("div");
      actionWrap.className = "device-actions";
      const editButton = document.createElement("button");
      editButton.type = "button";
      editButton.className = "button secondary compact";
      editButton.textContent = "编辑位置";
      editButton.addEventListener("click", () => selectDevice(device.id, true));
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "row-remove";
      removeButton.textContent = "×";
      removeButton.setAttribute("aria-label", `删除设备 ${device.name || device.ip}`);
      removeButton.addEventListener("click", () => removeDevice(device.id));
      actionWrap.append(editButton, removeButton);
      actionCell.append(actionWrap);

      row.append(nameCell, ipCell, macCell, locationCell, enabledCell, actionCell);
      ui.devicesBody.append(row);
    }
  }

  function fillLocationButton(button, device) {
    button.replaceChildren();
    const name = document.createElement("strong");
    name.textContent = device.location.name || "未命名位置";
    const coords = document.createElement("span");
    coords.className = "mono";
    coords.textContent = `${formatCoordinate(device.location.latitude)}, ${formatCoordinate(device.location.longitude)}`;
    button.append(name, coords);
    button.setAttribute("aria-label", `编辑 ${device.name || "设备"} 的位置：${device.location.name || "未命名位置"}`);
  }

  function renderDeviceLocationSummary(id) {
    const device = findDraftDevice(id);
    if (!device) return;
    const button = Array.from(ui.devicesBody.querySelectorAll("button[data-location-for]"))
      .find(item => item.dataset.locationFor === id);
    if (button) fillLocationButton(button, device);
  }

  function makeIdentityHint(deviceId) {
    const service = appState.server && appState.server.service;
    const identities = service && service.identity && typeof service.identity === "object" ? service.identity : null;
    const identity = identities && identities[deviceId] && typeof identities[deviceId] === "object" ? identities[deviceId] : null;
    if (!identity) return null;

    const status = ["match", "mismatch", "unknown"].includes(identity.status) ? identity.status : "unknown";
    const hint = document.createElement("small");
    hint.className = `identity-hint ${status}`;
    if (status === "match") hint.textContent = "已观察到并匹配";
    else if (status === "mismatch") hint.textContent = `MAC 不符${identity.observed_mac ? ` · ${identity.observed_mac}` : ""}`;
    else hint.textContent = identity.observed_mac ? `最近观察：${identity.observed_mac}` : "尚未观察到";
    return hint;
  }

  function renderPresets() {
    ui.presetsList.replaceChildren();
    const presets = appState.draft ? appState.draft.presets : [];
    ui.presetsEmpty.hidden = presets.length !== 0;
    ui.addPresetButton.disabled = !selectedDraftDevice();

    for (const preset of presets) {
      const card = document.createElement("article");
      card.className = "preset-card";

      const top = document.createElement("div");
      top.className = "preset-top";
      const details = document.createElement("div");
      const name = document.createElement("h3");
      name.className = "preset-name";
      name.textContent = preset.name || "未命名位置";
      const coords = document.createElement("p");
      coords.className = "preset-coords mono";
      coords.textContent = `${formatCoordinate(preset.latitude)}, ${formatCoordinate(preset.longitude)} · ±${formatAccuracy(preset.accuracy)}`;
      details.append(name, coords);
      top.append(details);

      const actions = document.createElement("div");
      actions.className = "preset-actions";
      const useButton = document.createElement("button");
      useButton.type = "button";
      useButton.className = "button secondary compact";
      useButton.textContent = "应用到当前设备";
      useButton.disabled = !selectedDraftDevice();
      useButton.addEventListener("click", () => applyPreset(preset.id));
      const removeButton = document.createElement("button");
      removeButton.type = "button";
      removeButton.className = "button danger-subtle compact";
      removeButton.textContent = "删除";
      removeButton.addEventListener("click", () => void removePreset(preset.id));
      actions.append(useButton, removeButton);

      card.append(top, actions);
      ui.presetsList.append(card);
    }
  }

  function renderEvents(events) {
    ui.eventsList.replaceChildren();
    const items = events.slice(0, 50);
    ui.eventsEmpty.hidden = items.length !== 0;

    for (const event of items) {
      const item = document.createElement("li");
      const level = normalizeEventLevel(event.level);
      item.className = `event-item ${level}`;
      const dot = document.createElement("span");
      dot.className = "event-dot";
      dot.setAttribute("aria-hidden", "true");
      const message = document.createElement("div");
      message.className = "event-message";
      message.textContent = eventText(event);
      const time = document.createElement("time");
      time.className = "event-time";
      const rawTime = event.time || event.timestamp || event.created_at;
      if (rawTime) time.dateTime = String(rawTime);
      time.textContent = rawTime ? formatDateTime(rawTime) : "—";
      item.append(dot, message, time);
      ui.eventsList.append(item);
    }
  }

  function renderDirtyState() {
    ui.dirtyBadge.hidden = !appState.dirty;
    ui.discardButton.disabled = !appState.dirty;
    ui.applyButton.disabled = !appState.dirty;
    ui.saveSummary.textContent = appState.dirty ? "存在未保存修改" : "配置已同步";
    ui.saveDetail.textContent = appState.dirty
      ? `将基于版本 r${String(appState.baseRevision)} 提交`
      : "修改后点击应用配置";
  }

  function switchMapLayer(key, initial = false) {
    if (!appState.map) return;
    const layer = MAP_LAYERS[key] || MAP_LAYERS["esri-proxy"];
    if (appState.tileLayer) appState.map.removeLayer(appState.tileLayer);
    appState.tileErrorShown = false;
    appState.tileLayer = window.L.tileLayer(layer.url, {
      maxZoom: layer.maxZoom,
      referrerPolicy: "origin",
      attribution: layer.attribution
    });
    appState.tileLayer.on("tileerror", () => {
      if (appState.tileErrorShown) return;
      appState.tileErrorShown = true;
      showToast("地图底图加载失败；可切换其他图层或直接输入经纬度。", "warning");
    });
    appState.tileLayer.addTo(appState.map);
    if (!initial && ui.mapLayerSelect.value !== key) ui.mapLayerSelect.value = key;
  }

  function ensureMap() {
    if (appState.map) {
      window.requestAnimationFrame(() => appState.map.invalidateSize());
      return;
    }

    if (typeof window.L === "undefined") {
      ui.map.textContent = "地图组件未能加载，请确认 /vendor/leaflet.js 可访问。";
      showToast("地图组件加载失败，仍可手动输入经纬度。", "warning");
      return;
    }

    const selected = selectedDraftDevice();
    const target = selected ? selected.location : DEFAULT_TARGET;
    appState.map = window.L.map(ui.map, {
      zoomControl: true,
      attributionControl: true,
      preferCanvas: true
    }).setView([target.latitude, target.longitude], 13);

    switchMapLayer(ui.mapLayerSelect.value || "esri-proxy", true);

    const icon = window.L.divIcon({
      className: "wloc-marker",
      iconSize: [30, 30],
      iconAnchor: [15, 30]
    });
    appState.marker = window.L.marker([target.latitude, target.longitude], {
      draggable: true,
      keyboard: true,
      title: "拖动选择目标位置",
      icon
    }).addTo(appState.map);

    appState.circleRenderer = window.L.canvas({ padding: 0.5 });
    appState.accuracyCircle = window.L.circle([target.latitude, target.longitude], {
      radius: target.accuracy,
      color: "#35d59a",
      weight: 2,
      opacity: 0.88,
      fillColor: "#35d59a",
      fillOpacity: 0.13,
      renderer: appState.circleRenderer
    }).addTo(appState.map);

    appState.map.on("click", event => setTargetCoordinates(event.latlng.lat, event.latlng.lng));
    appState.marker.on("drag", event => {
      const point = event.target.getLatLng();
      setTargetCoordinates(point.lat, point.lng, false);
    });
    appState.marker.on("dragend", event => {
      const point = event.target.getLatLng();
      setTargetCoordinates(point.lat, point.lng, true);
    });

    window.requestAnimationFrame(() => appState.map.invalidateSize());
  }

  function updateMapTarget(target, center = false) {
    if (!appState.map || !appState.marker || !appState.accuracyCircle) return;
    const point = [target.latitude, target.longitude];
    appState.marker.setLatLng(point);
    appState.accuracyCircle.setLatLng(point);
    appState.accuracyCircle.setRadius(target.accuracy);
    if (center) appState.map.setView(point, Math.max(appState.map.getZoom(), 13));
  }

  function setTargetCoordinates(latitude, longitude, center = false) {
    const device = selectedDraftDevice();
    if (!device) return;
    device.location.latitude = roundCoordinate(latitude);
    device.location.longitude = roundCoordinate(longitude);
    ui.targetLatitude.value = compactNumber(device.location.latitude);
    ui.targetLongitude.value = compactNumber(device.location.longitude);
    ui.targetLatitude.removeAttribute("aria-invalid");
    ui.targetLongitude.removeAttribute("aria-invalid");
    updateMapTarget(device.location, center);
    renderSelectedOverview();
    renderDeviceLocationSummary(device.id);
    markDirty();
  }

  function handleCoordinateInput() {
    const device = selectedDraftDevice();
    if (!device) return;
    const latitude = Number(ui.targetLatitude.value);
    const longitude = Number(ui.targetLongitude.value);
    const latValid = ui.targetLatitude.value.trim() !== "" && Number.isFinite(latitude) && latitude >= -90 && latitude <= 90;
    const lonValid = ui.targetLongitude.value.trim() !== "" && Number.isFinite(longitude) && longitude >= -180 && longitude <= 180;
    setAriaInvalid(ui.targetLatitude, !latValid);
    setAriaInvalid(ui.targetLongitude, !lonValid);

    if (latValid && lonValid) {
      device.location.latitude = latitude;
      device.location.longitude = longitude;
      updateMapTarget(device.location, false);
      renderSelectedOverview();
      renderDeviceLocationSummary(device.id);
    }
    markDirty();
  }

  function handleAccuracyInput() {
    const device = selectedDraftDevice();
    if (!device) return;
    const accuracy = Number(ui.targetAccuracy.value);
    const valid = ui.targetAccuracy.value.trim() !== "" && Number.isFinite(accuracy) && accuracy >= 1 && accuracy <= 100000;
    setAriaInvalid(ui.targetAccuracy, !valid);
    if (valid) {
      device.location.accuracy = accuracy;
      ui.targetAccuracyRange.value = String(clamp(accuracy, 1, 500));
      updateMapTarget(device.location, false);
      renderSelectedOverview();
      renderDeviceLocationSummary(device.id);
    }
    markDirty();
  }

  function targetFromInputs(reportErrors = true) {
    if (!selectedDraftDevice()) {
      if (reportErrors) reportInputError(new InputError("请先添加并选择一台设备。"));
      return null;
    }
    const name = ui.targetName.value.trim();
    const latitude = Number(ui.targetLatitude.value);
    const longitude = Number(ui.targetLongitude.value);
    const accuracy = Number(ui.targetAccuracy.value);

    try {
      if (!name) throw new InputError("请填写位置名称。", ui.targetName);
      if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90) {
        throw new InputError("纬度必须是 -90 到 90 之间的数字。", ui.targetLatitude);
      }
      if (!Number.isFinite(longitude) || longitude < -180 || longitude > 180) {
        throw new InputError("经度必须是 -180 到 180 之间的数字。", ui.targetLongitude);
      }
      if (!Number.isFinite(accuracy) || accuracy < 1 || accuracy > 100000) {
        throw new InputError("定位精度必须是 1 到 100000 米之间的数字。", ui.targetAccuracy);
      }
      return { name, latitude, longitude, accuracy };
    } catch (error) {
      if (reportErrors) reportInputError(error);
      return null;
    }
  }

  function openDeviceDialog() {
    if (!appState.draft) return;
    if (appState.draft.devices.length >= MAX_DEVICES) {
      showToast(`最多支持 ${MAX_DEVICES} 台设备。`, "warning");
      return;
    }
    const source = selectedDraftDevice();
    ui.deviceForm.reset();
    ui.deviceName.value = `新设备 ${appState.draft.devices.length + 1}`;
    ui.deviceLocationCopy.textContent = source
      ? `初始位置将复制自“${source.name}”：${source.location.name}`
      : `初始位置：${DEFAULT_TARGET.name}`;
    setFormError(ui.deviceError, "");
    ui.deviceName.removeAttribute("aria-invalid");
    ui.deviceIp.removeAttribute("aria-invalid");
    ui.deviceMac.removeAttribute("aria-invalid");
    if (typeof ui.deviceDialog.showModal === "function") {
      ui.deviceDialog.showModal();
      window.setTimeout(() => ui.deviceName.select(), 0);
    }
  }

  function closeDeviceDialog() {
    if (ui.deviceDialog.open) ui.deviceDialog.close();
  }

  function addDevice(event) {
    event.preventDefault();
    if (!appState.draft) return;
    if (appState.draft.devices.length >= MAX_DEVICES) {
      setFormError(ui.deviceError, `最多支持 ${MAX_DEVICES} 台设备。`);
      return;
    }

    const name = ui.deviceName.value.trim();
    const ip = ui.deviceIp.value.trim();
    const macRaw = ui.deviceMac.value.trim();
    if (!name) {
      setFormError(ui.deviceError, "请填写设备名称。");
      setAriaInvalid(ui.deviceName, true);
      ui.deviceName.focus();
      return;
    }
    if (!isIPv4(ip)) {
      setFormError(ui.deviceError, "请输入有效的内网 IPv4 地址。");
      setAriaInvalid(ui.deviceIp, true);
      ui.deviceIp.focus();
      return;
    }
    if (appState.draft.devices.some(device => device.ip === ip)) {
      setFormError(ui.deviceError, `设备地址 ${ip} 已存在。`);
      setAriaInvalid(ui.deviceIp, true);
      ui.deviceIp.focus();
      return;
    }
    if (macRaw && !isMacAddress(macRaw)) {
      setFormError(ui.deviceError, "固定 WLAN 地址格式无效，请使用 AA:BB:CC:DD:EE:FF。");
      setAriaInvalid(ui.deviceMac, true);
      ui.deviceMac.focus();
      return;
    }
    const mac = macRaw ? normalizeMacAddress(macRaw) : "";
    if (mac && appState.draft.devices.some(device => normalizeMacAddress(device.mac) === mac)) {
      setFormError(ui.deviceError, `固定 WLAN 地址 ${mac} 已存在。`);
      setAriaInvalid(ui.deviceMac, true);
      ui.deviceMac.focus();
      return;
    }

    const sourceLocation = selectedDraftDevice()?.location || DEFAULT_TARGET;
    const device = {
      id: makeId("device"),
      name,
      ip,
      mac,
      enabled: true,
      location: normalizeLocation(sourceLocation, DEFAULT_TARGET)
    };
    appState.draft.devices.push(device);
    appState.selectedDeviceId = device.id;
    closeDeviceDialog();
    renderDevices();
    renderPresets();
    renderSelectedDeviceEditor(true);
    markDirty();
    showToast(`已添加“${device.name}”，其位置副本可单独编辑。`, "success");
  }

  function removeDevice(id) {
    if (!appState.draft) return;
    const index = appState.draft.devices.findIndex(device => device.id === id);
    appState.draft.devices = appState.draft.devices.filter(device => device.id !== id);
    if (appState.selectedDeviceId === id) {
      const replacement = appState.draft.devices[Math.min(index, appState.draft.devices.length - 1)];
      appState.selectedDeviceId = replacement ? replacement.id : null;
    }
    renderDevices();
    renderPresets();
    renderSelectedDeviceEditor(false);
    markDirty();
  }

  function findDraftDevice(id) {
    return appState.draft ? appState.draft.devices.find(device => device.id === id) : null;
  }

  function saveCurrentAsPreset() {
    const device = selectedDraftDevice();
    if (!appState.draft || !device) {
      showToast("请先添加并选择一台设备。", "warning");
      return;
    }
    const target = targetFromInputs(true);
    if (!target) return;

    device.location = { ...target };
    appState.draft.presets.push({ id: makeId("preset"), ...target });
    renderDeviceLocationSummary(device.id);
    renderPresets();
    markDirty();
    showToast(`已将“${target.name}”加入预设，应用配置后生效。`, "success");
  }

  function applyPreset(id) {
    const device = selectedDraftDevice();
    if (!appState.draft || !device) {
      showToast("请先选择要应用预设的设备。", "warning");
      return;
    }
    const preset = appState.draft.presets.find(item => item.id === id);
    if (!preset) return;
    device.location = {
      name: preset.name,
      latitude: preset.latitude,
      longitude: preset.longitude,
      accuracy: preset.accuracy
    };
    ui.targetName.value = preset.name;
    ui.targetLatitude.value = compactNumber(preset.latitude);
    ui.targetLongitude.value = compactNumber(preset.longitude);
    ui.targetAccuracy.value = compactNumber(preset.accuracy);
    ui.targetAccuracyRange.value = String(clamp(preset.accuracy, 1, 500));
    clearTargetInvalidState();
    updateMapTarget(preset, true);
    renderSelectedOverview();
    renderDeviceLocationSummary(device.id);
    markDirty();
    showToast(`已将“${preset.name}”复制到“${device.name}”，点击应用配置后生效。`, "success");
  }

  async function removePreset(id) {
    if (!appState.draft) return;
    const preset = appState.draft.presets.find(item => item.id === id);
    if (!preset) return;
    const confirmed = await askConfirmation("删除地址预设", `确定删除“${preset.name}”吗？此修改需要应用配置后生效。`, "删除");
    if (!confirmed) return;
    appState.draft.presets = appState.draft.presets.filter(item => item.id !== id);
    renderPresets();
    markDirty();
  }

  function validateDraft() {
    if (!appState.draft) throw new InputError("尚未取得配置，请刷新页面后重试。");
    if (appState.draft.devices.length > MAX_DEVICES) {
      throw new InputError(`最多支持 ${MAX_DEVICES} 台设备。`);
    }
    const selected = selectedDraftDevice();
    if (selected) {
      const editorLocation = targetFromInputs(false);
      if (!editorLocation) throw new InputError("当前设备的位置填写有误，请检查位置名称、坐标和精度。", ui.targetName);
      selected.location = editorLocation;
    }

    const seenIps = new Set();
    const seenMacs = new Set();
    const devices = appState.draft.devices.map(device => {
      const name = String(device.name || "").trim();
      const ip = String(device.ip || "").trim();
      const macRaw = String(device.mac || "").trim();
      const row = ui.devicesBody.querySelector(`tr[data-id="${cssEscape(device.id)}"]`);
      const nameInput = row ? row.querySelector("td:nth-child(1) input") : null;
      const ipInput = row ? row.querySelector("td:nth-child(2) input") : null;
      const macInput = row ? row.querySelector("td:nth-child(3) input") : null;
      if (!name) throw new InputError("每台设备都需要填写名称。", nameInput);
      if (!isIPv4(ip)) throw new InputError(`设备“${name}”的 IPv4 地址无效。`, ipInput);
      if (seenIps.has(ip)) throw new InputError(`设备地址 ${ip} 重复，请保留一条。`, ipInput);
      seenIps.add(ip);
      if (macRaw && !isMacAddress(macRaw)) throw new InputError(`设备“${name}”的固定 WLAN 地址无效。`, macInput);
      const mac = macRaw ? normalizeMacAddress(macRaw) : "";
      if (mac && seenMacs.has(mac)) throw new InputError(`固定 WLAN 地址 ${mac} 重复，请保留一条。`, macInput);
      if (mac) seenMacs.add(mac);

      const result = {
        id: device.id,
        name,
        ip,
        enabled: Boolean(device.enabled),
        location: validateLocation(device.location, `设备“${name}”`)
      };
      if (mac) result.mac = mac;
      if (Object.prototype.hasOwnProperty.call(device, "slot")) result.slot = device.slot;
      return result;
    });

    const presets = appState.draft.presets.map(preset => {
      const normalized = normalizeLocation(preset, DEFAULT_TARGET);
      const name = String(normalized.name || "").trim();
      if (!name) throw new InputError("地址预设名称不能为空。");
      if (normalized.latitude < -90 || normalized.latitude > 90 || normalized.longitude < -180 || normalized.longitude > 180) {
        throw new InputError(`地址预设“${name}”的坐标无效。`);
      }
      if (normalized.accuracy < 1 || normalized.accuracy > 100000) {
        throw new InputError(`地址预设“${name}”的精度无效。`);
      }
      return { id: preset.id, name, latitude: normalized.latitude, longitude: normalized.longitude, accuracy: normalized.accuracy };
    });

    return {
      schema: 2,
      revision: appState.baseRevision,
      devices,
      presets
    };
  }

  function validateLocation(value, ownerLabel) {
    const name = String(value && value.name || "").trim();
    const latitude = Number(value && value.latitude);
    const longitude = Number(value && value.longitude);
    const accuracy = Number(value && value.accuracy);
    if (!name) throw new InputError(`${ownerLabel}的位置名称不能为空。`);
    if (!Number.isFinite(latitude) || latitude < -90 || latitude > 90) {
      throw new InputError(`${ownerLabel}的纬度无效。`);
    }
    if (!Number.isFinite(longitude) || longitude < -180 || longitude > 180) {
      throw new InputError(`${ownerLabel}的经度无效。`);
    }
    if (!Number.isFinite(accuracy) || accuracy < 1 || accuracy > 100000) {
      throw new InputError(`${ownerLabel}的定位精度无效。`);
    }
    return { name, latitude, longitude, accuracy };
  }

  async function applyConfig() {
    let payload;
    try {
      payload = validateDraft();
    } catch (error) {
      reportInputError(error);
      return;
    }

    setButtonBusy(ui.applyButton, true);
    ui.discardButton.disabled = true;
    try {
      const response = await request("/api/config", { method: "POST", body: payload });
      appState.dirty = false;
      receiveServerState(response, false);
      showToast(
        response && response.warning
          ? response.warning
          : "配置已应用。新的定位会在设备下次请求时生效。",
        response && response.warning ? "warning" : "success",
        response && response.warning ? 6500 : 3600
      );
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        showLogin("登录状态已失效，请重新登录。");
        return;
      }
      if (error instanceof ApiError && error.status === 409) {
        showToast("路由器上的配置已经变化。请放弃本地修改或刷新后重新编辑。", "warning", 6500);
      } else {
        showToast(error.message, "error", 6500);
      }
    } finally {
      setButtonBusy(ui.applyButton, false);
      renderDirtyState();
    }
  }

  function discardChanges() {
    if (!appState.server) return;
    appState.draft = cloneValue(appState.server.config);
    appState.baseRevision = appState.server.config.revision;
    appState.dirty = false;
    renderEditableConfig();
    renderDirtyState();
    showToast("已放弃尚未应用的修改。", "success");
  }

  async function rollbackLatest() {
    const warning = appState.dirty
      ? "这会丢弃当前未保存修改，并恢复路由器保存的上一个配置版本。"
      : "这会恢复路由器保存的上一个配置版本。";
    const confirmed = await askConfirmation("恢复上次配置", `${warning} 是否继续？`, "恢复");
    if (!confirmed) return;

    setButtonBusy(ui.rollbackButton, true);
    try {
      const response = await request("/api/rollback-latest", { method: "POST" });
      appState.dirty = false;
      if (response && typeof response === "object" && (response.config || response.data || response.state)) {
        receiveServerState(response, false);
      } else {
        await loadState({ preserveDraft: false, quiet: true });
      }
      showToast(
        response && response.warning ? response.warning : "已恢复上一个配置版本。",
        response && response.warning ? "warning" : "success",
        response && response.warning ? 6500 : 3600
      );
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        showLogin("登录状态已失效，请重新登录。");
      } else {
        showToast(error.message, "error", 6500);
      }
    } finally {
      setButtonBusy(ui.rollbackButton, false);
      if (appState.server) ui.rollbackButton.disabled = !appState.server.canRollback;
    }
  }

  async function handleLogin(event) {
    event.preventDefault();
    const password = ui.loginPassword.value;
    if (!password) {
      setFormError(ui.loginError, "请输入管理密码。");
      ui.loginPassword.focus();
      return;
    }

    setFormError(ui.loginError, "");
    setButtonBusy(ui.loginButton, true);
    try {
      await request("/api/login", { method: "POST", body: { password } });
      ui.loginPassword.value = "";
      await loadState({ initial: false });
    } catch (error) {
      const message = error instanceof ApiError && error.status === 401 ? "密码不正确。" : error.message;
      setFormError(ui.loginError, message);
      ui.loginPassword.select();
    } finally {
      setButtonBusy(ui.loginButton, false);
    }
  }

  async function handleLogout() {
    try {
      await request("/api/logout", { method: "POST" });
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        showToast(error.message, "warning");
      }
    } finally {
      showLogin("");
    }
  }

  function openPasswordDialog() {
    ui.passwordForm.reset();
    setFormError(ui.passwordError, "");
    if (typeof ui.passwordDialog.showModal === "function") {
      ui.passwordDialog.showModal();
      window.setTimeout(() => ui.currentPassword.focus(), 0);
    }
  }

  function closePasswordDialog() {
    if (ui.passwordDialog.open) ui.passwordDialog.close();
  }

  async function handlePasswordChange(event) {
    event.preventDefault();
    const currentPassword = ui.currentPassword.value;
    const newPassword = ui.newPassword.value;
    const confirmPassword = ui.confirmPassword.value;

    if (!currentPassword) {
      setFormError(ui.passwordError, "请输入当前密码。");
      ui.currentPassword.focus();
      return;
    }
    if (newPassword.length < 10) {
      setFormError(ui.passwordError, "新密码至少需要 10 个字符。");
      ui.newPassword.focus();
      return;
    }
    if (newPassword !== confirmPassword) {
      setFormError(ui.passwordError, "两次输入的新密码不一致。");
      ui.confirmPassword.focus();
      return;
    }
    if (newPassword === currentPassword) {
      setFormError(ui.passwordError, "新密码不能与当前密码相同。");
      ui.newPassword.focus();
      return;
    }

    setButtonBusy(ui.passwordSubmit, true);
    setFormError(ui.passwordError, "");
    try {
      await request("/api/password", {
        method: "POST",
        body: { currentPassword, newPassword }
      });
      ui.passwordForm.reset();
      closePasswordDialog();
      showLogin("管理密码已更新，请使用新密码重新登录。");
    } catch (error) {
      const message = error instanceof ApiError && error.status === 401 ? "当前密码不正确。" : error.message;
      setFormError(ui.passwordError, message);
    } finally {
      setButtonBusy(ui.passwordSubmit, false);
    }
  }

  function showApp() {
    const wasHidden = ui.appView.hidden;
    ui.loginView.hidden = true;
    ui.appView.hidden = false;
    setFormError(ui.loginError, "");
    if (wasHidden) {
      ensureMap();
      window.requestAnimationFrame(() => {
        if (appState.map) appState.map.invalidateSize();
      });
    }
  }

  function showLogin(message = "") {
    appState.authenticated = false;
    appState.server = null;
    appState.draft = null;
    appState.baseRevision = null;
    appState.dirty = false;
    appState.selectedDeviceId = null;
    ui.appView.hidden = true;
    ui.loginView.hidden = false;
    ui.loginPassword.value = "";
    setFormError(ui.loginError, message);
    updateConnection("offline", "未登录");
    window.setTimeout(() => ui.loginPassword.focus(), 0);
  }

  function markDirty() {
    if (!appState.draft) return;
    appState.dirty = true;
    renderDirtyState();
  }

  function updateConnection(stateName, label) {
    ui.connectionState.className = "connection-pill";
    if (stateName === "online") ui.connectionState.classList.add("online");
    if (stateName === "offline") ui.connectionState.classList.add("offline");
    const labelElement = ui.connectionState.querySelector("span");
    if (labelElement) labelElement.textContent = label;
    ui.connectionState.title = label;
  }

  function serviceStatus(rawStatus) {
    const status = String(rawStatus || "unknown").toLowerCase();
    if (["running", "healthy", "active", "ok", "online"].includes(status)) {
      return { label: "运行正常", className: "online", detail: "WLOC 服务工作正常" };
    }
    if (["stopped", "unhealthy", "failed", "error", "offline"].includes(status)) {
      return { label: "服务异常", className: "offline", detail: "请检查 WLOC 服务日志" };
    }
    if (["attention", "pending", "warning"].includes(status)) {
      return { label: "等待同步", className: "unknown", detail: "配置服务在线，动态规则尚待同步" };
    }
    return { label: "状态未知", className: "unknown", detail: "尚未取得服务运行状态" };
  }

  function setButtonBusy(button, busy) {
    if (!button) return;
    button.classList.toggle("is-loading", busy);
    button.disabled = busy;
    button.setAttribute("aria-busy", String(busy));
  }

  function setFormError(element, message) {
    element.textContent = message;
    element.hidden = !message;
  }

  function reportInputError(error) {
    const message = error instanceof Error ? error.message : "请检查填写的配置。";
    showToast(message, "error", 6000);
    if (error instanceof InputError && error.element) {
      error.element.setAttribute("aria-invalid", "true");
      error.element.focus();
    }
  }

  function clearTargetInvalidState() {
    ui.targetName.removeAttribute("aria-invalid");
    ui.targetLatitude.removeAttribute("aria-invalid");
    ui.targetLongitude.removeAttribute("aria-invalid");
    ui.targetAccuracy.removeAttribute("aria-invalid");
  }

  function setAriaInvalid(element, invalid) {
    if (invalid) element.setAttribute("aria-invalid", "true");
    else element.removeAttribute("aria-invalid");
  }

  function showToast(message, type = "info", duration = 4200) {
    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    toast.setAttribute("role", type === "error" ? "alert" : "status");
    toast.textContent = message;
    ui.toastRegion.append(toast);
    window.setTimeout(() => toast.remove(), duration);
  }

  function askConfirmation(title, message, confirmLabel = "确认") {
    if (typeof ui.confirmDialog.showModal !== "function") {
      return Promise.resolve(window.confirm(message));
    }
    ui.confirmTitle.textContent = title;
    ui.confirmMessage.textContent = message;
    ui.confirmAccept.textContent = confirmLabel;
    ui.confirmDialog.showModal();
    return new Promise(resolve => {
      ui.confirmDialog.addEventListener("close", () => {
        resolve(ui.confirmDialog.returnValue === "confirm");
      }, { once: true });
    });
  }

  function eventText(event) {
    const message = stringValue(event.message, "未命名事件");
    if (event.detail === undefined || event.detail === null || event.detail === "") return message;
    const detail = typeof event.detail === "string" ? event.detail : safeJson(event.detail);
    return detail ? `${message} · ${detail}` : message;
  }

  function normalizeEventLevel(level) {
    const value = String(level || "info").toLowerCase();
    if (["success", "ok", "patched"].includes(value)) return "success";
    if (["warn", "warning"].includes(value)) return "warning";
    if (["error", "failed", "failure"].includes(value)) return "error";
    return "info";
  }

  function isIPv4(value) {
    const parts = String(value).split(".");
    if (parts.length !== 4) return false;
    return parts.every(part => {
      if (!/^\d{1,3}$/.test(part)) return false;
      if (part.length > 1 && part.startsWith("0")) return false;
      const number = Number(part);
      return number >= 0 && number <= 255;
    });
  }

  function isMacAddress(value) {
    return /^(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$/.test(String(value).trim());
  }

  function normalizeMacAddress(value) {
    return String(value || "").trim().replace(/-/g, ":").toUpperCase();
  }

  function formatCoordinate(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number.toFixed(6) : "—";
  }

  function formatAccuracy(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    if (number >= 1000) return `${(number / 1000).toFixed(number >= 10000 ? 0 : 1)} km`;
    return `${Math.round(number)} m`;
  }

  function formatDateTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false
    }).format(date);
  }

  function compactNumber(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "";
    return String(Number(number.toFixed(7)));
  }

  function roundCoordinate(value) {
    return Number(Number(value).toFixed(7));
  }

  function finiteValue(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function stringValue(value, fallback = "") {
    return typeof value === "string" && value.trim() ? value : fallback;
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, Number(value)));
  }

  function makeId(prefix) {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return `${prefix}-${window.crypto.randomUUID()}`;
    }
    return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function cloneValue(value) {
    if (typeof window.structuredClone === "function") return window.structuredClone(value);
    return JSON.parse(JSON.stringify(value));
  }

  function safeJson(value) {
    try {
      return JSON.stringify(value);
    } catch (_error) {
      return "";
    }
  }

  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(value);
    return String(value).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function toCamel(value) {
    return value.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
  }
})();
