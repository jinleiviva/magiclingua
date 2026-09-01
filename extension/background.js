/**
 * Background Service Worker
 *
 * 职责：
 *   1. 转发翻译请求到本地统一服务（prompt 由服务端构造，客户端只传文本）
 *   2. 内存缓存 + 请求去重 + 并发队列
 *   3. 配置管理（以扩展 popup 为准，不再轮询服务端）
 */

// 内置术语表（只读，popup 勾选哪些词库由 config.activeGlossaries 控制）
importScripts('glossaries.js');

const DEFAULT_CONFIG = {
    enabled: true,
    sourceLanguage: 'auto',
    targetLanguage: 'Chinese',
    serverUrl: 'http://localhost:18770',
    fontSize: 24,
    backgroundColor: 'rgba(8, 8, 8, 0.75)',
    textColor: '#ffffff',
    displayMode: 'append',
    bilingualSubtitle: false,
    streamOutput: false,
    // 注意：黑名单是 hostname.includes() 子串匹配，写 'google.com' 会连
    // news.google.com 一起屏蔽。要挡的是搜索结果页，所以写成 www 前缀。
    blacklist: [
        'www.google.com', 'bing.com', 'baidu.com',
        'github.com', 'gitlab.com', 'stackoverflow.com',
        'localhost', '127.0.0.1'
    ],
    // 术语表：勾选的词库 + 用户自定义（未勾选的不生效）
    activeGlossaries: ['finance', 'tech'],
    customGlossary: {},
    customGlossaryEnabled: true,
    // 段落悬停翻译（popup 设置视图配置，默认 Ctrl+悬停）
    hoverTranslate: true,
    hoverModifier: 'ctrl',
    // 译文/字幕配色：null = 未设置（各表面默认色，零回归）；设置后为 {bg, text}
    translationStyle: null
};

// 注入 prompt 的术语总量封顶（与服务端 build_translate_prompt 一致）。
// 合并顺序：自定义 > 内置词库（按定义顺序），超出的部分不注入。
const GLOSSARY_MAX_ENTRIES = 80;

/**
 * 合并当前生效的术语表为 {原文: 固定译法} 平面对象。
 * 只有「勾选的内置词库 + 开启的自定义条目」参与，未勾选的一律不生效。
 */
function getActiveGlossary(config) {
    const merged = {};
    const push = (k, v) => {
        if (!k || !v || merged[k]) return;
        merged[k] = v;
    };

    // 1. 用户自定义优先（兼容旧版 config.glossary 键）
    if (config.customGlossaryEnabled !== false) {
        const custom = config.customGlossary || config.glossary || {};
        for (const [k, v] of Object.entries(custom)) push(k, v);
    }

    // 2. 已勾选的内置词库
    const active = Array.isArray(config.activeGlossaries) ? config.activeGlossaries : [];
    for (const g of globalThis.BUILTIN_GLOSSARIES || []) {
        if (!active.includes(g.id)) continue;
        for (const [src, dst] of g.entries) push(src, dst);
    }

    // 3. 总量封顶（避免 prompt 过长影响小模型翻译质量）
    const keys = Object.keys(merged);
    if (keys.length <= GLOSSARY_MAX_ENTRIES) return merged;
    const capped = {};
    for (const k of keys.slice(0, GLOSSARY_MAX_ENTRIES)) capped[k] = merged[k];
    return capped;
}

// 本地服务管理器的 Native Messaging 宿主名（由 native_host/install.command 注册）
const NATIVE_HOST = 'com.magiclingua.host';

const translationCache = new Map();   // cacheKey -> 译文（内存 L1）
const pendingRequests = new Map();

// ---------------------------------------------------------------------------
// 译文缓存持久化（L2 = chrome.storage.local）
//
// Service Worker 会被 Chrome 随时回收，纯内存缓存在浏览器重启后全部丢失，
// 同一篇文章第二天再翻要全部重算。降到 storage.local 后重启也能命中。
// storage 每条译文一个键（t:<hash>，value 含原始 cacheKey 供回读），
// cacheOrder 存 hash 的写入顺序，用于超限淘汰（与内存同步）。
// ---------------------------------------------------------------------------

const CACHE_MAX_ENTRIES = 2000;
const CACHE_EVICT_BATCH = 400;
const CACHE_PREFIX = 't:';
const CACHE_ORDER_KEY = 'cacheOrder';

