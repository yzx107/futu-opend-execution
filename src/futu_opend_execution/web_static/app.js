const state = {
  armed: false,
  events: [],
};

const $ = (selector) => document.querySelector(selector);

const els = {
  statusText: $("#statusText"),
  healthStatus: $("#healthStatus"),
  lastQuote: $("#lastQuote"),
  eventCount: $("#eventCount"),
  refreshAllBtn: $("#refreshAllBtn"),
  normalForm: $("#normalForm"),
  normalSymbol: $("#normalSymbol"),
  normalQuote: $("#normalQuote"),
  normalQuoteBtn: $("#normalQuoteBtn"),
  normalDryRun: $("#normalDryRun"),
  normalModeText: $("#normalModeText"),
  normalConfirmWrap: $("#normalConfirmWrap"),
  normalConfirmText: $("#normalConfirmText"),
  normalLimitPrice: $("#normalLimitPrice"),
  greyForm: $("#greyForm"),
  greySymbol: $("#greySymbol"),
  greyDryRun: $("#greyDryRun"),
  greyModeText: $("#greyModeText"),
  greyConfirmText: $("#greyConfirmText"),
  greyAckReal: $("#greyAckReal"),
  greyState: $("#greyState"),
  greyEvaluateBtn: $("#greyEvaluateBtn"),
  greyArmBtn: $("#greyArmBtn"),
  killSwitchBtn: $("#killSwitchBtn"),
  eventsRefreshBtn: $("#eventsRefreshBtn"),
  eventLog: $("#eventLog"),
  realModeStatus: $("#realModeStatus"),
  killStatus: $("#killStatus"),
  validateConfigBtn: $("#validateConfigBtn"),
  subscribeBtn: $("#subscribeBtn"),
  dryRunOpenTriggerBtn: $("#dryRunOpenTriggerBtn"),
  startLiveDryRunBtn: $("#startLiveDryRunBtn"),
  stopLiveRunBtn: $("#stopLiveRunBtn"),
  createKillSwitchBtn: $("#createKillSwitchBtn"),
  clearKillSwitchBtn: $("#clearKillSwitchBtn"),
  seedInventoryBtn: $("#seedInventoryBtn"),
  resetInventoryBtn: $("#resetInventoryBtn"),
  reconcileInventoryBtn: $("#reconcileInventoryBtn"),
  costReducerConfigForm: $("#costReducerConfigForm"),
  replayForm: $("#replayForm"),
  replaySummary: $("#replaySummary"),
};

function nowTime() {
  return new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

function setBusy(button, busy) {
  if (!button) return;
  button.disabled = busy;
}

function compactJson(value) {
  if (value == null || value === "") return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function addLocalEvent(type, message, level = "ok") {
  state.events.unshift({
    time: nowTime(),
    type,
    message,
    level,
  });
  state.events = state.events.slice(0, 80);
  renderEvents(state.events);
}

function renderEvents(events) {
  els.eventCount.textContent = String(events.length);
  els.eventLog.innerHTML = "";

  if (!events.length) {
    const empty = document.createElement("li");
    empty.className = "warn";
    empty.innerHTML = '<span class="time">--</span><span class="type">等待</span><span class="msg">暂无事件</span>';
    els.eventLog.append(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  events.forEach((event) => {
    const item = document.createElement("li");
    item.className = event.level || event.severity || "ok";

    const time = document.createElement("span");
    time.className = "time";
    time.textContent = event.time || event.ts || event.timestamp || nowTime();

    const type = document.createElement("span");
    type.className = "type";
    type.textContent = event.type || event.level || "事件";

    const msg = document.createElement("span");
    msg.className = "msg";
    msg.textContent = event.message || event.msg || compactJson(event);

    item.append(time, type, msg);
    fragment.append(item);
  });

  els.eventLog.append(fragment);
}

async function requestJson(url, options = {}) {
  const { timeoutMs = 15000, ...fetchOptions } = options;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(fetchOptions.headers || {}) },
    ...fetchOptions,
    signal: fetchOptions.signal || controller.signal,
  }).catch((error) => {
    if (error.name === "AbortError") {
      throw new Error("请求超时：OpenD 没有及时返回，请检查连接、标的代码和暗盘行情。");
    }
    throw error;
  }).finally(() => window.clearTimeout(timer));

  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { message: text };
    }
  }

  if (!response.ok) {
    const detail = data?.error || data?.message || response.statusText;
    throw new Error(`${response.status} ${detail}`);
  }

  return data;
}

