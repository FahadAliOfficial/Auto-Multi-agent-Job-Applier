const PROTOCOL = 1;
let socket = null;
let reconnectTimer = null;
let reconnectDelayMs = 1000;
const dedicatedTabs = new Set();
const restoredTabs = chrome.storage.session.get("dedicatedTabIds").then(({dedicatedTabIds}) => {
  for (const tabId of dedicatedTabIds || []) dedicatedTabs.add(tabId);
});

async function saveDedicatedTabs() {
  await chrome.storage.session.set({dedicatedTabIds: [...dedicatedTabs]});
}

async function rememberTab(tabId) {
  dedicatedTabs.add(tabId);
  await saveDedicatedTabs();
}

async function forgetTab(tabId) {
  dedicatedTabs.delete(tabId);
  await saveDedicatedTabs();
}

chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());
chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === "reconnect") reconnect();
});
chrome.alarms.create("bridge-heartbeat", {periodInMinutes: 0.33});
chrome.alarms.onAlarm.addListener(() => {
  if (socket?.readyState === WebSocket.OPEN) {
    safeSend(socket, {type: "event", event: "heartbeat"});
  } else {
    connect();
  }
});

chrome.tabs.onCreated.addListener((tab) => {
  restoredTabs.then(async () => {
    if (!dedicatedTabs.has(tab.openerTabId)) return;
    await rememberTab(tab.id);
    sendEvent("tab_created", tabData(tab));
  });
});
chrome.tabs.onRemoved.addListener((tabId) => forgetTab(tabId));

function sendEvent(event, tab) {
  safeSend(socket, {type: "event", event, tab});
}

function safeSend(ws, payload) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  try {
    ws.send(JSON.stringify(payload));
    return true;
  } catch (_) {
    return false;
  }
}

function tabData(tab) {
  return {
    tabId: tab.id,
    windowId: tab.windowId,
    openerTabId: tab.openerTabId,
    url: tab.url || tab.pendingUrl || ""
  };
}

async function connect() {
  if (socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(socket.readyState)) return;
  const cfg = await chrome.storage.local.get(["bridgeUrl", "token"]);
  if (!cfg.token) return;
  const bridgeUrl = cfg.bridgeUrl || "ws://127.0.0.1:8765";
  if (!/^ws:\/\/(127\.0\.0\.1|localhost|\[::1\]):\d+$/.test(bridgeUrl)) return;
  const ws = new WebSocket(bridgeUrl);
  socket = ws;
  ws.addEventListener("open", () => {
    if (socket !== ws) return;
    reconnectDelayMs = 1000;
    safeSend(ws, {type: "hello", protocol: PROTOCOL, token: cfg.token});
  });
  ws.addEventListener("message", async (event) => {
    if (socket !== ws) return;
    const message = JSON.parse(event.data);
    if (message.type !== "command") return;
    try {
      const result = await handleCommand(message);
      safeSend(ws, {type: "response", id: message.id, ok: true, result});
    } catch (error) {
      safeSend(ws, {
        type: "response", id: message.id, ok: false,
        error: error?.message || String(error)
      });
    }
  });
  ws.addEventListener("close", () => {
    if (socket !== ws) return;
    socket = null;
    scheduleReconnect();
  });
  ws.addEventListener("error", () => {
    if ([WebSocket.CONNECTING, WebSocket.OPEN].includes(ws.readyState)) ws.close();
  });
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, reconnectDelayMs);
  reconnectDelayMs = Math.min(reconnectDelayMs * 2, 30000);
}

function reconnect() {
  clearTimeout(reconnectTimer);
  const previous = socket;
  socket = null;
  if (
    previous
    && [WebSocket.CONNECTING, WebSocket.OPEN].includes(previous.readyState)
  ) {
    previous.close();
  }
  reconnectDelayMs = 1000;
  connect();
}

async function getTab(tabId) {
  const tab = await chrome.tabs.get(tabId);
  return tabData(tab);
}