const cacheHashIndex = new Map();  // storage hash -> cacheKey（淘汰时反查内存键）
let cacheOrder = [];               // storage hash 数组，按写入先后
let cacheLoaded = null;            // 启动加载 promise（整个 SW 生命周期只跑一次）

function loadPersistentCache() {
    if (cacheLoaded) return cacheLoaded;
    cacheLoaded = new Promise((resolve) => {
        chrome.storage.local.get(CACHE_ORDER_KEY, (data) => {
            const order = (data && data[CACHE_ORDER_KEY]) || [];
            if (!order.length) { resolve(); return; }
            chrome.storage.local.get(order.map(h => CACHE_PREFIX + h), (entries) => {
                for (const h of order) {
                    const entry = entries[CACHE_PREFIX + h];
                    if (entry && entry.k && entry.v) {
                        translationCache.set(entry.k, entry.v);
                        cacheHashIndex.set(h, entry.k);
                        cacheOrder.push(h);
                    }
                }
                resolve();
            });
        });
    });
    return cacheLoaded;
}

function persistTranslation(cacheKey, translation) {
    const isNew = !translationCache.has(cacheKey);
    translationCache.set(cacheKey, translation);
    if (!isNew) return;

    let h = hashString(cacheKey);
    // 32 位 hash 极小概率碰撞，撞了就加盐重散列
    while (cacheHashIndex.has(h) && cacheHashIndex.get(h) !== cacheKey) {
        h = hashString(h + '#');
    }
    cacheHashIndex.set(h, cacheKey);
    cacheOrder.push(h);

    chrome.storage.local.set({ [CACHE_PREFIX + h]: { k: cacheKey, v: translation } });

    if (cacheOrder.length > CACHE_MAX_ENTRIES) {
        const evicted = cacheOrder.splice(0, CACHE_EVICT_BATCH);
        evicted.forEach(x => {
            const k = cacheHashIndex.get(x);
            if (k !== undefined) translationCache.delete(k);
            cacheHashIndex.delete(x);
        });
        chrome.storage.local.remove(evicted.map(x => CACHE_PREFIX + x));
    }
    chrome.storage.local.set({ [CACHE_ORDER_KEY]: cacheOrder });
}

chrome.runtime.onInstalled.addListener(() => {
    chrome.storage.sync.get(DEFAULT_CONFIG, (config) => {
        // 迁移旧版 config.glossary → customGlossary（一次性，保留用户自定义条目）
        if (config.glossary && Object.keys(config.glossary).length && !config.customGlossary) {
            config.customGlossary = config.glossary;
            delete config.glossary;
        }
        chrome.storage.sync.set(config);
    });

    // 右键菜单「翻译选中内容」：划词翻译的入口之一
    chrome.contextMenus.create({
        id: 'hy-mt-translate-selection',
        title: '翻译选中内容',
        contexts: ['selection']
    }, () => void chrome.runtime.lastError); // 重复安装时已存在，忽略报错
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
    if (info.menuItemId !== 'hy-mt-translate-selection' || !tab || tab.id == null) return;
    chrome.tabs.sendMessage(tab.id, { action: 'translateSelection' }).catch(() => {
        // 内容脚本未注入（chrome:// 页等）时忽略
    });
});

// 快捷键（默认 Alt+T，chrome://extensions/shortcuts 可改）
chrome.commands.onCommand.addListener((command) => {
    if (command !== 'translate-selection') return;
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (tabs[0] && tabs[0].id != null) {
            chrome.tabs.sendMessage(tabs[0].id, { action: 'translateSelection' }).catch(() => {});
        }
    });
});

// ---------------------------------------------------------------------------
// 消息处理
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    switch (request.action) {
        case 'translate':
            return handleTranslateRequest(request, sendResponse);
        case 'getConfig':
            chrome.storage.sync.get(DEFAULT_CONFIG, (config) => {
                sendResponse({ success: true, config });
            });
            return true;
        case 'updateConfig':
            chrome.storage.sync.set(request.config, () => {
                sendResponse({ success: true });
                notifyAllTabs({ action: 'configUpdated', config: request.config });
            });
            return true;

        // ---- 本地服务启停（经 Native Messaging，扩展本身不能起进程）----
        case 'serviceStatus':
            serviceStatus()
                .then(result => sendResponse(result))
                .catch(error => sendResponse({ ok: false, error: 'EXCEPTION', message: error.message }));
            return true;
        case 'serviceStart':
            nativeCall({ action: 'start' })
                .then(result => sendResponse(result))
                .catch(error => sendResponse({ ok: false, error: 'EXCEPTION', message: error.message }));
            return true;
        case 'serviceStop':
            nativeCall({ action: 'stop' })
                .then(result => sendResponse(result))
                .catch(error => sendResponse({ ok: false, error: 'EXCEPTION', message: error.message }));
            return true;
    }
});

