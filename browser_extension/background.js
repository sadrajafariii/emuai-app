// bud browser extension — background service worker v2
// Connects to bud server via WebSocket so bud can browse in YOUR Chrome

const BUD_URLS = ['http://127.0.0.1:8082', 'https://emuai.org'];
let BUD_WS_URL = 'ws://127.0.0.1:8082/ws/extension';

let ws = null;
let budGroupId = null;  // Chrome tab group ID for bud's tabs
let reconnectTimer = null;

// ── Connect to bud server ──────────────────────────────────────────────────────
function connectToBud() {
  if (ws && ws.readyState === WebSocket.OPEN) return;
  try {
    ws = new WebSocket(BUD_WS_URL);
    ws.onopen = () => {
      console.log('[bud] Extension connected to bud server');
      clearTimeout(reconnectTimer);
      ws.send(JSON.stringify({ type: 'extension_hello', agent: navigator.userAgent }));
    };
    ws.onmessage = async (e) => {
      const msg = JSON.parse(e.data);
      await handleBudCommand(msg);
    };
    ws.onclose = () => {
      console.log('[bud] Disconnected — reconnecting in 3s');
      reconnectTimer = setTimeout(connectToBud, 3000);
    };
    ws.onerror = () => ws.close();
  } catch(e) {
    reconnectTimer = setTimeout(connectToBud, 5000);
  }
}

// ── Handle commands from bud ───────────────────────────────────────────────────
async function handleBudCommand(msg) {
  const { id, type } = msg;

  if (type === 'navigate') {
    const tab = await ensureBudTab(msg.url);
    // Wait for load then capture
    await waitForLoad(tab.id);
    const screenshot = await captureTab(tab.id);
    const text = await getPageText(tab.id);
    reply(id, { type: 'navigate_result', screenshot, text, url: tab.url || msg.url });
  }

  else if (type === 'screenshot') {
    const tab = await getActiveBudTab();
    if (!tab) { reply(id, { type: 'error', error: 'No bud tab open' }); return; }
    const screenshot = await captureTab(tab.id);
    reply(id, { type: 'screenshot_result', screenshot, url: tab.url });
  }

  else if (type === 'click') {
    const tab = await getActiveBudTab();
    if (!tab) return;
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: (x, y) => {
        const el = document.elementFromPoint(x, y);
        if (el) el.click();
      },
      args: [msg.x || 0, msg.y || 0],
    });
    await new Promise(r => setTimeout(r, 600));
    const screenshot = await captureTab(tab.id);
    const text = await getPageText(tab.id);
    reply(id, { type: 'click_result', screenshot, text });
  }

  else if (type === 'type') {
    const tab = await getActiveBudTab();
    if (!tab) return;
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: (text) => {
        const el = document.activeElement || document.querySelector('input,textarea,[contenteditable]');
        if (el) {
          el.focus();
          el.value = text;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
        }
      },
      args: [msg.text || ''],
    });
    reply(id, { type: 'type_result' });
  }

  else if (type === 'close_group') {
    await closeBudGroup();
    reply(id, { type: 'close_result' });
  }
}

function reply(id, data) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ ...data, id }));
  }
}

// ── Tab group management ───────────────────────────────────────────────────────
async function ensureBudTab(url) {
  // Find existing bud group
  if (budGroupId !== null) {
    const groups = await chrome.tabGroups.query({ id: budGroupId }).catch(() => []);
    if (!groups.length) budGroupId = null;
  }

  // Create a new tab
  const tab = await chrome.tabs.create({ url, active: false });

  if (budGroupId === null) {
    // Create a new "bud" group
    budGroupId = await chrome.tabs.group({ tabIds: [tab.id] });
    await chrome.tabGroups.update(budGroupId, {
      title: 'bud',
      color: 'yellow',
      collapsed: false,
    });
  } else {
    // Add to existing bud group
    await chrome.tabs.group({ tabIds: [tab.id], groupId: budGroupId });
  }

  // Make tab visible so user can watch
  await chrome.tabs.update(tab.id, { active: true });
  return tab;
}

async function getActiveBudTab() {
  if (budGroupId === null) return null;
  const tabs = await chrome.tabs.query({ groupId: budGroupId });
  return tabs.length ? tabs[tabs.length - 1] : null;
}

async function closeBudGroup() {
  if (budGroupId === null) return;
  const tabs = await chrome.tabs.query({ groupId: budGroupId });
  const ids = tabs.map(t => t.id);
  if (ids.length) await chrome.tabs.remove(ids);
  budGroupId = null;
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function waitForLoad(tabId, timeout = 8000) {
  return new Promise((resolve) => {
    const start = Date.now();
    const check = () => {
      chrome.tabs.get(tabId, (tab) => {
        if (chrome.runtime.lastError || !tab) { resolve(); return; }
        if (tab.status === 'complete' || Date.now() - start > timeout) { resolve(); return; }
        setTimeout(check, 300);
      });
    };
    setTimeout(check, 500);
  });
}

async function captureTab(tabId) {
  try {
    await chrome.tabs.update(tabId, { active: true });
    await new Promise(r => setTimeout(r, 200));
    const dataUrl = await chrome.tabs.captureVisibleTab({ format: 'jpeg', quality: 80 });
    return dataUrl; // base64 jpeg
  } catch(e) {
    return null;
  }
}

async function getPageText(tabId) {
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const sel = 'p,h1,h2,h3,h4,li,td,th,a,label,button,span';
        return Array.from(document.querySelectorAll(sel))
          .map(e => e.innerText?.trim())
          .filter(t => t && t.length > 2)
          .slice(0, 500)
          .join('\n');
      },
    });
    return results?.[0]?.result?.slice(0, 4000) || '';
  } catch(e) {
    return '';
  }
}

// ── Context menu (existing) ────────────────────────────────────────────────────
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: 'ask-bud', title: 'Ask bud: "%s"', contexts: ['selection'] });
  chrome.contextMenus.create({ id: 'open-bud', title: 'Open bud', contexts: ['page', 'action'] });
  connectToBud();
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === 'open-bud') { chrome.tabs.create({ url: BUD_URLS[0] }); return; }
  if (info.menuItemId === 'ask-bud' && info.selectionText) {
    const text = info.selectionText.trim().slice(0, 2000);
    chrome.tabs.create({ url: `${BUD_URLS[0]}/?ask=${encodeURIComponent(text)}&source=${encodeURIComponent(tab?.url || '')}` });
  }
});

chrome.action.onClicked.addListener(() => {
  chrome.tabs.create({ url: BUD_URLS[0] });
});

// Start connecting on service worker startup
connectToBud();