async function waitForLoad(tabId, timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === "complete") return tabData(tab);
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error("Timed out waiting for tab to load");
}

async function handleCommand(message) {
  await restoredTabs;
  const tabId = message.tabId;
  switch (message.command) {
    case "ensure_tab": {
      const saved = await chrome.storage.local.get("dedicatedTabId");
      if (message.reuse && saved.dedicatedTabId) {
        try {
          const tab = await chrome.tabs.get(saved.dedicatedTabId);
          await rememberTab(tab.id);
          await chrome.tabs.update(tab.id, {active: true});
          return tabData(tab);
        } catch (_) {}
      }
      const tab = await chrome.tabs.create({url: message.url, active: true});
      await rememberTab(tab.id);
      await chrome.storage.local.set({dedicatedTabId: tab.id});
      return tabData(tab);
    }
    case "new_tab": {
      const tab = await chrome.tabs.create({url: message.url || "about:blank", active: false});
      await rememberTab(tab.id);
      return tabData(tab);
    }
    case "navigate":
      await chrome.tabs.update(tabId, {url: message.url});
      return await waitForLoad(tabId, message.timeout);
    case "wait_for_load":
      return await waitForLoad(tabId, message.timeout);
    case "activate": {
      const tab = await chrome.tabs.get(tabId);
      await chrome.windows.update(tab.windowId, {focused: true});
      await chrome.tabs.update(tabId, {active: true});
      return tabData(tab);
    }
    case "close_tab":
      await chrome.tabs.remove(tabId);
      await forgetTab(tabId);
      return {tabId, closed: true};
    case "screenshot": {
      if (!dedicatedTabs.has(tabId)) throw new Error("Refusing to capture a non-dedicated tab");
      const settings = await chrome.storage.local.get("screenshotsEnabled");
      const capturePermission = {origins: ["<all_urls>"]};
      if (
        !settings.screenshotsEnabled
        || !await chrome.permissions.contains(capturePermission)
      ) {
        throw new Error("Screenshots are disabled; enable them in extension Options");
      }
      const tab = await chrome.tabs.get(tabId);
      const previous = await chrome.tabs.query({active: true, windowId: tab.windowId});
      if (!tab.active) await chrome.tabs.update(tabId, {active: true});
      try {
        const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {format: "png"});
        return {data: dataUrl.split(",", 2)[1] || ""};
      } finally {
        if (!tab.active && previous[0]?.id && previous[0].id !== tabId) {
          await chrome.tabs.update(previous[0].id, {active: true});
        }
      }
    }
    case "dom": {
      if (!dedicatedTabs.has(tabId)) throw new Error("Refusing to control a non-dedicated tab");
      const results = await chrome.scripting.executeScript({
        // Indeed Easy Apply is commonly hosted in a cross-frame application.
        // Execute in every permitted frame and prefer the apply frame below.
        target: {tabId, allFrames: true},
        func: runDomOperation,
        args: [message.descriptor, message.operation, message.payload || {}]
      });
      const successful = results.filter((item) => item.result?.ok);
      if (!successful.length) {
        const error = results.find((item) => item.result?.error)?.result?.error;
        throw new Error(error || "DOM operation failed");
      }
      const meaningful = (item) => {
        const value = item.result.value;
        return value !== 0 && value !== false && value !== null && value !== ""
          && (!Array.isArray(value) || value.length > 0);
      };
      const isApplyFrame = (item) => {
        const frame = item.result.frame || {};
        return !frame.isTop && /indeedapply|smartapply|apply[-_.\/]?form/i.test(
          `${frame.href || ""} ${frame.title || ""} ${frame.name || ""}`
        );
      };
      const chosen =
        successful
          .filter((item) => meaningful(item) && item.result.frame?.applicationScore > 0)
          .sort((a, b) =>
            b.result.frame.applicationScore - a.result.frame.applicationScore
          )[0]
        || successful.find((item) => isApplyFrame(item) && meaningful(item))
        || successful.find((item) => item.result.frame?.isTop && meaningful(item))
        || successful.find(meaningful)
        || successful.find((item) => item.result.frame?.isTop)
        || successful[0];
      return {...chosen.result, url: (await getTab(tabId)).url};
    }
    default:
      throw new Error(`Unknown command: ${message.command}`);
  }
}

