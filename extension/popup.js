/**
 * Popup UI Logic
 *
 * 配置统一由本面板管理（Electron 桌面端已归档）。
 * 改动写入 chrome.storage.sync 后通知 background，再广播到所有标签页。
 *
 * 本地服务的启停走 background -> Native Messaging 宿主，
 * 扩展自身不能创建进程，这是 Chrome 唯一允许的通路。
 */

const DEFAULT_CONFIG = {
    enabled: true,
    targetLanguage: 'Chinese',
    displayMode: 'append',
    bilingualSubtitle: false,
    pdfBilingual: false,
    fontSize: 24,
    serverUrl: 'http://localhost:18770',
    blacklist: []
};

// DOM 元素（显式声明，不依赖 id 隐式全局）
const enabledToggle = document.getElementById('enabledToggle');
const siteToggle = document.getElementById('siteToggle');
const currentSiteDomain = document.getElementById('currentSiteDomain');
const targetLanguage = document.getElementById('targetLanguage');
const displayMode = document.getElementById('displayMode');
const bilingualToggle = document.getElementById('bilingualToggle');
const pdfBilingualToggle = document.getElementById('pdfBilingualToggle');
const fontSize = document.getElementById('fontSize');
const fontSizeValue = document.getElementById('fontSizeValue');
const serverUrl = document.getElementById('serverUrl');
const openPdfBtn = document.getElementById('openPdfBtn');
const statusText = document.getElementById('statusText');
const statusBox = document.getElementById('status');

const serviceState = document.getElementById('serviceState');
const serviceHint = document.getElementById('serviceHint');
const serviceToggleBtn = document.getElementById('serviceToggleBtn');
const serviceRow = document.getElementById('serviceRow');
const pageTranslateBtn = document.getElementById('pageTranslateBtn');

let initialHostname = null;
let refreshTimer = null;

document.addEventListener('DOMContentLoaded', () => {
    loadConfig();
    attachEventListeners();
    initCurrentSite();
    initPageTranslateBtn();
    refreshService();
    // 面板开着时持续刷新：启动过程中要能看到「加载中 -> 运行中」
    refreshTimer = setInterval(refreshService, 4000);
});

// ---------------------------------------------------------------------------
// 整页翻译按钮（任何网站都可用，与站点适配器无关）
// ---------------------------------------------------------------------------

function initPageTranslateBtn() {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (!tabs[0]) return;
        chrome.tabs.sendMessage(tabs[0].id, { action: 'getPageTranslateState' }, (resp) => {
            if (chrome.runtime.lastError || !resp || !resp.ok) return;
            pageTranslateBtn.textContent = resp.active ? '恢复原文' : '翻译本页';
        });
    });
}

function wirePageTranslateBtn() {
    pageTranslateBtn.addEventListener('click', () => {
        pageTranslateBtn.disabled = true;
        pageTranslateBtn.textContent = '处理中…';

        chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
            if (!tabs[0]) {
                pageTranslateBtn.disabled = false;
                pageTranslateBtn.textContent = '翻译本页';
                return;
            }
            chrome.tabs.sendMessage(tabs[0].id, { action: 'togglePageTranslate' }, (resp) => {
                pageTranslateBtn.disabled = false;
                if (chrome.runtime.lastError || !resp || !resp.ok) {
                    // 内容脚本未注入（chrome:// 页、PDF 页、刚打开未刷新等）
                    pageTranslateBtn.textContent = '此页面不支持，刷新后重试';
                    setTimeout(() => { pageTranslateBtn.textContent = '翻译本页'; }, 2500);
                    return;
                }
                pageTranslateBtn.textContent = resp.active ? '恢复原文' : '翻译本页';
            });
        });
    });
}

window.addEventListener('unload', () => {
    if (refreshTimer) clearInterval(refreshTimer);
});

// ---------------------------------------------------------------------------
// 当前站点
// ---------------------------------------------------------------------------