// ---------------------------------------------------------------------------
// 本地服务管理
// ---------------------------------------------------------------------------

function nativeCall(payload) {
    return new Promise((resolve) => {
        try {
            chrome.runtime.sendNativeMessage(NATIVE_HOST, payload, (response) => {
                const err = chrome.runtime.lastError;
                if (err) {
                    resolve({ ok: false, error: 'NO_HOST', message: err.message });
                    return;
                }
                resolve(response || { ok: false, error: 'EMPTY_REPLY' });
            });
        } catch (e) {
            resolve({ ok: false, error: 'NO_HOST', message: e.message });
        }
    });
}

async function serviceStatus() {
    const result = await nativeCall({ action: 'status' });

    // 服务起来了就顺便拿详细运行信息（空闲倒计时等）
    if (result && result.ok && result.running) {
        try {
            const config = await new Promise(resolve => {
                chrome.storage.sync.get(DEFAULT_CONFIG, resolve);
            });
            const resp = await fetch(`${config.serverUrl}/v1/status`, {
                signal: AbortSignal.timeout(3000)
            });
            result.detail = await resp.json();
        } catch (e) {
            // /v1/status 取不到不影响主状态
        }
    }
    return result;
}

function handleTranslateRequest(request, sendResponse) {
    // 先等持久化缓存加载完成（只等第一次），再做内存命中判断
    loadPersistentCache().then(() => {
        chrome.storage.sync.get(DEFAULT_CONFIG, (config) => {
            // context 和生效词库都参与缓存 key：勾选/词条任一变化，旧译文自动失效
            const contextKey = request.context ? hashString(request.context) : '';
            const activeGlossary = getActiveGlossary(config);
            const glossaryKey = Object.keys(activeGlossary).length
                ? hashString(JSON.stringify(activeGlossary)) : '';
            const cacheKey = `${request.text}|${request.targetLanguage}|${contextKey}|${glossaryKey}`;

            if (translationCache.has(cacheKey)) {
                sendResponse({ success: true, translation: translationCache.get(cacheKey) });
                return;
            }

            // 相同请求正在飞行中时复用，不重复占队列
            if (pendingRequests.has(cacheKey)) {
                pendingRequests.get(cacheKey)
                    .then(translation => sendResponse({ success: true, translation }))
                    .catch(error => sendResponse({ success: false, error: error.message }));
                return;
            }

            const promise = enqueueTranslation(request, cacheKey);
            pendingRequests.set(cacheKey, promise);

            promise
                .then(translation => {
                    pendingRequests.delete(cacheKey);
                    sendResponse({ success: true, translation });
                })
                .catch(error => {
                    pendingRequests.delete(cacheKey);
                    sendResponse({ success: false, error: error.message });
                });
        });
    });

    return true;
}

function hashString(str) {
    let h = 0;
    for (let i = 0; i < str.length; i++) {
        h = ((h << 5) - h) + str.charCodeAt(i);
        h |= 0;
    }
    return h.toString(36);
}

// ---------------------------------------------------------------------------
// 并发队列
// ---------------------------------------------------------------------------

const requestQueue = [];
let activeRequests = 0;
const MAX_CONCURRENT = 3;

function enqueueTranslation(request, cacheKey) {
    return new Promise((resolve, reject) => {
        const item = { request, cacheKey, resolve, reject };

        // 视频字幕（high）插队，网页段落（normal）排队
        if (request.priority === 'high') {
            requestQueue.unshift(item);
        } else {
            requestQueue.push(item);
        }

        processQueue();
    });
}