async function runDomOperation(descriptor, operation, payload) {
  try {
    const visible = (el) => {
      if (!el || !el.isConnected) return false;
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden"
        && rect.width > 0 && rect.height > 0;
    };
    const matcher = (spec) => {
      if (spec && typeof spec === "object" && spec.regex !== undefined) {
        const regex = new RegExp(spec.regex, spec.flags || "");
        return (value) => regex.test((value || "").trim());
      }
      return (value) => (value || "").trim().includes(String(spec || ""));
    };
    const splitSelectors = (selector) => {
      const parts = []; let part = ""; let quote = ""; let depth = 0;
      for (const ch of selector) {
        if (quote) {
          part += ch;
          if (ch === quote) quote = "";
        } else if (ch === "'" || ch === '"') {
          quote = ch; part += ch;
        } else if ("([".includes(ch)) {
          depth++; part += ch;
        } else if (")]".includes(ch)) {
          depth--; part += ch;
        } else if (ch === "," && depth === 0) {
          parts.push(part.trim()); part = "";
        } else part += ch;
      }
      if (part.trim()) parts.push(part.trim());
      return parts;
    };
    const cssQuery = (root, selector) => {
      const found = [];
      for (let part of splitSelectors(selector)) {
        let text = null;
        part = part.replace(/:has-text\((['"])(.*?)\1\)/g, (_, _q, value) => {
          text = value; return "";
        });
        const needsVisible = part.includes(":visible");
        part = part.replace(/:visible/g, "") || "*";
        let nodes;
        try { nodes = [...root.querySelectorAll(part)]; } catch (_) { nodes = []; }
        if (text !== null) nodes = nodes.filter((el) => (el.innerText || el.textContent || "").includes(text));
        if (needsVisible) nodes = nodes.filter(visible);
        found.push(...nodes);
      }
      return [...new Set(found)];
    };
    const implicitRole = (el) => {
      const explicit = el.getAttribute("role");
      if (explicit) return explicit;
      const tag = el.tagName.toLowerCase();
      const type = (el.getAttribute("type") || "").toLowerCase();
      if (tag === "button" || (tag === "input" && ["button", "submit", "reset"].includes(type))) return "button";
      if (tag === "a" && el.hasAttribute("href")) return "link";
      if (/^h[1-6]$/.test(tag)) return "heading";
      if (tag === "textarea" || (tag === "input" && !["button", "submit", "checkbox", "radio", "file"].includes(type))) return "textbox";
      if (tag === "select") return "combobox";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      return "";
    };
    const accessibleName = (el) => el.getAttribute("aria-label")
      || el.innerText || el.value || el.textContent || "";
    const resolve = () => {
      if (descriptor.kind === "page") return [document];
      let nodes = [document];
      for (const step of descriptor.steps || []) {
        if (step.kind === "css") {
          nodes = nodes.flatMap((root) => cssQuery(root, step.selector));
        } else if (step.kind === "role") {
          const matches = matcher(step.name);
          nodes = nodes.flatMap((root) => [...root.querySelectorAll("*")].filter(
            (el) => implicitRole(el) === step.role && (!step.name || matches(accessibleName(el)))
          ));
        } else if (step.kind === "text") {
          const matches = matcher(step.text);
          nodes = nodes.flatMap((root) => [...root.querySelectorAll("*")].filter((el) => {
            const text = (el.innerText || el.textContent || "").trim();
            if (!matches(text)) return false;
            if (step.exact && typeof step.text === "string" && text !== step.text) return false;
            return ![...el.children].some((child) => matches((child.innerText || "").trim()));
          }));
        } else if (step.kind === "filter_text") {
          const matches = matcher(step.text);
          nodes = nodes.filter((el) => matches(el.innerText || el.textContent || ""));
        } else if (step.kind === "closest") {
          nodes = nodes.map((el) => el.closest(step.selector)).filter(Boolean);
        } else if (step.kind === "index") {
          const index = step.index < 0 ? nodes.length + step.index : step.index;
          nodes = nodes[index] ? [nodes[index]] : [];
        }
      }
      return nodes;
    };
    const first = () => {
      const el = resolve()[0];
      if (!el) throw new Error("Locator did not match an element");
      return el;
    };
    const setValue = (el, value) => {
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
      if (setter) setter.call(el, value); else el.value = value;
      el.dispatchEvent(new Event("input", {bubbles: true}));
      el.dispatchEvent(new Event("change", {bubbles: true}));
    };

    let value;
    if (operation === "count") value = resolve().length;
    else if (operation === "inner_text") value = first().innerText || first().textContent || "";
    else if (operation === "text_content") value = first().textContent;
    else if (operation === "get_attribute") value = first().getAttribute(payload.name);
    else if (operation === "input_value") value = first().value || "";
    else if (operation === "is_visible") value = visible(resolve()[0]);
    else if (operation === "is_enabled") {
      const el = resolve()[0]; value = Boolean(el && !el.disabled && el.getAttribute("aria-disabled") !== "true");
    } else if (operation === "is_checked") value = Boolean(resolve()[0]?.checked);
    else if (operation === "click") {
      const el = first(); el.scrollIntoView({block: "center"}); el.click(); value = true;
    } else if (operation === "fill") {
      const el = first(); el.focus(); setValue(el, payload.value); value = true;
    } else if (operation === "check") {
      const el = first(); if (!el.checked) el.click(); value = true;
    } else if (operation === "scroll") {
      first().scrollIntoView({block: "center"}); value = true;
    } else if (operation === "select_option") {
      const el = first();
      const option = [...el.options].find((item) =>
        payload.label != null ? item.text.trim() === String(payload.label) : item.value === String(payload.value)
      );
      if (!option) throw new Error("Select option was not found");
      el.value = option.value;
      el.dispatchEvent(new Event("input", {bubbles: true}));
      el.dispatchEvent(new Event("change", {bubbles: true}));
      value = [option.value];
    } else if (operation === "press") {
      const el = descriptor.kind === "page" ? document.activeElement : first();
      const key = payload.key;
      if (key === "Control+A") el?.select?.();
      else if (key === "Backspace") setValue(el, "");
      else if (key === "Escape") el?.blur?.();
      else if (key?.length === 1) setValue(el, (el.value || "") + key);
      el?.dispatchEvent(new KeyboardEvent("keydown", {key, bubbles: true}));
      el?.dispatchEvent(new KeyboardEvent("keyup", {key, bubbles: true}));
      value = true;
    } else if (operation === "set_files") {
      const bytes = Uint8Array.from(atob(payload.data), (ch) => ch.charCodeAt(0));
      const file = new File([bytes], payload.name, {type: payload.mime});
      const transfer = new DataTransfer(); transfer.items.add(file);
      const el = first(); el.files = transfer.files;
      el.dispatchEvent(new Event("input", {bubbles: true}));
      el.dispatchEvent(new Event("change", {bubbles: true}));
      value = true;
    } else if (operation === "wait") {
      const deadline = Date.now() + (payload.timeout || 30000);
      do {
        const nodes = resolve();
        if (payload.state === "attached" ? nodes.length : nodes.some(visible)) { value = true; break; }
        await new Promise((resolveWait) => setTimeout(resolveWait, 100));
      } while (Date.now() < deadline);
      if (!value) throw new Error("Timed out waiting for locator");
    } else if (operation === "wait_ready") {
      const deadline = Date.now() + (payload.timeout || 30000);
      do {
        const text = (document.body?.innerText || "").toLowerCase();
        const fields = [...document.querySelectorAll(
          "input:not([type=hidden]):not([type=submit]):not([type=button]),select,textarea,[role=combobox],[aria-haspopup=listbox]"
        )].filter(visible);
        const actions = [...document.querySelectorAll(
          "button,a,[role=button],input[type=submit],input[type=button]"
        )].filter((el) => {
          if (!visible(el) || el.disabled || el.getAttribute("aria-disabled") === "true") {
            return false;
          }
          const label = (
            el.innerText || el.value || el.getAttribute("aria-label") || ""
          ).trim().toLowerCase();
          return /^(continue|next|review|review your application|submit your application|submit application|submit|apply anyway)$/.test(label)
            && !/save and close|report/.test(label);
        });
        if (/application submitted|you have applied|application sent|add a resume|upload a resume/.test(text)
            || /(?:don.t|do not) meet (?:these|the) employer requirements/.test(text)
            || fields.length > 0
            || actions.length > 0) { value = true; break; }
        await new Promise((resolveWait) => setTimeout(resolveWait, 100));
      } while (Date.now() < deadline);
      if (!value) throw new Error("Timed out waiting for page readiness");
    } else if (operation === "evaluate_known") {
      const source = payload.expression || "";
      const el = descriptor.kind === "page" ? null : first();
      if (source.includes("window.scrollTo")) {
        window.scrollTo(0, document.body.scrollHeight); value = null;
      } else if (source.includes("selectedIndex")) {
        const select = document.querySelector(payload.argument);
        value = select?.options?.[select.selectedIndex]?.text?.trim() || "";
      } else if (source.includes("setAttribute('data-bot-field-id'")) {
        el.setAttribute("data-bot-field-id", payload.argument); value = null;
      } else if (source.includes("tagName.toLowerCase")) value = el.tagName.toLowerCase();
      else if (source.includes("Array.from(el.options)")) {
        value = [...el.options].map((item) => item.textContent.trim()).filter(Boolean);
      } else if (source.includes("tagName === 'FIELDSET'")) {
        value = el.closest("fieldset")?.querySelector("legend")?.textContent?.trim() || "";
      } else if (source.includes("sameGroup")) {
        const name = el.getAttribute("name");
        let node = el.parentElement; value = "";
        for (let depth = 0; depth < 8 && node; depth++, node = node.parentElement) {
          const group = name ? [...node.querySelectorAll(`input[name="${CSS.escape(name)}"]`)] : [];
          if (group.length >= 2) {
            const labels = new Set([...node.querySelectorAll("label")].map((x) => x.innerText.trim().toLowerCase()));
            const lines = (node.innerText || "").split("\n").map((x) => x.replace(/\s+/g, " ").trim()).filter(Boolean);
            value = lines.find((line) => !labels.has(line.toLowerCase()) && line.length > 8) || "";
            if (value) break;
          }
        }
      } else if (source.includes("aria-label") || source.includes("labelNode")) {
        let node = el.parentElement; value = "";
        for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
          const label = node.getAttribute("aria-label")
            || node.querySelector('label,[class*="label"],[class*="question"]')?.textContent?.trim();
          if (label) { value = label; break; }
        }
      } else throw new Error("Unsupported evaluate expression in extension mode");
    } else throw new Error(`Unsupported DOM operation: ${operation}`);
    const pageText = (document.body?.innerText || "").toLowerCase();
    const applicationSignals = [
      "save and close",
      "select a resume",
      "your resume",
      "contact information",
      "continue",
      "submit your application",
      "review your application",
      "employer requirements",
      "apply anyway",
      "add a resume",
      "upload a resume",
    ];
    const applicationScore = applicationSignals.reduce(
      (score, signal) => score + (pageText.includes(signal) ? 10 : 0),
      0,
    ) + (
      window !== window.top
      && /apply|smartapply|indeed/i.test(
        `${location.href} ${document.title || ""} ${window.name || ""}`
      )
        ? 5
        : 0
    );
    return {
      ok: true,
      value,
      frame: {
        isTop: window === window.top,
        href: location.href,
        title: document.title || "",
        name: window.name || "",
        applicationScore,
      }
    };
  } catch (error) {
    return {ok: false, error: error?.message || String(error)};
  }
}

connect();