function numberOrNull(value) {
  if (value === "" || value == null) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function checkedValue(form, name) {
  return new FormData(form).get(name);
}

function normalPayload() {
  const mode = checkedValue(els.normalForm, "quantity_mode");
  const orderType = checkedValue(els.normalForm, "order_type");
  const real = !els.normalDryRun.checked;
  return {
    symbol: $("#normalSymbol").value.trim(),
    side: checkedValue(els.normalForm, "side"),
    order_type: orderType,
    quantity_mode: mode,
    lots: numberOrNull($("#normalLots").value),
    shares: numberOrNull($("#normalShares").value),
    limit_price: orderType === "MARKET" ? null : numberOrNull($("#normalLimitPrice").value),
    max_notional: numberOrNull($("#normalMaxNotional").value),
    real,
    confirm_text: real ? els.normalConfirmText.value.trim() : "",
    remark: "web_normal_trade",
  };
}

function greyPayload() {
  const primarySymbol = $("#greySymbol")?.value || "";
  const setupSymbol = $("#setupSymbol")?.value || "";
  const quantity = numberOrNull($("#greyQuantity")?.value) ?? numberOrNull($("#setupQuantity")?.value);
  const maxPrice = numberOrNull($("#greyMaxPrice")?.value) ?? numberOrNull($("#setupMaxPrice")?.value);
  const maxNotional = numberOrNull($("#greyMaxNotional")?.value) ?? numberOrNull($("#setupMaxNotional")?.value);
  const maxAttempts = numberOrNull($("#greyMaxAttempts")?.value) ?? numberOrNull($("#setupMaxOrderAttempts")?.value);
  const coolDownMs = numberOrNull($("#greyCoolDown")?.value) ?? numberOrNull($("#setupCoolDownMs")?.value);
  return {
    symbol: (primarySymbol || setupSymbol).trim(),
    max_price: maxPrice,
    quantity,
    max_qty: numberOrNull($("#setupMaxQty")?.value),
    max_notional: maxNotional,
    max_order_attempts: maxAttempts,
    cool_down_ms: coolDownMs,
    lot_size: numberOrNull($("#setupLotSize")?.value),
    opening_burst_seconds: numberOrNull($("#setupOpeningBurstSeconds")?.value),
    opening_burst_cool_down_ms: numberOrNull($("#setupOpeningBurstCoolDownMs")?.value),
    remark: $("#setupRemark")?.value || "web_grey_open",
    real: !els.greyDryRun.checked,
    real_mode: !els.greyDryRun.checked,
    acknowledge_real_order: Boolean(els.greyAckReal?.checked),
    confirm_text: els.greyConfirmText?.value.trim() || "",
    timeout_seconds: 600,
    poll_interval_ms: 50,
  };
}

function validateGreyPayload(payload) {
  if (!payload.symbol) return "请输入暗盘标的代码。";
  if (!(payload.quantity > 0)) return "请输入暗盘买入股数，不是手数。";
  if (!(payload.max_price > 0)) return "请输入暗盘最高限价。";
  if (!(payload.max_notional > 0)) return "请输入最大金额。";
  if (payload.max_qty != null && payload.max_qty > 0 && payload.quantity > payload.max_qty) {
    return "买入数量不能超过最大股数。";
  }
  if (payload.max_price * payload.quantity > payload.max_notional) {
    return "最高限价 x 股数不能超过最大金额。";
  }
  return "";
}

function setGreyStatus(message, level = "") {
  if (!els.greyState) return;
  els.greyState.textContent = message;
  els.greyState.className = level === "error" ? "is-error" : level === "warn" ? "is-warn" : "";
}

function inventorySeedPayload() {
  return {
    target_quantity: numberOrNull($("#setupQuantity")?.value || $("#greyQuantity").value),
    lot_size: numberOrNull($("#setupLotSize")?.value),
    anchor_price: numberOrNull($("#inventoryAnchorPrice")?.value || $("#setupMaxPrice")?.value),
    core_ratio: $("#inventoryCoreRatio")?.value || "0.5",
    trading_ratio: $("#inventoryTradingRatio")?.value || "0.5",
  };
}

function costReducerConfigPayload() {
  const form = els.costReducerConfigForm;
  const data = new FormData(form);
  const payload = {};
  for (const [key, value] of data.entries()) {
    payload[key] = value;
  }
  ["cost_reducer_enabled", "dry_run_only", "manual_approval_required", "enable_auto_cost_reducer"].forEach((key) => {
    const input = form.querySelector(`[name="${key}"]`);
    if (input) payload[key] = input.checked;
  });
  return payload;
}

function replayPayload() {
  return {
    ...greyPayload(),
    input_path: $("#replayInputPath")?.value,
    output_log_path: $("#replayOutputPath")?.value,
    cost_reducer_dry_run: true,
  };
}

function syncModes() {
  els.normalModeText.textContent = els.normalDryRun.checked ? "Dry-run" : "实盘";
  els.normalModeText.className = els.normalDryRun.checked ? "" : "is-live";
  els.normalConfirmWrap.classList.toggle("is-hidden", els.normalDryRun.checked);
  els.greyModeText.textContent = els.greyDryRun.checked ? "模拟" : "实盘";
  els.greyModeText.className = els.greyDryRun.checked ? "" : "is-live";
  if (els.greyEvaluateBtn) {
    els.greyEvaluateBtn.textContent = els.greyDryRun.checked ? "试跑一次" : "实盘预检并布防";
  }
  if (els.greyArmBtn) {
    els.greyArmBtn.textContent = els.greyDryRun.checked ? "开始模拟盯盘" : "开始实盘抢单";
    els.greyArmBtn.classList.toggle("danger-button", !els.greyDryRun.checked);
    els.greyArmBtn.classList.toggle("primary-button", els.greyDryRun.checked);
  }
}

function syncOrderType() {
  const isMarket = checkedValue(els.normalForm, "order_type") === "MARKET";
  els.normalLimitPrice.disabled = isMarket;
  els.normalLimitPrice.placeholder = isMarket ? "市价单无需限价" : "0.000";
}

function syncArmed() {
  els.greyState.textContent = state.armed
    ? "已布防：运行期间请盯盘；停止会创建 kill switch"
    : "试跑一次看当前盘口；模拟布防会后台盯暗盘开盘但不真实下单。";
  els.greyState.className = state.armed ? "is-warn" : "";
  if (state.armed) {
    els.greyArmBtn.textContent = "已布防";
  } else {
    syncModes();
  }
}

async function loadHealth() {
  try {
    const data = await requestJson("/api/health");
    const status = data?.status || data?.ok || "OK";
    els.healthStatus.textContent = String(status);
    els.healthStatus.className = "is-ok";
    els.statusText.textContent = "本地服务在线";
    if (els.realModeStatus) els.realModeStatus.textContent = data?.allow_real_trade ? "允许" : "关闭";
    if (els.killStatus) els.killStatus.textContent = data?.kill_switch ? "已触发" : "未触发";
  } catch (error) {
    els.healthStatus.textContent = "异常";
    els.healthStatus.className = "is-warn";
    els.statusText.textContent = "本地服务不可用";
    addLocalEvent("health", error.message, "error");
  }
}

async function validateConfig() {
  const data = await requestJson("/api/validate-config", {
    method: "POST",
    body: JSON.stringify(greyPayload()),
  });
  addLocalEvent("config", `配置有效 ${data?.rules?.symbol || ""}`, "ok");
}

async function subscribeMarket() {
  const data = await requestJson("/api/subscribe", {
    method: "POST",
    body: JSON.stringify({ symbol: greyPayload().symbol, active: false }),
  });
  addLocalEvent("subscribe", `已记录订阅标的 ${data.symbol}`, "ok");
}

async function startLiveDryRun() {
  const payload = greyPayload();
  const validation = validateGreyPayload(payload);
  if (validation) {
    setGreyStatus(validation, "error");
    addLocalEvent("grey", validation, "error");
    return;
  }
  setGreyStatus("正在启动实时模拟布防，会读取 OpenD 行情但不会真实下单...", "warn");
  const data = await requestJson("/api/grey-open/start-live-dry-run", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  state.armed = Boolean(data.running);
  syncArmed();
  addLocalEvent("live", `实时模拟布防已启动 ${data.symbol || ""}，超时 ${data.timeout_seconds || "--"} 秒`, "warn");
}

async function stopLiveRun() {
  const data = await requestJson("/api/grey-open/stop", { method: "POST", body: "{}" });
  addLocalEvent("live", `live run stopped=${!data.running}`, "ok");
}

async function seedInventory() {
  const data = await requestJson("/api/inventory/seed-dry-run", {
    method: "POST",
    body: JSON.stringify(inventorySeedPayload()),
  });
  addLocalEvent("inventory", `seeded position=${data?.inventory?.current_position}`, "ok");
}

async function resetInventory() {
  await requestJson("/api/inventory/reset", { method: "POST", body: "{}" });
  addLocalEvent("inventory", "inventory reset", "ok");
}

async function reconcileInventory() {
  const data = await requestJson("/api/inventory/reconcile", { method: "POST", body: "{}" });
  addLocalEvent("inventory", `reconciled fills=${data.fill_count}`, "ok");
}

async function applyCostReducerConfig(event) {
  event.preventDefault();
  const data = await requestJson("/api/cost-reducer/config", {
    method: "POST",
    body: JSON.stringify(costReducerConfigPayload()),
  });
  addLocalEvent("cost", `params applied max_spread=${data?.config?.max_spread_bps}`, "ok");
}

async function runReplay(event) {
  event.preventDefault();
  const data = await requestJson("/api/replay/run", {
    method: "POST",
    body: JSON.stringify(replayPayload()),
  });
  if (els.replaySummary) {
    els.replaySummary.textContent = JSON.stringify(data.summary || {}, null, 2);
  }
  addLocalEvent("replay", `replay done submitted=${data.submitted_or_would_submit}`, "ok");
  refreshEvents();
}

async function refreshQuote(symbol, target) {
  const cleanSymbol = symbol.trim();
  if (!cleanSymbol) {
    addLocalEvent("quote", "代码不能为空", "warn");
    return;
  }

  const data = await requestJson(`/api/quote?symbol=${encodeURIComponent(cleanSymbol)}`);
  const quote = data?.quote || {};
  const price = quote.last_price ?? data?.price ?? data?.last_price ?? data?.last ?? "--";
  const bid = quote.best_bid ?? data?.bid ?? quote.bid;
  const ask = quote.best_ask ?? data?.ask ?? quote.ask;
  const lot = quote.lot_size != null ? ` 一手 ${quote.lot_size}` : "";
  const name = quote.name ? `${quote.name} ` : "";
  const quoteText = bid != null || ask != null ? `${name}${cleanSymbol} ${price}  买 ${bid ?? "--"} / 卖 ${ask ?? "--"}${lot}` : `${name}${cleanSymbol} ${price}${lot}`;
  target.textContent = quoteText;
  els.lastQuote.textContent = quoteText;
  addLocalEvent("quote", `报价 ${quoteText}`, "ok");
}

async function submitNormal(event) {
  event.preventDefault();
  const payload = normalPayload();
  if (payload.real && payload.confirm_text !== "确认实盘") {
    addLocalEvent("normal", "实盘下单前请输入：确认实盘", "warn");
    return;
  }
  setBusy(event.submitter, true);
  try {
    const data = await requestJson("/api/normal/order", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const intent = data?.intent || {};
    const prefix = data?.submitted ? "真实订单已提交" : "Dry-run 已生成";
    addLocalEvent("normal", `${prefix} ${intent.symbol || ""} ${intent.side || ""} ${intent.order_type || ""} qty=${intent.quantity || "--"} 金额=${intent.risk_notional || "--"}`, data?.submitted ? "warn" : "ok");
    refreshEvents();
  } catch (error) {
    addLocalEvent("normal", error.message, "error");
  } finally {
    setBusy(event.submitter, false);
  }
}

async function evaluateGrey(event) {
  event.preventDefault();
  const payload = greyPayload();
  const validation = validateGreyPayload(payload);
  if (validation) {
    setGreyStatus(validation, "error");
    addLocalEvent("grey", validation, "error");
    return;
  }
  if (payload.real) {
    if (payload.confirm_text !== "确认实盘" || !payload.acknowledge_real_order) {
      addLocalEvent("grey", "实盘暗盘抢单前必须输入：确认实盘，并勾选确认框", "warn");
      return;
    }
  }
  setBusy(event.submitter, true);
  try {
    const url = payload.real ? "/api/grey-open/start-live-real-buy-only" : "/api/grey/evaluate";
    setGreyStatus(
      payload.real
        ? "正在做实盘暗盘买入预检并启动布防..."
        : "正在读取 OpenD 暗盘报价和盘口，做一次试跑评估...",
      "warn",
    );
    const data = await requestJson(url, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const message = payload.real
      ? `实盘暗盘买入已布防 ${data.symbol || ""}，最大尝试 ${data.max_order_attempts || "--"}，超时 ${data.timeout_seconds || "--"} 秒`
      : `评估完成 ${compactJson(data)}`;
    state.armed = Boolean(payload.real);
    syncArmed();
    if (!payload.real) {
      const action = data?.decision?.action || "--";
      const reason = data?.decision?.reason || "--";
      setGreyStatus(`试跑完成：${action}，${reason}`, action === "ORDER" ? "warn" : "");
    }
    addLocalEvent("grey", data?.message || message, payload.real ? "warn" : "ok");
    refreshEvents();
  } catch (error) {
    setGreyStatus(error.message, "error");
    addLocalEvent("grey", error.message, "error");
  } finally {
    setBusy(event.submitter, false);
  }
}

async function toggleArm() {
  if (els.greyDryRun.checked) {
    setBusy(els.greyArmBtn, true);
    try {
      await startLiveDryRun();
    } catch (error) {
      setGreyStatus(error.message, "error");
      addLocalEvent("grey", error.message, "error");
    } finally {
      setBusy(els.greyArmBtn, false);
    }
    return;
  }
  evaluateGrey(new Event("submit"));
}

async function killSwitch() {
  setBusy(els.killSwitchBtn, true);
  try {
    const data = await requestJson("/api/kill-switch", { method: "POST", body: "{}" });
    state.armed = false;
    syncArmed();
    addLocalEvent("kill", data?.message || "停止指令已发送", "warn");
    refreshEvents();
  } catch (error) {
    addLocalEvent("kill", error.message, "error");
  } finally {
    setBusy(els.killSwitchBtn, false);
  }
}

async function refreshEvents() {
  try {
    const data = await requestJson("/api/events?limit=80");
    const events = Array.isArray(data) ? data : data?.events;
  if (Array.isArray(events)) {
      state.events = events.map((event) => ({
        time: event.time || event.ts || event.timestamp || "",
        type: event.event || event.type || event.source || event.level || "事件",
        message: event.message || event.msg || compactJson(event),
        level: event.level || event.severity || "ok",
      }));
      renderEvents(state.events);
    }
  } catch (error) {
    addLocalEvent("events", error.message, "error");
  }
}

async function refreshAll() {
  setBusy(els.refreshAllBtn, true);
  try {
    await Promise.allSettled([
      loadHealth(),
      refreshEvents(),
      refreshQuote(els.normalSymbol.value, els.normalQuote),
    ]);
  } finally {
    setBusy(els.refreshAllBtn, false);
  }
}

els.normalForm.addEventListener("submit", submitNormal);
els.greyForm.addEventListener("submit", evaluateGrey);
els.normalQuoteBtn.addEventListener("click", async () => {
  setBusy(els.normalQuoteBtn, true);
  try {
    await refreshQuote(els.normalSymbol.value, els.normalQuote);
  } catch (error) {
    addLocalEvent("quote", error.message, "error");
  } finally {
    setBusy(els.normalQuoteBtn, false);
  }
});
els.greyArmBtn.addEventListener("click", toggleArm);
els.killSwitchBtn.addEventListener("click", killSwitch);
els.validateConfigBtn?.addEventListener("click", () => validateConfig().catch((error) => addLocalEvent("config", error.message, "error")));
els.subscribeBtn?.addEventListener("click", () => subscribeMarket().catch((error) => addLocalEvent("subscribe", error.message, "error")));
els.dryRunOpenTriggerBtn?.addEventListener("click", () => evaluateGrey(new Event("submit")));
els.startLiveDryRunBtn?.addEventListener("click", () => startLiveDryRun().catch((error) => addLocalEvent("live", error.message, "error")));
els.stopLiveRunBtn?.addEventListener("click", () => stopLiveRun().catch((error) => addLocalEvent("live", error.message, "error")));
els.createKillSwitchBtn?.addEventListener("click", () => requestJson("/api/kill-switch/create", { method: "POST", body: "{}" }).then(loadHealth).catch((error) => addLocalEvent("kill", error.message, "error")));
els.clearKillSwitchBtn?.addEventListener("click", () => requestJson("/api/kill-switch/clear", { method: "POST", body: "{}" }).then(loadHealth).catch((error) => addLocalEvent("kill", error.message, "error")));
els.seedInventoryBtn?.addEventListener("click", () => seedInventory().catch((error) => addLocalEvent("inventory", error.message, "error")));
els.resetInventoryBtn?.addEventListener("click", () => resetInventory().catch((error) => addLocalEvent("inventory", error.message, "error")));
els.reconcileInventoryBtn?.addEventListener("click", () => reconcileInventory().catch((error) => addLocalEvent("inventory", error.message, "error")));
els.costReducerConfigForm?.addEventListener("submit", applyCostReducerConfig);
els.replayForm?.addEventListener("submit", runReplay);
els.eventsRefreshBtn.addEventListener("click", refreshEvents);
els.refreshAllBtn.addEventListener("click", refreshAll);
els.normalDryRun.addEventListener("change", syncModes);
els.greyDryRun.addEventListener("change", syncModes);
els.normalForm.querySelectorAll('input[name="order_type"]').forEach((input) => {
  input.addEventListener("change", syncOrderType);
});

syncModes();
syncOrderType();
syncArmed();
renderEvents([]);
refreshAll();
setInterval(loadHealth, 15000);
setInterval(refreshEvents, 6000);