async function processQueue() {
    if (activeRequests >= MAX_CONCURRENT || requestQueue.length === 0) return;

    activeRequests++;
    const item = requestQueue.shift();

    try {
        const translation = await callTranslateService(item.request, item.cacheKey);
        item.resolve(translation);
    } catch (error) {
        item.reject(error);
    } finally {
        activeRequests--;
        processQueue();
    }
}

async function callTranslateService(request, cacheKey) {
    const config = await new Promise(resolve => {
        chrome.storage.sync.get(DEFAULT_CONFIG, resolve);
    });

    const body = {
        text: request.text,
        target_lang: langNameToCode(request.targetLanguage || config.targetLanguage),
        stream: false
    };

    // YouTube 字幕带上前一句作上下文，能明显减少断句误译
    if (request.context) body.context = request.context;

    // 术语表（勾选词库 + 自定义，合并后服务端构造进 prompt）
    const activeGlossary = getActiveGlossary(config);
    if (Object.keys(activeGlossary).length) {
        body.glossary = activeGlossary;
    }

    const response = await fetch(`${config.serverUrl}/v1/translate`, {
        method: 'POST',
        signal: AbortSignal.timeout(60000),
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    });

    if (!response.ok) {
        throw new Error(`服务返回 ${response.status}`);
    }

    const data = await response.json();

    if (data.error) {
        throw new Error(data.error);
    }

    // 服务端判定源语言与目标语言一致时会跳过翻译
    if (data.skipped) {
        return request.text;
    }

    const translation = (data.translation || '').trim();
    if (!translation) {
        throw new Error('服务返回空译文');
    }

    // 写入内存 + chrome.storage.local（Service Worker 回收/重启后仍能命中）
    persistTranslation(cacheKey, translation);

    return translation;
}

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

const LANG_CODE_MAP = {
    'Chinese': 'zh', 'Traditional Chinese': 'zh-Hant', 'Cantonese': 'yue',
    'English': 'en', 'Japanese': 'ja', 'Korean': 'ko',
    'French': 'fr', 'German': 'de', 'Spanish': 'es', 'Portuguese': 'pt',
    'Italian': 'it', 'Dutch': 'nl', 'Russian': 'ru', 'Ukrainian': 'uk',
    'Polish': 'pl', 'Czech': 'cs', 'Turkish': 'tr', 'Arabic': 'ar',
    'Persian': 'fa', 'Hebrew': 'he', 'Hindi': 'hi', 'Urdu': 'ur',
    'Bengali': 'bn', 'Gujarati': 'gu', 'Marathi': 'mr', 'Tamil': 'ta',
    'Telugu': 'te', 'Thai': 'th', 'Vietnamese': 'vi',
    'Indonesian': 'id', 'Malay': 'ms', 'Filipino': 'tl',
    'Khmer': 'km', 'Burmese': 'my',
    'Tibetan': 'bo', 'Kazakh': 'kk', 'Mongolian': 'mn', 'Uyghur': 'ug'
};

function langNameToCode(name) {
    if (!name) return 'zh';
    if (LANG_CODE_MAP[name]) return LANG_CODE_MAP[name];
    // 已经是大写代码时直接返回
    if (/^[a-z]{2}(-[A-Z]{2})?$/.test(name)) return name;
    const lower = name.toLowerCase();
    for (const [k, v] of Object.entries(LANG_CODE_MAP)) {
        if (k.toLowerCase() === lower) return v;
    }
    return 'zh';
}

function notifyAllTabs(message) {
    // 通知全部标签页：新闻站的适配器也要收到配置更新
    chrome.tabs.query({}, (tabs) => {
        tabs.forEach(tab => {
            chrome.tabs.sendMessage(tab.id, message).catch(() => {
                // 标签页未注入脚本时忽略
            });
        });
    });
}

// ---------------------------------------------------------------------------
// 配置同步
//
// 原先这里每 1 秒轮询服务端 /v1/config，用于把 Electron 桌面端的改动同步过来。
// Electron 桌面端已归档，配置统一由本扩展的 popup 管理，轮询没有存在意义，
// 只会持续消耗 CPU 并把服务日志刷爆。改为监听 storage 变更。
// ---------------------------------------------------------------------------

chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'sync') return;

    const updated = {};
    for (const key of Object.keys(changes)) {
        updated[key] = changes[key].newValue;
    }

    chrome.storage.sync.get(DEFAULT_CONFIG, (config) => {
        notifyAllTabs({ action: 'configUpdated', config });
    });
});
