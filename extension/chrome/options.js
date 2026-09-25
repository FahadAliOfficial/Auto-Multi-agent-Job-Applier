const urlInput = document.querySelector("#url");
const tokenInput = document.querySelector("#token");
const companySites = document.querySelector("#companySites");
const screenshots = document.querySelector("#screenshots");
const status = document.querySelector("#status");

async function load() {
  const cfg = await chrome.storage.local.get([
    "bridgeUrl",
    "token",
    "companySitesEnabled",
    "screenshotsEnabled",
  ]);
  urlInput.value = cfg.bridgeUrl || "ws://127.0.0.1:8765";
  tokenInput.value = cfg.token || "";
  const broadAccess = await chrome.permissions.contains({origins: ["<all_urls>"]});
  companySites.checked = broadAccess && Boolean(cfg.companySitesEnabled);
  screenshots.checked = broadAccess && Boolean(cfg.screenshotsEnabled);
  status.className = "ok";
  status.textContent = `Loaded extension v${chrome.runtime.getManifest().version}.`;
}

document.querySelector("#save").addEventListener("click", async () => {
  const bridgeUrl = urlInput.value.trim();
  const token = tokenInput.value.trim();
  if (!/^ws:\/\/(127\.0\.0\.1|localhost|\[::1\]):\d+$/.test(bridgeUrl)) {
    status.className = "error";
    status.textContent = "Use a loopback WebSocket URL such as ws://127.0.0.1:8765.";
    return;
  }
  if (token.length < 32) {
    status.className = "error";
    status.textContent = "The pairing token is missing or too short.";
    return;
  }
  // Chrome requires permissions.request() to be reached directly from the
  // user's click. Request every checked optional permission in one prompt
  // before doing any awaited storage or messaging work.
  const requested = {};
  if (companySites.checked || screenshots.checked) requested.origins = ["<all_urls>"];
  if (requested.origins) {
    try {
      if (!await chrome.permissions.request(requested)) {
        status.className = "error";
        status.textContent = "Chrome did not grant the selected optional permissions.";
        return;
      }
    } catch (error) {
      status.className = "error";
      status.textContent =
        `Permission setup failed. Reload extension v${chrome.runtime.getManifest().version} `
        + `in chrome://extensions, `
        + `then reopen Options. ${error?.message || error}`;
      return;
    }
  }

  const origins = {origins: ["<all_urls>"]};
  if (
    !companySites.checked
    && !screenshots.checked
    && await chrome.permissions.contains(origins)
  ) {
    await chrome.permissions.remove(origins);
  }
  await chrome.storage.local.set({
    bridgeUrl,
    token,
    companySitesEnabled: companySites.checked,
    screenshotsEnabled: screenshots.checked,
  });
  try {
    await chrome.runtime.sendMessage({type: "reconnect"});
  } catch (_) {
    // The service worker may be restarting; its alarm will reconnect shortly.
  }
  status.className = "ok";
  status.textContent = "Saved. The extension is connecting to the local bot.";
});

load();
