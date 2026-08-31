/**
 * Popup UI Logic
 *
 * 配置统一由本面板管理（Electron 桌面端已归档）。
 * 改动写入 chrome.storage.sync 后通知 background，再广播到所有标签页。
 *
 * 信息架构（双视图，CSS display 切换，零路由）：
 *   主视图   = 高频动作：站点开关 / 翻译本页 / 导出字幕 / 目标语言+显示模式 / 文档入口
 *   设置视图 = 低频配置：翻译行为（悬停翻译）/ 译文样式 / 术语表（含 CSV）/ 本地服务
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
    blacklist: [],
    activeGlossaries: ['finance', 'tech'],
    customGlossary: {},
    customGlossaryEnabled: true,
    // 段落悬停翻译（默认 Ctrl+悬停）
    hoverTranslate: true,
    hoverModifier: 'ctrl',
    // 译文/字幕配色：null = 未设置（各表面保持默认色，零回归）；
    // 设置后为 {bg, text}，字幕底色/文字色 + 整页译文与 feed 文字色统一跟随
    translationStyle: null
};

// 译文配色预设（与沉浸式等工具对齐的 4 组常用组合）
const STYLE_PRESETS = [
    { name: '深底白字', bg: 'rgba(0,0,0,0.8)', text: '#ffffff' },
    { name: '浅底深字', bg: 'rgba(244,246,250,0.96)', text: '#1c1c1e' },
    { name: '深底黄字', bg: 'rgba(28,28,30,0.92)', text: '#ffd02f' },
    { name: '半透明黑底白字', bg: 'rgba(8,8,8,0.75)', text: '#ffffff' }
];

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
const openDocBtn = document.getElementById('openDocBtn');
const statusText = document.getElementById('statusText');
const statusBox = document.getElementById('status');

// 双视图
const viewMain = document.getElementById('viewMain');
const viewSettings = document.getElementById('viewSettings');
const settingsBtn = document.getElementById('settingsBtn');
const backBtn = document.getElementById('backBtn');
const settingsTitle = document.getElementById('settingsTitle');

// 悬停翻译 + 样式
const hoverToggle = document.getElementById('hoverToggle');
const hoverModifier = document.getElementById('hoverModifier');
const stylePresets = document.getElementById('stylePresets');
const styleBgColor = document.getElementById('styleBgColor');
const styleTextColor = document.getElementById('styleTextColor');

// 字幕导出
const exportSubtitleBtn = document.getElementById('exportSubtitleBtn');

// 服务
const serviceState = document.getElementById('serviceState');
const serviceHint = document.getElementById('serviceHint');
const serviceToggleBtn = document.getElementById('serviceToggleBtn');
const serviceRow = document.getElementById('serviceRow');
const pageTranslateBtn = document.getElementById('pageTranslateBtn');

// 术语表
const glossCard = document.getElementById('glossCard');
const builtinGlossList = document.getElementById('builtinGlossList');
const customGlossRow = document.getElementById('customGlossRow');
const customGlossSwitch = document.getElementById('customGlossSwitch');
const customGlossToggle = document.getElementById('customGlossToggle');
const customGlossCount = document.getElementById('customGlossCount');
const customGlossaryInput = document.getElementById('customGlossaryInput');
const importCsvBtn = document.getElementById('importCsvBtn');
const exportCsvBtn = document.getElementById('exportCsvBtn');
const importCsvFile = document.getElementById('importCsvFile');

const GLOSSARY_MAX_ENTRIES = 80; // 与服务端 build_translate_prompt 的上限一致

let initialHostname = null;
let refreshTimer = null;

document.addEventListener('DOMContentLoaded', () => {
    loadConfig();
    attachEventListeners();
    initCurrentSite();
    initPageTranslateBtn();
    initViewSwitch();
    initExportSubtitle();
    refreshService();
    // 面板开着时持续刷新：启动过程中要能看到「加载中 -> 运行中」
    refreshTimer = setInterval(refreshService, 4000);
});

// ---------------------------------------------------------------------------
// 视图切换：主视图 <-> 设置视图（CSS display 切换，零路由）
// ---------------------------------------------------------------------------

function initViewSwitch() {
    settingsBtn.addEventListener('click', () => {
        viewMain.hidden = true;
        viewSettings.hidden = false;
        backBtn.classList.remove('hidden');
        settingsTitle.classList.remove('hidden');
        settingsBtn.classList.add('active');
    });
    backBtn.addEventListener('click', () => {
        viewSettings.hidden = true;
        viewMain.hidden = false;
        backBtn.classList.add('hidden');
        settingsTitle.classList.add('hidden');
        settingsBtn.classList.remove('active');
    });
}

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
        // 迁移旧版 config.glossary → customGlossary（自定义条目保留，继续生效）
        if (config.glossary && Object.keys(config.glossary).length && !config.customGlossary) {
            config.customGlossary = config.glossary;
            delete config.glossary;
            saveConfig({ customGlossary: config.customGlossary });
        }

        enabledToggle.checked = config.enabled;
        targetLanguage.value = config.targetLanguage;
        // 配置里存着已不支持的语言（历史遗留）时回退到中文，避免 select 落到空值
        if (targetLanguage.selectedIndex === -1) targetLanguage.value = 'Chinese';
        displayMode.value = config.displayMode;
        bilingualToggle.checked = config.bilingualSubtitle;
        pdfBilingualToggle.checked = config.pdfBilingual;
        fontSize.value = config.fontSize;
        fontSizeValue.textContent = `${config.fontSize}px`;
        serverUrl.value = config.serverUrl;

        // 悬停翻译 + 译文样式
        hoverToggle.checked = config.hoverTranslate !== false;
        hoverModifier.value = config.hoverModifier || 'ctrl';
        const tStyle = config.translationStyle || {};
        styleBgColor.value = rgbaToHex(tStyle.bg || 'rgba(8,8,8,0.75)');
        styleTextColor.value = rgbaToHex(tStyle.text || '#ffffff');
        renderStylePresets(config.translationStyle);

        renderBuiltinGlossaries(config.activeGlossaries);
        customGlossToggle.checked = config.customGlossaryEnabled !== false;
        customGlossaryInput.value = glossaryToText(config.customGlossary);
        updateCustomGlossCount(config);
    });
}

// ---------------------------------------------------------------------------
// 译文样式：4 组预设色块 + 自定义颜色
// ---------------------------------------------------------------------------

function renderStylePresets(current) {
    stylePresets.innerHTML = '';
    const cur = current || {};

    STYLE_PRESETS.forEach((p, idx) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'preset-swatch';
        btn.dataset.idx = String(idx);
        btn.title = p.name;
        btn.style.background = p.bg;
        btn.style.color = p.text;
        if (cur.bg === p.bg && cur.text === p.text) btn.classList.add('selected');
        stylePresets.appendChild(btn);
    });
}

function rgbaToHex(rgba) {
    const m = String(rgba).match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/);
    if (m) {
        const to2 = x => parseInt(x, 10).toString(16).padStart(2, '0');
        return '#' + to2(m[1]) + to2(m[2]) + to2(m[3]);
    }
    if (String(rgba).startsWith('#')) return rgba;
    return '#000000';
}

// ---------------------------------------------------------------------------
// 术语表：内置词库勾选生效 + 自定义条目 + CSV 导入导出
// config.activeGlossaries  = 勾选的内置词库 id 数组（未勾选的不生效）
// config.customGlossary    = 用户自定义 {原文: 固定译法}
// config.customGlossaryEnabled = 自定义条目总开关
// ---------------------------------------------------------------------------

function renderBuiltinGlossaries(activeIds) {
    const active = Array.isArray(activeIds) ? activeIds : [];
    builtinGlossList.innerHTML = '';

    for (const g of globalThis.BUILTIN_GLOSSARIES || []) {
        const row = document.createElement('label');
        row.className = 'gloss-row';

        const sw = document.createElement('span');
        sw.className = 'switch sm';
        sw.innerHTML = '<input type="checkbox" data-gloss-id=""><span class="track"><span class="knob"></span></span>';
        sw.querySelector('input').dataset.glossId = g.id;
        sw.querySelector('input').checked = active.includes(g.id);

        const name = document.createElement('span');
        name.className = 'gloss-name';
        name.textContent = g.name;

        const count = document.createElement('span');
        count.className = 'gloss-count';
        count.textContent = `${g.entries.length} 条`;

        row.append(sw, name, count);

        builtinGlossList.append(row);
    }
}

function glossaryToText(glossary) {
    if (!glossary) return '';
    return Object.entries(glossary).map(([k, v]) => `${k}=${v}`).join('\n');
}

function textToGlossary(text) {
    const glossary = {};
    for (const rawLine of (text || '').split('\n')) {
        const line = rawLine.trim();
        if (!line) continue;
        const eq = line.indexOf('=');
        if (eq <= 0) continue; // 没有「=」或键为空的行直接忽略
        const key = line.slice(0, eq).trim();
        const value = line.slice(eq + 1).trim();
        if (!key || !value) continue;
        glossary[key] = value;
        if (Object.keys(glossary).length >= GLOSSARY_MAX_ENTRIES) break;
    }
    return glossary;
}

// ---- CSV（与沉浸式翻译术语库同格式：原文,译法，两列）----

function csvEscape(value) {
    const str = String(value);
    return /[",\r\n]/.test(str) ? '"' + str.replace(/"/g, '""') + '"' : str;
}

function glossaryToCsv(glossary) {
    return Object.entries(glossary)
        .map(([k, v]) => `${csvEscape(k)},${csvEscape(v)}`)
        .join('\r\n');
}

function parseCsvLine(line) {
    const out = [];
    let cur = '', inQ = false;
    for (let i = 0; i < line.length; i++) {
        const c = line[i];
        if (inQ) {
            if (c === '"') {
                if (line[i + 1] === '"') { cur += '"'; i++; }
                else inQ = false;
            } else {
                cur += c;
            }
        } else if (c === '"') {
            inQ = true;
        } else if (c === ',') {
            out.push(cur); cur = '';
        } else {
            cur += c;
        }
    }
    out.push(cur);
    return out;
}

function csvToGlossary(text) {
    const glossary = {};
    const body = String(text || '').replace(/^\uFEFF/, '');
    for (const rawLine of body.split(/\r?\n/)) {
        const line = rawLine.trim();
        if (!line) continue;
        const cols = parseCsvLine(line);
        const key = (cols[0] || '').trim();
        const value = (cols[1] || '').trim();
        if (!key || !value) continue;
        glossary[key] = value;
        if (Object.keys(glossary).length >= GLOSSARY_MAX_ENTRIES) break;
    }
    return glossary;
}

/** 触发浏览器下载（无需 downloads 权限）。bom=true 用于 CSV（Excel 中文不乱码） */
function downloadText(filename, text, mime, bom) {
    const blob = new Blob([bom ? '\uFEFF' : '', text], { type: `${mime};charset=utf-8` });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** 自定义条目行的条数角标（内置词库行内已各自显示条数，此处只更新自定义） */
function updateCustomGlossCount(config) {
    const customN = config.customGlossary ? Object.keys(config.customGlossary).length : 0;
    customGlossCount.textContent = customN ? `${customN} 条` : '';
}

// ---------------------------------------------------------------------------
// 字幕导出（视频站场景，VideoAdapter 提供数据）
// ---------------------------------------------------------------------------

function initExportSubtitle() {
    // 打开面板时探测当前页是否有可导出的字幕轨道
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (!tabs[0]) return;
        chrome.tabs.sendMessage(tabs[0].id, { action: 'exportSubtitles', probe: true }, (resp) => {
            if (chrome.runtime.lastError || !resp || !resp.ok || !resp.available) return;
            exportSubtitleBtn.classList.remove('hidden');
        });
    });

    exportSubtitleBtn.addEventListener('click', () => {
        chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
            if (!tabs[0]) return;
            chrome.tabs.sendMessage(tabs[0].id, { action: 'exportSubtitles' }, (resp) => {
                if (chrome.runtime.lastError || !resp || !resp.ok || !resp.srtText) {
                    flashSubtitleBtn('暂无可用字幕');
                    return;
                }
                downloadText(`${resp.name || 'subtitles'}.srt`, resp.srtText, 'text/plain', false);
                flashSubtitleBtn('已导出 ✓');
            });
        });
    });
}

