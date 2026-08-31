/**
 * HY-MT Universal Core
 * 负责通用逻辑：通信、缓存、UI渲染
 */

class UniversalCore {
    constructor() {
        this.config = {
            enabled: true,
            targetLanguage: 'Chinese',
            fontSize: 20,
            displayMode: 'append'
        };
        this.translationCache = new Map();
        this.activeAdapter = null;

        // 手动整页翻译状态（popup「翻译本页」按钮触发）
        this.pageTranslateOn = false;
        this.pageItems = [];

        console.log('HY-MT: Universal Core Initializing...');
        this.init();
    }

    async init() {
        await this.loadConfig();
        this.listenForConfigUpdates();
        this.injectGlobalStyles();
    }

    async loadConfig() {
        return new Promise((resolve) => {
            chrome.runtime.sendMessage({ action: 'getConfig' }, (response) => {
                if (response && response.success) {
                    this.config = { ...this.config, ...response.config };
                    console.log('HY-MT: Config Loaded', this.config);

                    if (this.isBlacklisted()) {
                        console.log(`HY-MT: Site [${window.location.hostname}] is blacklisted. Disabling.`);
                        this.config.enabled = false;
                        return;
                    }

                    this.updateGlobalStyles();
                }
                resolve();
            });
        });
    }

    isBlacklisted() {
        if (!this.config.blacklist) return false;
        const hostname = window.location.hostname;
        return this.config.blacklist.some(domain => hostname.includes(domain));
    }