function initCurrentSite() {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (!tabs || !tabs[0]) return;

        try {
            const url = new URL(tabs[0].url);
            initialHostname = url.hostname;
            currentSiteDomain.textContent = initialHostname;

            chrome.storage.sync.get(DEFAULT_CONFIG, (config) => {
                const isBlacklisted = (config.blacklist || []).some(
                    d => initialHostname.includes(d)
                );
                siteToggle.checked = !isBlacklisted;
            });
        } catch (e) {
            currentSiteDomain.textContent = '无法获取当前域名';
            siteToggle.disabled = true;
        }
    });
}

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------

function loadConfig() {
    chrome.storage.sync.get(DEFAULT_CONFIG, (config) => {
        enabledToggle.checked = config.enabled;
        targetLanguage.value = config.targetLanguage;
        displayMode.value = config.displayMode;
        bilingualToggle.checked = config.bilingualSubtitle;
        pdfBilingualToggle.checked = config.pdfBilingual;
        fontSize.value = config.fontSize;
        fontSizeValue.textContent = `${config.fontSize}px`;
        serverUrl.value = config.serverUrl;
    });
}

function attachEventListeners() {
    wirePageTranslateBtn();

    enabledToggle.addEventListener('change', () => {
        saveConfig({ enabled: enabledToggle.checked });
    });

    siteToggle.addEventListener('change', () => {
        if (!initialHostname) return;

        chrome.storage.sync.get(DEFAULT_CONFIG, (config) => {
            let blacklist = config.blacklist || [];

            if (siteToggle.checked) {
                blacklist = blacklist.filter(d => !initialHostname.includes(d));
            } else if (!blacklist.includes(initialHostname)) {
                blacklist.push(initialHostname);
            }

            saveConfig({ blacklist });
        });
    });

    targetLanguage.addEventListener('change', () => {
        saveConfig({ targetLanguage: targetLanguage.value });
    });

    displayMode.addEventListener('change', () => {
        saveConfig({ displayMode: displayMode.value });
    });

    bilingualToggle.addEventListener('change', () => {
        saveConfig({ bilingualSubtitle: bilingualToggle.checked });
    });

    pdfBilingualToggle.addEventListener('change', () => {
        saveConfig({ pdfBilingual: pdfBilingualToggle.checked });
    });

    fontSize.addEventListener('input', () => {
        fontSizeValue.textContent = `${fontSize.value}px`;
    });

    fontSize.addEventListener('change', () => {
        saveConfig({ fontSize: parseInt(fontSize.value, 10) });
    });

    serverUrl.addEventListener('change', () => {
        saveConfig({ serverUrl: serverUrl.value.trim() });
        refreshService();
    });

    openPdfBtn.addEventListener('click', (e) => {
        e.preventDefault();
        chrome.storage.sync.get(DEFAULT_CONFIG, (config) => {
            chrome.tabs.create({ url: `${config.serverUrl}/pdf` });
        });
    });

    serviceToggleBtn.addEventListener('click', () => {
        const isRunning = serviceToggleBtn.classList.contains('is-danger');
        const action = isRunning ? 'serviceStop' : 'serviceStart';
        const verb = isRunning ? '停止' : '启动';
        controlService(action, verb);
    });
}

function saveConfig(updates) {
    chrome.storage.sync.get(DEFAULT_CONFIG, (config) => {
        const newConfig = { ...config, ...updates };
        chrome.storage.sync.set(newConfig, () => {
            chrome.runtime.sendMessage({
                action: 'updateConfig',
                config: newConfig
            });
        });
    });
}

// ---------------------------------------------------------------------------
// 本地服务：状态与启停
// ---------------------------------------------------------------------------

function sendToBackground(action) {
    return new Promise((resolve) => {
        chrome.runtime.sendMessage({ action }, (response) => {
            resolve(response || { ok: false, error: 'NO_REPLY' });
        });
    });
}