function flashSubtitleBtn(label) {
    // 只改文案 span，不动 SVG（避免 textContent 整片覆盖把图标节点冲掉）
    const lab = exportSubtitleBtn.querySelector('.sub-label');
    if (!lab) return;
    const original = lab.textContent;
    lab.textContent = label;
    setTimeout(() => { lab.textContent = original; }, 2000);
}

function attachEventListeners() {
    wirePageTranslateBtn();

    enabledToggle.addEventListener('change', () => {
        const enabled = enabledToggle.checked;
        saveConfig({ enabled });
        // 开启→当前页自动开始翻译；关闭→移除当前页译文恢复原文（无需手动刷新）
        notifyActiveTab({ action: 'autoTranslatePage', enabled });
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

    // 悬停翻译：总开关 + 触发修饰键
    hoverToggle.addEventListener('change', () => {
        saveConfig({ hoverTranslate: hoverToggle.checked });
    });

    hoverModifier.addEventListener('change', () => {
        saveConfig({ hoverModifier: hoverModifier.value });
    });

    // 译文样式：预设点选 / 自定义颜色
    stylePresets.addEventListener('click', (e) => {
        const btn = e.target.closest('.preset-swatch');
        if (!btn) return;
        const preset = STYLE_PRESETS[parseInt(btn.dataset.idx, 10)];
        if (!preset) return;
        const tStyle = { bg: preset.bg, text: preset.text };
        saveConfig({ translationStyle: tStyle });
        renderStylePresets(tStyle);
        styleBgColor.value = rgbaToHex(preset.bg);
        styleTextColor.value = rgbaToHex(preset.text);
    });

    const saveStyleColors = () => {
        const tStyle = { bg: styleBgColor.value, text: styleTextColor.value };
        saveConfig({ translationStyle: tStyle });
        renderStylePresets(tStyle);
    };
    styleBgColor.addEventListener('change', saveStyleColors);
    styleTextColor.addEventListener('change', saveStyleColors);

    // 术语表：勾选/取消内置词库（未勾选的不生效）
    builtinGlossList.addEventListener('change', (e) => {
        const input = e.target;
        if (!input.dataset || !input.dataset.glossId) return;
        const gid = input.dataset.glossId;

        chrome.storage.sync.get(DEFAULT_CONFIG, (config) => {
            let active = Array.isArray(config.activeGlossaries) ? config.activeGlossaries : [];
            if (input.checked) {
                if (!active.includes(gid)) active.push(gid);
            } else {
                active = active.filter(x => x !== gid);
            }
            const newConfig = { ...config, activeGlossaries: active };
            saveConfig({ activeGlossaries: active });
            updateCustomGlossCount(newConfig);
        });
    });

    // 术语表：自定义条目总开关
    customGlossToggle.addEventListener('change', () => {
        chrome.storage.sync.get(DEFAULT_CONFIG, (config) => {
            const newConfig = { ...config, customGlossaryEnabled: customGlossToggle.checked };
            saveConfig({ customGlossaryEnabled: customGlossToggle.checked });
            updateCustomGlossCount(newConfig);
        });
    });

    // 术语表：点击「自定义条目」行展开/收起输入框（平时收起，按下才编辑）
    customGlossRow.addEventListener('click', () => {
        customGlossaryInput.hidden = !customGlossaryInput.hidden;
        if (!customGlossaryInput.hidden) customGlossaryInput.focus();
    });

    // 术语表：开关点击（div 不再 label，点 .track/.knob 不会触发 checkbox，需要手动处理）
    customGlossSwitch.addEventListener('click', (e) => {
        if (e.target === customGlossToggle) return; // 点 input 本身，由 input 自然 toggle
        customGlossToggle.checked = !customGlossToggle.checked;
        customGlossToggle.dispatchEvent(new Event('change'));
    });

    // 术语表：自定义条目失焦/收起时解析保存；第一次写入内容时自动开启
    customGlossaryInput.addEventListener('change', () => {
        const customGlossary = textToGlossary(customGlossaryInput.value);
        customGlossaryInput.value = glossaryToText(customGlossary);

        chrome.storage.sync.get(DEFAULT_CONFIG, (config) => {
            const updates = { customGlossary };
            if (!customGlossToggle.checked && Object.keys(customGlossary).length) {
                customGlossToggle.checked = true;
                updates.customGlossaryEnabled = true;
            }
            const newConfig = { ...config, ...updates };
            saveConfig(updates);
            updateCustomGlossCount(newConfig);
        });
    });

    // 术语表：CSV 导出（自定义 + 勾选词库合并，UTF-8 BOM 防 Excel 乱码）
    exportCsvBtn.addEventListener('click', () => {
        chrome.storage.sync.get(DEFAULT_CONFIG, (config) => {
            const merged = {};
            if (config.customGlossaryEnabled !== false) {
                Object.assign(merged, config.customGlossary);
            }
            const active = Array.isArray(config.activeGlossaries) ? config.activeGlossaries : [];
            for (const g of globalThis.BUILTIN_GLOSSARIES || []) {
                if (!active.includes(g.id)) continue;
                for (const [k, v] of g.entries) {
                    if (!(k in merged)) merged[k] = v;
                }
            }
            if (!Object.keys(merged).length) {
                flashCsvBtn(exportCsvBtn, '暂无内容');
                return;
            }
            downloadText('magiclingua-glossary.csv', glossaryToCsv(merged), 'text/csv', true);
        });
    });

    // 术语表：CSV 导入（合并进自定义条目，冲突以文件为准，自动开启）
    importCsvBtn.addEventListener('click', () => importCsvFile.click());
    importCsvFile.addEventListener('change', () => {
        const file = importCsvFile.files[0];
        importCsvFile.value = '';
        if (!file) return;

        const reader = new FileReader();
        reader.onload = () => {
            const imported = csvToGlossary(String(reader.result || ''));
            chrome.storage.sync.get(DEFAULT_CONFIG, (config) => {
                const merged = { ...(config.customGlossary || {}), ...imported };
                const updates = { customGlossary: merged };
                if (!customGlossToggle.checked && Object.keys(merged).length) {
                    customGlossToggle.checked = true;
                    updates.customGlossaryEnabled = true;
                }
                const newConfig = { ...config, ...updates };
                saveConfig(updates);
                customGlossaryInput.value = glossaryToText(merged);
                updateCustomGlossCount(newConfig);
                flashCsvBtn(importCsvBtn, `导入 ${Object.keys(imported).length} 条 ✓`);
            });
        };
        reader.readAsText(file, 'utf-8');
    });

    // 文档翻译入口（服务端 /pdf 页已扩展为 PDF/EPUB/TXT/字幕 通用文档页）
    openDocBtn.addEventListener('click', (e) => {
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

function flashCsvBtn(btn, label) {
    const original = btn.textContent;
    btn.textContent = label;
    setTimeout(() => { btn.textContent = original; }, 2000);
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

/** 向当前激活标签页发消息（内容脚本未注入时静默忽略） */
function notifyActiveTab(message) {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
        if (!tabs[0] || tabs[0].id == null) return;
        chrome.tabs.sendMessage(tabs[0].id, message).catch(() => {});
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
        serviceRow.classList.add('svc-off');
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
        serviceRow.classList.add('svc-off');
        serviceState.textContent = '状态未知';
        serviceHint.textContent = `${result.message || result.error}（扩展 ID: ${myId}）`;
        serviceToggleBtn.disabled = true;
        serviceRow.title = `扩展 ID: ${myId}`;
        return;
    }

    const running = result.running;
    serviceToggleBtn.disabled = false;
    serviceRow.title = `扩展 ID: ${myId}`;
    serviceRow.classList.remove('svc-off', 'svc-loading');

    if (result.state === 'loading') {
        setStatus('checking', '模型加载中');
        serviceRow.classList.add('svc-loading');
        serviceState.textContent = '加载模型中';
        serviceHint.textContent = '首次请求会稍慢，约 10–20 秒';
        setToggleButton('stopping', '启动中…', false);
        return;
    }

    if (!running) {
        setStatus('offline', '服务已停止');
        serviceRow.classList.add('svc-off');
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

    // 模型启动成功 → 当前页自动开始翻译（译文位置会先出现转圈提示）
    if (action === 'serviceStart' && result.ok && result.running) {
        notifyActiveTab({ action: 'autoTranslatePage', enabled: true });
    }
}

function setStatus(kind, label) {
    statusBox.className = `status status-${kind}`;
    statusText.textContent = label;
}
