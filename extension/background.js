/**
 * Background Service Worker
 *
 * 职责：
 *   1. 转发翻译请求到本地统一服务（prompt 由服务端构造，客户端只传文本）
 *   2. 内存缓存 + 请求去重 + 并发队列
 *   3. 配置管理（以扩展 popup 为准，不再轮询服务端）
 */

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
    blacklist: [
        'google.com', 'bing.com', 'baidu.com',
        'github.com', 'gitlab.com', 'stackoverflow.com',
        'localhost', '127.0.0.1'
    ]
};

// 本地服务管理器的 Native Messaging 宿主名（由 native_host/install.command 注册）
const NATIVE_HOST = 'com.magiclingua.host';

const translationCache = new Map();
const pendingRequests = new Map();

chrome.runtime.onInstalled.addListener(() => {
    chrome.storage.sync.get(DEFAULT_CONFIG, (config) => {
        chrome.storage.sync.set(config);
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
    // context 参与缓存 key，避免不同上下文下同一句译文被错误复用
    const contextKey = request.context ? hashString(request.context) : '';
    const cacheKey = `${request.text}|${request.targetLanguage}|${contextKey}`;

    if (translationCache.has(cacheKey)) {
        sendResponse({ success: true, translation: translationCache.get(cacheKey) });
        return true;
    }

    // 相同请求正在飞行中时复用，不重复占队列
    if (pendingRequests.has(cacheKey)) {
        pendingRequests.get(cacheKey)
            .then(translation => sendResponse({ success: true, translation }))
            .catch(error => sendResponse({ success: false, error: error.message }));
        return true;
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
        const translation = await callTranslateService(item.request);
        item.resolve(translation);
    } catch (error) {
        item.reject(error);
    } finally {
        activeRequests--;
        processQueue();
    }
}

async function callTranslateService(request) {
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

    translationCache.set(
        `${request.text}|${request.targetLanguage}|${request.context ? hashString(request.context) : ''}`,
        translation
    );

    // 缓存上限 2000 条，超出后淘汰最早的一批
    if (translationCache.size > 2000) {
        const keys = Array.from(translationCache.keys()).slice(0, 400);
        keys.forEach(k => translationCache.delete(k));
    }

    return translation;
}

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

const LANG_CODE_MAP = {
    'Chinese': 'zh', 'English': 'en', 'Japanese': 'ja', 'Korean': 'ko',
    'French': 'fr', 'German': 'de', 'Spanish': 'es', 'Russian': 'ru',
    'Portuguese': 'pt', 'Italian': 'it', 'Dutch': 'nl', 'Arabic': 'ar'
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
