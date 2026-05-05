const state = {
  armed: false,
  events: [],
  autoGreyMaxNotional: "",
  greyMaxNotionalEdited: false,
};

const MAX_GREY_SYMBOLS = 5;
const GREY_ARM_TIMEOUT_SECONDS = 14400;

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
  greyMaxPrice: $("#greyMaxPrice"),
  greyQuantity: $("#greyQuantity"),
  greyMaxNotional: $("#greyMaxNotional"),
  greyState: $("#greyState"),
  greyEvaluateBtn: $("#greyEvaluateBtn"),
  greyArmBtn: $("#greyArmBtn"),
  greyRealArmBtn: $("#greyRealArmBtn"),
  greyResult: $("#greyResult"),
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
  resetReadyBtn: $("#resetReadyBtn"),
  restartBtn: $("#restartBtn"),
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

function setResetBusy(busy) {
  setBusy(els.restartBtn, busy);
  setBusy(els.resetReadyBtn, busy);
}

function delay(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
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

function formatMoney(value) {
  if (!Number.isFinite(value)) return "";
  return value.toFixed(2);
}

function autoFillGreyMaxNotional({ force = false } = {}) {
  if (!els.greyMaxNotional) return;
  const maxPrice = numberOrNull(els.greyMaxPrice?.value);
  const quantity = numberOrNull(els.greyQuantity?.value);
  if (!(maxPrice > 0) || !(quantity > 0)) return;

  const calculated = formatMoney(maxPrice * quantity);
  const current = els.greyMaxNotional.value.trim();
  const shouldUpdate =
    force ||
    !state.greyMaxNotionalEdited ||
    current === "" ||
    current === state.autoGreyMaxNotional;

  state.autoGreyMaxNotional = calculated;
  if (shouldUpdate) {
    els.greyMaxNotional.value = calculated;
    state.greyMaxNotionalEdited = false;
  }
}

function markGreyMaxNotionalEdited() {
  const current = els.greyMaxNotional?.value.trim() || "";
  state.greyMaxNotionalEdited =
    current !== "" && current !== state.autoGreyMaxNotional;
  if (!state.greyMaxNotionalEdited) {
    autoFillGreyMaxNotional();
  }
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

function parseGreySymbols(raw) {
  const seen = new Set();
  return String(raw || "")
    .split(/[\s,，;；]+/)
    .map((symbol) => symbol.trim().toUpperCase())
    .filter(Boolean)
    .map((symbol) => (symbol.includes(".") ? symbol : `HK.${symbol}`))
    .filter((symbol) => {
      if (seen.has(symbol)) return false;
      seen.add(symbol);
      return true;
    });
}

function greySymbols() {
  const primarySymbol = $("#greySymbol")?.value || "";
  const setupSymbol = $("#setupSymbol")?.value || "";
  return parseGreySymbols(primarySymbol || setupSymbol);
}

function greyPayload(symbolOverride = "", options = {}) {
  const symbols = greySymbols();
  const quantity = numberOrNull($("#greyQuantity")?.value) ?? numberOrNull($("#setupQuantity")?.value);
  const maxPrice = numberOrNull($("#greyMaxPrice")?.value) ?? numberOrNull($("#setupMaxPrice")?.value);
  const maxNotional = numberOrNull($("#greyMaxNotional")?.value) ?? numberOrNull($("#setupMaxNotional")?.value);
  const maxAttempts = numberOrNull($("#greyMaxAttempts")?.value) ?? numberOrNull($("#setupMaxOrderAttempts")?.value);
  const coolDownMs = numberOrNull($("#greyCoolDown")?.value) ?? numberOrNull($("#setupCoolDownMs")?.value);
  const real = options.real ?? !els.greyDryRun.checked;
  return {
    symbol: symbolOverride || symbols[0] || "",
    symbols,
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
    real,
    real_mode: real,
    acknowledge_real_order: Boolean(els.greyAckReal?.checked),
    confirm_text: els.greyConfirmText?.value.trim() || "",
    timeout_seconds: GREY_ARM_TIMEOUT_SECONDS,
    poll_interval_ms: 50,
  };
}

function validateGreyPayload(payload) {
  if (!payload.symbols?.length) return "请输入暗盘标的代码。";
  if (payload.symbols.length > MAX_GREY_SYMBOLS) return `一次最多同时盯 ${MAX_GREY_SYMBOLS} 个暗盘代码。`;
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

function renderGreyResult(title, rows = [], message = "") {
  if (!els.greyResult) return;
  els.greyResult.innerHTML = "";

  const heading = document.createElement("div");
  heading.className = "result-title";
  heading.textContent = title;
  els.greyResult.append(heading);

  if (message) {
    const paragraph = document.createElement("p");
    paragraph.textContent = message;
    els.greyResult.append(paragraph);
  }

  if (!rows.length) return;

  const list = document.createElement("div");
  list.className = "result-list";
  rows.forEach((row) => {
    const item = document.createElement("div");
    item.className = `result-row ${row.level || ""}`.trim();

    const symbol = document.createElement("strong");
    symbol.textContent = row.symbol || "--";

    const action = document.createElement("span");
    action.textContent = row.action || "--";

    const reason = document.createElement("span");
    reason.textContent = row.reason || "--";

    const market = document.createElement("span");
    market.textContent = row.market || "";

    item.append(symbol, action, reason, market);
    list.append(item);
  });
  els.greyResult.append(list);
}

function greyLevelFromAction(action) {
  if (action === "ORDER") return "warn";
  if (action === "BLOCK") return "error";
  return "ok";
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
  els.greyModeText.textContent = "默认模拟";
  els.greyModeText.className = "";
  if (els.greyEvaluateBtn) {
    els.greyEvaluateBtn.textContent = "1. 试跑行情";
  }
  if (els.greyArmBtn) {
    els.greyArmBtn.textContent = "2. 模拟布防";
    els.greyArmBtn.classList.add("primary-button");
    els.greyArmBtn.classList.remove("danger-button");
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
  const payload = greyPayload("", { real: false });
  const validation = validateGreyPayload(payload);
  if (validation) {
    setGreyStatus(validation, "error");
    addLocalEvent("grey", validation, "error");
    renderGreyResult("模拟布防未启动", [], validation);
    return;
  }
  setGreyStatus(`正在启动 ${payload.symbols.length} 个代码的实时模拟盯盘，不会真实下单...`, "warn");
  renderGreyResult(
    "模拟布防启动中",
    payload.symbols.map((symbol) => ({
      symbol,
      action: "WAIT",
      reason: "等待 OpenD 暗盘状态变为 TRADING",
      market: "不会真实下单",
      level: "warn",
    })),
  );
  const results = await Promise.allSettled(
    payload.symbols.map((symbol) => requestJson("/api/grey-open/start-live-dry-run", {
      method: "POST",
      body: JSON.stringify({ ...payload, symbol }),
    })),
  );
  const ok = results.filter((result) => result.status === "fulfilled");
  const failed = results.filter((result) => result.status === "rejected");
  state.armed = ok.length > 0;
  syncArmed();
  if (failed.length) {
    const message = `${ok.length} 个已启动，${failed.length} 个失败：${failed.map((result) => result.reason.message).join("；")}`;
    setGreyStatus(message, ok.length ? "warn" : "error");
    addLocalEvent("live", message, ok.length ? "warn" : "error");
    renderGreyResult("模拟布防结果", [
      ...ok.map((result) => ({
        symbol: result.value.symbol,
        action: "ARMED",
        reason: "后台等待暗盘 TRADING",
        market: "模拟",
        level: "warn",
      })),
      ...failed.map((result) => ({
        symbol: "--",
        action: "FAILED",
        reason: result.reason.message,
        market: "",
        level: "error",
      })),
    ]);
    return;
  }
  const symbols = ok.map((result) => result.value.symbol).join(", ");
  setGreyStatus(`模拟盯盘已启动：${symbols}`, "warn");
  addLocalEvent("live", `实时模拟盯盘已启动：${symbols}`, "warn");
  renderGreyResult("模拟布防已启动", ok.map((result) => ({
    symbol: result.value.symbol,
    action: "ARMED",
    reason: "后台等待 dark_status = TRADING；触发后只记录 would-submit",
    market: `timeout ${result.value.timeout_seconds}s`,
    level: "warn",
  })));
}

async function stopLiveRun() {
  const data = await requestJson("/api/grey-open/stop", { method: "POST", body: "{}" });
  addLocalEvent("live", `live run stopped=${!data.running}`, "ok");
  state.armed = false;
  syncArmed();
  renderGreyResult("已发送停止指令", [], "Kill switch 已创建；旧布防线程会在下一轮检查时退出。");
}

async function restart() {
  setResetBusy(true);
  try {
    let data = null;
    for (let attempt = 0; attempt < 4; attempt += 1) {
      data = await requestJson("/api/restart", { method: "POST", body: "{}" });
      if (data.ready !== false) break;
      state.armed = false;
      syncArmed();
      const message = data.message || "旧布防线程仍在退出中，正在继续等待。";
      setGreyStatus(message, "warn");
      renderGreyResult("Reset 等待中", [], message);
      addLocalEvent("restart", message, "warn");
      await delay(1000);
    }
    state.armed = false;
    syncArmed();
    if (data?.ready === false) {
      const message = data.message || "旧布防线程仍在退出中；Kill Switch 已保持。";
      setGreyStatus(message, "warn");
      renderGreyResult("Reset 未完成", [], message);
      addLocalEvent("restart", message, "warn");
    } else {
      const message = data?.message || "Kill Switch 已清除，可以重新布防。";
      setGreyStatus(message, "ok");
      renderGreyResult("已恢复可布防", [], message);
      addLocalEvent("restart", message, "ok");
    }
    await loadHealth();
    refreshEvents();
  } catch (error) {
    setGreyStatus(error.message, "error");
    addLocalEvent("restart", error.message, "error");
    renderGreyResult("Reset 失败", [], error.message);
  } finally {
    setResetBusy(false);
  }
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
  const payload = greyPayload("", { real: false });
  const validation = validateGreyPayload(payload);
  if (validation) {
    setGreyStatus(validation, "error");
    addLocalEvent("grey", validation, "error");
    renderGreyResult("试跑未执行", [], validation);
    return;
  }
  setBusy(event.submitter, true);
  try {
    setGreyStatus(
      `正在读取 ${payload.symbols.length} 个代码的 OpenD 暗盘报价和盘口，做一次试跑评估...`,
      "warn",
    );
    renderGreyResult("试跑中", payload.symbols.map((symbol) => ({
      symbol,
      action: "READ",
      reason: "读取报价与一档盘口",
      market: "OpenD",
      level: "warn",
    })));
    const results = await Promise.allSettled(
      payload.symbols.map((symbol) => requestJson("/api/grey/evaluate", {
        method: "POST",
        body: JSON.stringify({ ...payload, symbol }),
      })),
    );
    const ok = results.filter((result) => result.status === "fulfilled");
    const failed = results.filter((result) => result.status === "rejected");
    state.armed = false;
    syncArmed();
    if (failed.length) {
      const message = `${ok.length} 个成功，${failed.length} 个失败：${failed.map((result) => result.reason.message).join("；")}`;
      setGreyStatus(message, ok.length ? "warn" : "error");
      addLocalEvent("grey", message, ok.length ? "warn" : "error");
      renderGreyResult("试跑结果", [
        ...ok.map((result) => {
          const data = result.value;
          const action = data?.decision?.action || "--";
          const quote = data?.signal || {};
          return {
            symbol: quote.symbol || "--",
            action,
            reason: data?.decision?.reason || "--",
            market: `暗盘 ${quote.dark_status || "--"} / 卖一 ${quote.best_ask ?? "--"}`,
            level: greyLevelFromAction(action),
          };
        }),
        ...failed.map((result) => ({
          symbol: "--",
          action: "FAILED",
          reason: result.reason.message,
          market: "",
          level: "error",
        })),
      ]);
      return;
    }
    const rows = ok.map((result) => {
      const data = result.value;
      const action = data?.decision?.action || "--";
      const reason = data?.decision?.reason || "--";
      const quote = data?.signal || {};
      const symbol = quote.symbol || data?.decision?.intent?.symbol || "--";
      return {
        symbol,
        action,
        reason,
        market: `暗盘 ${quote.dark_status || "--"} / 买一 ${quote.best_bid ?? "--"} / 卖一 ${quote.best_ask ?? "--"}`,
        level: greyLevelFromAction(action),
      };
    });
    const messages = rows.map((row) => `${row.symbol}: ${row.action} / ${row.reason}`);
    setGreyStatus(`试跑完成：${messages.join("；")}`, rows.some((row) => row.action === "ORDER") ? "warn" : "");
    renderGreyResult(
      "试跑完成",
      rows,
      rows.some((row) => row.action === "ORDER")
        ? "当前规则已经满足下单条件；实盘仍需点击红色按钮单独布防。"
        : "没有真实下单。若原因是 dark_status 不是 TRADING，说明系统会继续等暗盘开盘。",
    );
    addLocalEvent("grey", `试跑完成：${messages.join("；")}`, "ok");
    refreshEvents();
  } catch (error) {
    setGreyStatus(error.message, "error");
    addLocalEvent("grey", error.message, "error");
    renderGreyResult("试跑失败", [], error.message);
  } finally {
    setBusy(event.submitter, false);
  }
}

async function startLiveRealBuy() {
  const payload = greyPayload("", { real: true });
  const validation = validateGreyPayload(payload);
  if (validation) {
    setGreyStatus(validation, "error");
    addLocalEvent("grey", validation, "error");
    renderGreyResult("实盘布防未启动", [], validation);
    return;
  }
  if (payload.confirm_text !== "确认实盘" || !payload.acknowledge_real_order) {
    const message = "实盘暗盘抢单前必须输入：确认实盘，并勾选确认框";
    setGreyStatus(message, "error");
    addLocalEvent("grey", message, "warn");
    renderGreyResult("实盘布防未启动", [], message);
    return;
  }

  setBusy(els.greyRealArmBtn, true);
  try {
    setGreyStatus(`正在启动 ${payload.symbols.length} 个代码的实盘暗盘买入布防...`, "warn");
    renderGreyResult("实盘布防启动中", payload.symbols.map((symbol) => ({
      symbol,
      action: "ARMING",
      reason: "服务端将再次检查 FUTU_ALLOW_REAL_TRADE、确认短语、kill switch、金额和次数限制",
      market: "真实限价买入",
      level: "warn",
    })));
    const results = await Promise.allSettled(
      payload.symbols.map((symbol) => requestJson("/api/grey-open/start-live-real-buy-only", {
        method: "POST",
        body: JSON.stringify({ ...payload, symbol }),
      })),
    );
    const ok = results.filter((result) => result.status === "fulfilled");
    const failed = results.filter((result) => result.status === "rejected");
    state.armed = ok.length > 0;
    syncArmed();
    const rows = [
      ...ok.map((result) => ({
        symbol: result.value.symbol,
        action: "REAL_ARMED",
        reason: "等待 dark_status = TRADING；满足规则后提交真实限价买单",
        market: `最多 ${result.value.max_order_attempts} 次 / timeout ${result.value.timeout_seconds}s`,
        level: "warn",
      })),
      ...failed.map((result) => ({
        symbol: "--",
        action: "BLOCKED",
        reason: result.reason.message,
        market: "未启动",
        level: "error",
      })),
    ];
    if (failed.length) {
      const message = `${ok.length} 个已布防，${failed.length} 个被拦截：${failed.map((result) => result.reason.message).join("；")}`;
      setGreyStatus(message, ok.length ? "warn" : "error");
      addLocalEvent("grey", message, ok.length ? "warn" : "error");
      renderGreyResult("实盘布防结果", rows);
      return;
    }
    const symbols = ok.map((result) => result.value.symbol).join(", ");
    setGreyStatus(`实盘暗盘买入已布防：${symbols}`, "warn");
    addLocalEvent("grey", `实盘暗盘买入已布防：${symbols}`, "warn");
    renderGreyResult("实盘暗盘买入已布防", rows, "现在不要重复点击；如需停止，点一键停止，它会创建 kill switch。");
    refreshEvents();
  } catch (error) {
    setGreyStatus(error.message, "error");
    addLocalEvent("grey", error.message, "error");
    renderGreyResult("实盘布防失败", [], error.message);
  } finally {
    setBusy(els.greyRealArmBtn, false);
  }
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
els.greyEvaluateBtn?.addEventListener("click", (event) => evaluateGrey(event));
els.greyArmBtn.addEventListener("click", async () => {
  setBusy(els.greyArmBtn, true);
  try {
    await startLiveDryRun();
  } catch (error) {
    setGreyStatus(error.message, "error");
    addLocalEvent("grey", error.message, "error");
    renderGreyResult("模拟布防失败", [], error.message);
  } finally {
    setBusy(els.greyArmBtn, false);
  }
});
els.greyRealArmBtn?.addEventListener("click", startLiveRealBuy);
els.killSwitchBtn.addEventListener("click", killSwitch);
els.validateConfigBtn?.addEventListener("click", () => validateConfig().catch((error) => addLocalEvent("config", error.message, "error")));
els.subscribeBtn?.addEventListener("click", () => subscribeMarket().catch((error) => addLocalEvent("subscribe", error.message, "error")));
els.dryRunOpenTriggerBtn?.addEventListener("click", () => evaluateGrey(new Event("submit")));
els.startLiveDryRunBtn?.addEventListener("click", () => startLiveDryRun().catch((error) => addLocalEvent("live", error.message, "error")));
els.stopLiveRunBtn?.addEventListener("click", () => stopLiveRun().catch((error) => addLocalEvent("live", error.message, "error")));
els.createKillSwitchBtn?.addEventListener("click", () => requestJson("/api/kill-switch/create", { method: "POST", body: "{}" }).then(loadHealth).catch((error) => addLocalEvent("kill", error.message, "error")));
els.clearKillSwitchBtn?.addEventListener("click", () => requestJson("/api/kill-switch/clear", { method: "POST", body: "{}" }).then(loadHealth).catch((error) => addLocalEvent("kill", error.message, "error")));
els.resetReadyBtn?.addEventListener("click", restart);
els.restartBtn?.addEventListener("click", restart);
els.seedInventoryBtn?.addEventListener("click", () => seedInventory().catch((error) => addLocalEvent("inventory", error.message, "error")));
els.resetInventoryBtn?.addEventListener("click", () => resetInventory().catch((error) => addLocalEvent("inventory", error.message, "error")));
els.reconcileInventoryBtn?.addEventListener("click", () => reconcileInventory().catch((error) => addLocalEvent("inventory", error.message, "error")));
els.costReducerConfigForm?.addEventListener("submit", applyCostReducerConfig);
els.replayForm?.addEventListener("submit", runReplay);
els.eventsRefreshBtn.addEventListener("click", refreshEvents);
els.refreshAllBtn.addEventListener("click", refreshAll);
els.normalDryRun.addEventListener("change", syncModes);
els.greyDryRun.addEventListener("change", syncModes);
els.greyMaxPrice?.addEventListener("input", () => autoFillGreyMaxNotional());
els.greyQuantity?.addEventListener("input", () => autoFillGreyMaxNotional());
els.greyMaxNotional?.addEventListener("input", markGreyMaxNotionalEdited);
els.normalForm.querySelectorAll('input[name="order_type"]').forEach((input) => {
  input.addEventListener("change", syncOrderType);
});

syncModes();
syncOrderType();
syncArmed();
autoFillGreyMaxNotional();
renderEvents([]);
refreshAll();
setInterval(loadHealth, 15000);
setInterval(refreshEvents, 6000);