    listenForConfigUpdates() {
        chrome.runtime.onMessage.addListener((request) => {
            if (request.action === 'configUpdated') {
                this.config = { ...this.config, ...request.config };
                console.log('HY-MT: Config Updated', this.config);
                this.updateGlobalStyles();

                // Notify adapter if needed
                if (this.activeAdapter && this.activeAdapter.onConfigUpdate) {
                    this.activeAdapter.onConfigUpdate(this.config);
                }
            }
        });

        // 手动整页翻译：popup「翻译本页」按钮 -> 内容脚本
        chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
            if (request.action === 'togglePageTranslate') {
                this.togglePageTranslate().then(sendResponse);
                return true;
            }
            // 启用翻译开关 / 模型启动成功 -> 当前页自动开始翻译（带转圈提示）
            if (request.action === 'autoTranslatePage') {
                if (request.enabled === false) {
                    this.removePageTranslations();
                    sendResponse({ ok: true, active: false });
                    return false;
                }
                this.startPageTranslate(false).then(sendResponse);
                return true;
            }
            if (request.action === 'getPageTranslateState') {
                sendResponse({ ok: true, active: this.pageTranslateOn });
                return false;
            }
        });
    }

    // --- 手动整页翻译 ---

    /**
     * 翻译 / 恢复当前页面。与站点适配器无关，任何页面都可用：
     * 收集正文区块（p/h1-h4/li/blockquote），逐段翻译后插入译文，
     * 再点一次按钮则移除所有译文恢复原文。
     */
    async togglePageTranslate() {
        if (this.pageTranslateOn) {
            this.removePageTranslations();
            return { ok: true, active: false };
        }
        // 手动按钮 = 强制翻译（不受 enabled 总开关 / 黑名单影响）
        return this.startPageTranslate(true);
    }

    /**
     * 开始整页翻译：每个待翻段落先插入转圈 loading，译文返回后原位替换。
     * @param {boolean} force  手动触发时 true（不走 enabled 总开关检查）；
     *                         开关联动时 false（黑名单页 / 关闭状态不翻）
     */
    async startPageTranslate(force = false) {
        if (!force && !this.config.enabled) {
            console.log('HY-MT: 翻译未启用，跳过自动整页翻译');
            return { ok: true, active: false, count: 0 };
        }

        const nodes = this.collectPageTextNodes();
        let queued = 0;

        for (const node of nodes) {
            // 已有译文 / 已有转圈中的段落跳过
            const next = node.nextSibling;
            if (next && next.classList) {
                if (next.classList.contains('hy-mt-page-item')) continue;
                if (next.classList.contains('hy-mt-page-loading')) continue;
            }

            const text = node.textContent.trim();
            if (text.length < 2) continue;
            if (this.isSameLanguage(text, this.config.targetLanguage)) continue;

            // 在译文将出现的位置先放转圈提示，译文到了再替换
            const loading = document.createElement('div');
            loading.className = 'hy-mt-page-loading';
            loading.innerHTML = '<span class="hy-mt-loading-spinner"></span><span>翻译中…</span>';
            node.parentNode.insertBefore(loading, node.nextSibling);
            queued++;

            chrome.runtime.sendMessage(
                { action: 'translate', text, targetLanguage: this.config.targetLanguage, priority: 'normal' },
                (resp) => {
                    // 无论成败先撤掉转圈（节点可能已被页面刷新移除）
                    if (loading.parentNode) loading.remove();
                    // 翻译被停止（恢复原文）后到达的响应不再插入译文
                    if (!this.pageTranslateOn) return;
                    const tr = resp && resp.success ? resp.translation : null;
                    if (!tr) return;
                    if (typeof PROMPT_ECHO_RE !== 'undefined' && PROMPT_ECHO_RE.test(tr)) return;
                    if (!node.parentNode) return;
                    const div = document.createElement('div');
                    div.className = 'hy-mt-page-item';
                    div.textContent = tr;
                    node.parentNode.insertBefore(div, node.nextSibling);
                    this.pageItems.push(div);
                }
            );
        }

        this.pageTranslateOn = true;
        console.log('HY-MT: 整页翻译已提交 ' + queued + ' 段');
        return { ok: true, active: true, count: queued };
    }

    collectPageTextNodes() {
        // 正文优先取语义容器，没有就退化到 body
        let roots = document.querySelectorAll('article, main, [role="main"]');
        if (!roots.length) roots = [document.body];

        const found = new Set();
        roots.forEach(root => {
            root.querySelectorAll('p, h1, h2, h3, h4, li, blockquote').forEach(n => {
                if (found.has(n)) return;
                const rect = n.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) return;
                if (n.closest('nav, aside, footer, header, script, style, code, pre, form, button, figcaption, [class*="menu"], [class*="nav"], [class*="sidebar"], [class*="comment"], [class*="promo"]')) return;
                found.add(n);
            });
        });
        return Array.from(found);
    }

    removePageTranslations() {
        // 同时清理 DOM 里游离的译文（SPA 局部刷新可能留下引用丢失的节点）
        document.querySelectorAll('.hy-mt-page-item').forEach(el => el.remove());
        // 清掉转圈占位；进行中的翻译回调到达后由 pageTranslateOn 守卫阻止插入
        document.querySelectorAll('.hy-mt-page-loading').forEach(el => el.remove());
        this.pageItems = [];
        this.pageTranslateOn = false;
        console.log('HY-MT: 已恢复原文');
    }

    // --- Translation API ---

    /**
     * @param {string} text     待翻译文本
     * @param {object} options  { priority: 'high'|'normal', context: 上一句 }
     */
    async translate(text, options = {}) {
        if (!text || !this.config.enabled) return null;

        const cleanText = text.trim();

        // 源文本已是目标语言时不浪费一次推理
        if (this.isSameLanguage(cleanText, this.config.targetLanguage)) {
            return null;
        }

        const context = options.context || '';
        const cacheKey = context ? `${cleanText}||${context}` : cleanText;

        if (this.translationCache.has(cacheKey)) {
            return this.translationCache.get(cacheKey);
        }

        try {
            const response = await chrome.runtime.sendMessage({
                action: 'translate',
                text: cleanText,
                targetLanguage: this.config.targetLanguage,
                context: context,
                priority: options.priority || 'normal'
            });

            if (response && response.success) {
                this.translationCache.set(cacheKey, response.translation);
                if (this.translationCache.size > 2000) {
                    const first = this.translationCache.keys().next().value;
                    this.translationCache.delete(first);
                }
                return response.translation;
            }
        } catch (e) {
            console.error('HY-MT: Translation Failed', e);
        }
        return null;
    }

    isSameLanguage(text, targetLang) {
        if (!text || !targetLang) return false;

        const lang = targetLang.toLowerCase();
        const isTargetChinese = lang.includes('zh') || lang.includes('chinese');
        const isTargetEnglish = lang.includes('en') || lang.includes('english');

        // Remove spaces and punctuation for density calculation
        const stripped = text.replace(/[\s\p{P}]/gu, '');
        if (stripped.length === 0) return false;

        if (isTargetChinese) {
            // Check for Chinese characters
            const matches = stripped.match(/[\u4e00-\u9fa5]/g);
            const count = matches ? matches.length : 0;
            return (count / stripped.length) > 0.3;
        }

        if (isTargetEnglish) {
            const matches = stripped.match(/[a-zA-Z]/g);
            const count = matches ? matches.length : 0;
            return (count / stripped.length) > 0.5;
        }

        return false;
    }

    // --- UI Helpers ---

    injectGlobalStyles() {
        const style = document.createElement('style');
        style.id = 'hy-mt-universal-styles';
        style.textContent = `
            /* Universal Wrapper */
            .hy-mt-wrapper {
                font-family: system-ui, -apple-system, sans-serif;
                pointer-events: none; /* Let clicks pass through by default */
            }
            
            /* Type 1: Video Overlay (Floating Bottom) */
            /* Type 1: Video Overlay (Floating Bottom) */
            .hy-mt-video-overlay {
                position: absolute;
                /* Bottom controlled by Adapter */
                left: 50%;
                transform: translateX(-50%);
                z-index: 9999;
                text-align: center;
                pointer-events: none;
                /* 固定宽度：像原生字幕一样文字在框内换行，
                   避免每句话长度不同导致框体不断伸缩跳动 */
                width: 80%;
                max-width: 80%;
                transition: bottom 0.1s ease-out; /* Smooth */
            }

            .hy-mt-video-text {
                background: var(--hy-mt-bg, rgba(0, 0, 0, 0.8));
                color: var(--hy-mt-color, #fff);
                font-size: var(--hy-mt-size, 16px);
                padding: 6px 12px;
                border-radius: 6px;
                display: inline-block;
                text-shadow: 0 1px 2px rgba(0,0,0,0.8);
                white-space: pre-wrap;
                max-width: 100%;
            }

            /* 双语模式：原文在译文之上，弱化处理避免抢视线 */
            .hy-mt-video-original {
                display: block;
                margin-bottom: 4px;
                font-size: calc(var(--hy-mt-size, 16px) * 0.62);
                opacity: 0.62;
                background: transparent;
                padding: 0 12px;
                line-height: 1.35;
            }

            /* Type 2: Text Feed (Inline Block) */
            .hy-mt-text-feed-item {
                display: block;
                margin-top: 6px;
                padding: 4px 0;
                color: var(--hy-mt-text-color-feed, #0f1419);
                /* Text feeds use 50% of the configured size for better readability */
                font-size: calc(var(--hy-mt-size, 16px) * 0.5);
                line-height: 1.6;
                font-weight: 400;
                pointer-events: auto;
            }

            /* 手动整页翻译：插入的译文段落（淡蓝色区分原文） */
            .hy-mt-page-item {
                display: block;
                margin-top: 4px;
                padding: 2px 0;
                color: var(--hy-mt-page-color, #2563eb);
                font-size: 1em;
                line-height: 1.6;
                pointer-events: auto;
            }

            /* 整页翻译：译文到达前的转圈占位（译文出来后原位替换） */
            .hy-mt-page-loading {
                display: flex;
                align-items: center;
                gap: 6px;
                margin-top: 4px;
                padding: 2px 0;
                font-size: 0.85em;
                line-height: 1.6;
                color: var(--hy-mt-page-color, #2563eb);
                opacity: 0.65;
                pointer-events: none;
            }
            .hy-mt-page-loading .hy-mt-loading-spinner {
                width: 12px;
                height: 12px;
                margin-left: 0;
                vertical-align: middle;
                border-top-color: var(--hy-mt-page-color, #2563eb);
            }

            /* Loading Spinner */
            .hy-mt-loading-spinner {
                display: inline-block;
                width: 14px;
                height: 14px;
                margin-left: 8px;
                vertical-align: sub; /* Align with text baseline */
                border: 2px solid rgba(0,0,0,0.1);
                border-radius: 50%;
                border-top-color: var(--hy-mt-text-color-feed, #0f1419);
                animation: hy-mt-spin 0.8s linear infinite;
            }
            
            @keyframes hy-mt-spin {
                to { transform: rotate(360deg); }
            }

            /* Retry Button */
            .hy-mt-retry-btn {
                display: inline-flex;
                align-items: center;
                justify-content: center;
                width: 24px;
                height: 24px;
                margin-left: 8px;
                cursor: pointer;
                opacity: 0.7;
                border-radius: 50%;
                transition: all 0.2s;
                vertical-align: middle;
            }
            .hy-mt-retry-btn:hover {
                opacity: 1;
                background-color: rgba(0,0,0,0.05);
            }
            .hy-mt-retry-icon {
                width: 16px;
                height: 16px;
                fill: currentColor;
            }
        `;
        document.head.appendChild(style);
    }

    updateGlobalStyles() {
        const root = document.documentElement;
        root.style.setProperty('--hy-mt-size', `${this.config.fontSize}px`);

        // 译文/字幕配色：未显式设置时各表面用默认（视频字幕深底白字），零回归；
        // 用户在设置视图配置后，字幕底色/文字色 + 整页译文/feed 文字色统一跟随。
        const tStyle = this.config.translationStyle || {};
        const bg = tStyle.bg || this.config.backgroundColor || 'rgba(0,0,0,0.8)';
        const text = tStyle.text || this.config.textColor || '#ffffff';
        root.style.setProperty('--hy-mt-bg', bg);
        root.style.setProperty('--hy-mt-color', text);
        if (this.config.translationStyle) {
            root.style.setProperty('--hy-mt-text-color-feed', text);
            root.style.setProperty('--hy-mt-page-color', text);
        }
        // High Contrast: Twitter Black for Light Mode, Twitter White for Dark Mode
        if (!this.config.translationStyle) {
            root.style.setProperty('--hy-mt-text-color-feed', this.config.theme === 'dark' ? '#e7e9ea' : '#0f1419');
        }
    }

    // --- Adapter Management ---
    registerAdapter(adapter) {
        this.activeAdapter = adapter;
        this.activeAdapter.attach(this); // Pass core to adapter
        console.log(`HY - MT: Adapter[${adapter.name}]Registered`);
    }
}

// Export singleton
window.HY_MT_CORE = new UniversalCore();
