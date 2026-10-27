// Official AstrBot bridge only. Failed mutations retain their business ID.
const drafts = new Map();
const versions = new Map();
const pendingWrites = new Map();
const memoryDrafts = Object.freeze({
  getItem: key => drafts.get(String(key)) ?? null,
  setItem: (key, value) => drafts.set(String(key), String(value)),
  removeItem: key => drafts.delete(String(key)),
});

let readyPromise;
function ready() {
  if (!readyPromise) readyPromise = (async () => {
    const bridge = window.AstrBotPluginView;
    if (!bridge?.ready || !bridge.apiGet || !bridge.apiPost || !bridge.download) {
      throw new Error('当前 AstrBot 不支持插件视图，请检查版本。');
    }
    await bridge.ready();
    return bridge;
  })();
  return readyPromise;
}

function endpointFor(path, write) {
  if (typeof path !== 'string' || !path.startsWith('/api/')) throw new Error('未知页面接口。');
  const endpoint = path.slice(5);
  const allowed = write ? /^(collect|generate|profile|feedback|undo)$/ : /^(state|runs\/[A-Za-z0-9_-]{1,100})$/;
  if (!allowed.test(endpoint)) throw new Error('未知页面接口。');
  return endpoint;
}

async function api(path, body) {
  const bridge = await ready();
  const endpoint = endpointFor(path, body !== undefined);
  let signature;
  if (body !== undefined) {
    body = structuredClone(body);
    delete body.request_id;
    delete body.key;
    if (endpoint === 'profile') {
      const version = versions.get(JSON.stringify(body.expected));
      if (!version) throw new Error('设置版本已失效，请刷新后重新保存。');
      delete body.expected;
      body.expected_version = version;
    }
    signature = JSON.stringify([endpoint, body]);
    let entry = pendingWrites.get(signature);
    if (!entry) {
      const key = crypto.randomUUID();
      entry = {...body, [endpoint === 'feedback' || endpoint === 'undo' ? 'key' : 'request_id']: key};
      pendingWrites.set(signature, entry);
    }
    body = entry;
  }
  // No automatic retry, including after timeout or malformed model output.
  const result = body === undefined ? await bridge.apiGet(endpoint) : await bridge.apiPost(endpoint, body);
  if (signature) pendingWrites.delete(signature);
  if (endpoint === 'state') {
    versions.set(JSON.stringify(result.profile), result.profile_version);
    while (versions.size > 20) versions.delete(versions.keys().next().value);
    const banner = document.getElementById('plugin-initialization');
    if (banner) {
      banner.textContent = result.initialization?.message || '';
      banner.hidden = !banner.textContent;
    }
  }
  return result;
}

document.addEventListener('click', async event => {
  const link = event.target.closest?.('#download');
  if (!link || link.hidden) return;
  event.preventDefault();
  const match = /^\/api\/runs\/([A-Za-z0-9_-]{1,100})\/download$/.exec(link.getAttribute('href') || '');
  if (!match) return;
  try {
    await (await ready()).download(`runs/${match[1]}/download`, {}, `briefing-${match[1]}.md`);
  } catch (error) {
    const banner = document.getElementById('connection-error');
    banner.textContent = error.message;
    banner.hidden = false;
  }
});

window.BilibiliShixunTransport = Object.freeze({ready, api, drafts: memoryDrafts});