async function refreshService() {
    const result = await sendToBackground('serviceStatus');

    const myId = chrome.runtime.id || '未知';

    if (result.error === 'NO_HOST') {
        setStatus('offline', '未连接');
        const msg = result.message || '(无报错详情)';
        // Chrome 几种拒绝理由，对应完全不同的修法：
        //   forbidden/blocked/access -> 扩展 ID 不在白名单（重装或换文件夹导致 ID 变了）
        //   not found/missing        -> 宿主清单没被读到（清单路径/名称不对）
        //   failed to start/exited   -> 宿主进程启动即崩溃（shebang/权限/依赖）
        const forbidden = /forbidden|blocked|not allowed|access/i.test(msg);
        const notFound = /not found|missing|unable to.*host|could not/i.test(msg);
        const startFail = /failed to start|exited|crash/i.test(msg);
        serviceState.textContent = forbidden ? '扩展 ID 未授权'
            : (notFound ? '未找到服务管理器' : (startFail ? '宿主启动失败' : '本地服务未连接'));
        // 始终把 Chrome 的原始报错打出来，便于精确定位（不再给误导性提示）
        serviceHint.textContent = `ID ${myId}｜报错: ${msg}`;
        serviceToggleBtn.disabled = true;
        serviceRow.title = `原始报错: ${msg}\n扩展 ID: ${myId}`;
        return;
    }

    if (result.error) {
        setStatus('offline', '连接失败');
        serviceState.textContent = '状态未知';
        serviceHint.textContent = `${result.message || result.error}（扩展 ID: ${myId}）`;
        serviceToggleBtn.disabled = true;
        serviceRow.title = `扩展 ID: ${myId}`;
        return;
    }

    const running = result.running;
    serviceToggleBtn.disabled = false;
    serviceRow.title = `扩展 ID: ${myId}`;

    if (result.state === 'loading') {
        setStatus('checking', '模型加载中');
        serviceState.textContent = '加载模型中';
        serviceHint.textContent = '首次请求会稍慢，约 10–20 秒';
        setToggleButton('stopping', '启动中…', false);
        return;
    }

    if (!running) {
        setStatus('offline', '服务已停止');
        serviceState.textContent = '未运行';
        serviceHint.textContent = '不占内存，需要时点启动';
        setToggleButton('start', '启动', false);
        return;
    }

    setStatus('online', '运行正常');
    serviceState.textContent = '运行中';

    const detail = result.detail;
    if (detail && detail.idle_exit_minutes > 0) {
        const left = Math.max(0, Math.round(detail.seconds_to_idle_exit / 60));
        serviceHint.textContent = `空闲 ${detail.idle_exit_minutes} 分钟自动退出（还有 ${left} 分钟）`;
    } else {
        serviceHint.textContent = '常驻模式，不会自动退出';
    }
    setToggleButton('stop', '停止', false);
}

function setToggleButton(state, label, disabled) {
    serviceToggleBtn.textContent = label;
    serviceToggleBtn.disabled = disabled;
    serviceToggleBtn.classList.toggle('is-danger', state === 'stop');
}

async function controlService(action, verb) {
    const original = { label: serviceToggleBtn.textContent, disabled: serviceToggleBtn.disabled };
    serviceToggleBtn.disabled = true;
    serviceToggleBtn.textContent = verb + '中…';

    const result = await sendToBackground(action);

    if (result.error === 'NO_HOST') {
        serviceHint.textContent = '请先运行 native_host/install.command';
    } else if (result.error) {
        serviceHint.textContent = result.message || result.error;
    }

    await refreshService();
    // refreshService 会重设按钮；保留 disabled 防止用户连点
    if (!result.running && result.ok) {
        serviceToggleBtn.disabled = false;
    } else {
        serviceToggleBtn.disabled = original.disabled;
    }
}

function setStatus(kind, label) {
    statusBox.className = `status status-${kind}`;
    statusText.textContent = label;
}
