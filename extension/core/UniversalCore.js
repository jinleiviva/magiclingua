/**
 * HY-MT Universal Core
 * 负责通用逻辑：通信、缓存、UI渲染
 */

// ---------------------------------------------------------------------------
// 正文节点收集：排除策略分两层
//
// 强信号 = 语义标签（nav / aside / footer / header / form ...）
//   站点自己划定的非正文区域，信号明确，可以沿整条祖先链匹配，误杀率极低。
//
// 弱信号 = 布局类 class 子串（sidebar / menu / nav / comment ...）
//   只准就近匹配。整页主体容器的类名经常是 page__body--with-sidebar、
//   site-content--with-nav 这种，一旦沿全链 closest 匹配，整篇正文会被一次
//   误杀干净（pbs.org 就是这么全灭的）。所以限定在自身 + 近 3 层祖先。
//
// 中信号 = 功能性区块类名（cookie 条 / 付费墙 / 订阅框 / 广告 / 弹窗）
//   这些词不会出现在整页主体容器上，可以安全地沿整条祖先链匹配。
// ---------------------------------------------------------------------------

// 强信号：沿整条祖先链匹配
const EXCLUDE_SELECTOR_STRICT =
    'nav, aside, footer, header, form, button, select, textarea, ' +
    'script, style, code, pre, figcaption, ' +
    '[role="navigation"], [role="complementary"], [aria-hidden="true"], ' +
    // 站点自己标记的「不翻译」区域（Google 的 material icon 用 <i class="…notranslate">，
    // 以及很多站点给图标/品牌字标加 translate="no"），尊重它，翻出来只是噪音
    '[translate="no"], .notranslate, [class*="notranslate"]';

// 中信号：功能性区块，沿整条祖先链匹配
const MID_EXCLUDE_SELECTOR = [
    '[class*="cookie"]', '[class*="paywall"]', '[class*="newsletter"]',
    '[class*="subscribe"]', '[class*="advert"]', '[class*="promo"]',
    '[class*="sponsored"]', '[class*="modal"]', '[class*="popup"]',
    '[class*="overlay"]', '[class*="banner"]', '[class*="gdpr"]'
].join(', ');

// 弱信号：布局类 class 子串，只在近 NEAR_EXCLUDE_DEPTH 层内生效
const NEAR_EXCLUDE_KEYWORDS = [
    'menu', 'nav', 'sidebar', 'comment', 'related', 'recommend', 'social', 'share'
];
const NEAR_EXCLUDE_DEPTH = 3;

// 收集范围：块级正文 + 独立成块的链接（Google News / Hacker News 这类标题流
// 把标题放在 <a> 上，只收 p/h/li 会颗粒无收）。
// 刻意不收 td/th：老式站点（Hacker News 等）用 table 做整页布局，
// 收 td 会把序号、空单元格、导航行全当成正文，脏得没法看。
const BODY_TEXT_SELECTOR =
    'p, h1, h2, h3, h4, h5, h6, li, blockquote, dd, dt, summary, a';

// 链接若已落在块级正文里，交给外层元素整段翻译，避免同一句翻两遍
const BLOCK_ANCESTOR_SELECTOR = 'p, h1, h2, h3, h4, h5, h6, li, blockquote, dd, dt, summary';

// 独立链接的最小长度：低于此值基本是图标/导航词，翻出来只会污染布局
const MIN_ANCHOR_LENGTH = 15;

// 叶子文本兜底收集的最小长度（现代 SPA 用 div/span/time 兜正文，
// 例：Google News 的「AP News / 2 hours ago / By X」）。低于此值基本是
// 序号、标点、留白，没有翻译价值
const MIN_LEAF_LENGTH = 2;

// 叶子兜底收集：只取这些「可能兜正文」的内联/容器标签的纯文本叶子
// （不含元素子节点）。不收 a（已在 BODY_TEXT_SELECTOR 处理）、不收
// table 系（老式 table 布局站点会炸）
const LEAF_TEXT_SELECTOR = 'div, span, time, label, b, strong, em, i, small, cite, figcaption, dd, dt';

// 语义容器里收集到的节点少于此值时，退化为全页扫描
const MIN_BODY_NODES = 3;

// 单页提交上限：本地推理是串行的，超长页面先保证视口附近出译文
const MAX_PAGE_NODES = 400;

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

        const nodes = this.sortByViewport(this.collectPageTextNodes());
        let queued = 0;

        for (const node of nodes) {
            try {
                // 已有译文 / 已有转圈中的段落跳过
                const next = node.nextSibling;
                if (next && next.classList) {
                    if (next.classList.contains('hy-mt-page-item')) continue;
                    if (next.classList.contains('hy-mt-page-loading')) continue;
                }

                const text = this.extractText(node);
                if (text.length < 2) continue;
                // 纯数字/标点（序号、计数、留白）没有翻译价值，翻出来只是噪音
                if (!/[a-zA-Z一-鿿]/.test(text)) continue;
                if (this.isSameLanguage(text, this.config.targetLanguage)) continue;

                // 在译文将出现的位置先放转圈提示，译文到了再替换
                // 注意：用 DOM 方法而不是 innerHTML，因为 Google News 等站点启用了
                // Trusted Types，innerHTML 赋值会抛 TypeError 并直接中断整页翻译。
                const loading = document.createElement('div');
                loading.className = 'hy-mt-page-loading';
                const spinner = document.createElement('span');
                spinner.className = 'hy-mt-loading-spinner';
                const label = document.createElement('span');
                label.textContent = '翻译中…';
                loading.appendChild(spinner);
                loading.appendChild(label);
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
            } catch (e) {
                // 单段失败不中断整页翻译；Trusted Types 等极端环境也至少能翻后面的节点
                console.warn('HY-MT: 单段翻译提交失败', e);
            }
        }

        this.pageTranslateOn = true;
        console.log('HY-MT: 整页翻译已提交 ' + queued + ' 段');
        return { ok: true, active: true, count: queued };
    }

    collectPageTextNodes() {
        // 正文优先取语义容器
        const roots = document.querySelectorAll('article, main, [role="main"]');
        let found = roots.length ? this.collectFromRoots(roots) : [];

        // 语义容器里几乎没收到东西时（标题流站点把内容铺在 c-wiz / 纯 div 里，
        // 压根没有 article/main 包裹），退化到全页扫描，避免「点了没反应」
        if (found.length < MIN_BODY_NODES) {
            const fallback = this.collectFromRoots([document.body]);
            if (fallback.length > found.length) found = fallback;
        }
        return found;
    }

    collectFromRoots(roots) {
        const found = new Set();
        Array.from(roots).forEach(root => {
            // 第一遍：块级正文 + 合格链接（保留段落结构）
            root.querySelectorAll(BODY_TEXT_SELECTOR).forEach(n => {
                if (found.has(n)) return;
                if (!this.isBodyNode(n)) return;
                found.add(n);
            });
        });

        // 第二遍：叶子文本兜底收集。
        // 现代 SPA（Google News / 各类信息流）把来源名、相对时间、作者署名、
        // 日期标签全裹在 <div>/<span>/<time> 里，光靠块级标签选择器会漏掉一大片。
        // 只取「不含元素子节点」的纯文本叶子，并跳过已落在某个收集到的块级
        // 祖先里的（避免同一句话翻两遍）。
        Array.from(roots).forEach(root => {
            root.querySelectorAll(LEAF_TEXT_SELECTOR).forEach(n => {
                if (found.has(n)) return;
                if (n.children.length > 0) return; // 只收叶子，避免和已收集的块重复
                if (!this.isBodyNode(n)) return;

                // 已落在某个收集到的块级祖先里 → 交给外层元素整段翻译
                let p = n.parentElement, depth = 0;
                while (p && depth < 6) {
                    if (found.has(p)) return;
                    p = p.parentElement;
                    depth++;
                }

                const t = n.textContent.trim();
                if (t.length < MIN_LEAF_LENGTH) return;
                if (!/[a-zA-Z一-鿿]/.test(t)) return; // 纯数字/标点/符号没翻译价值
                found.add(n);
            });
        });

        return Array.from(found);
    }

    /**
     * 判断一个元素是否算「值得翻译的正文」。
     * 排除按强/弱信号分层处理，弱信号只就近匹配（见文件顶部说明）。
     */
    isBodyNode(n) {
        const rect = n.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return false;

        // 强信号：语义标签，沿整条祖先链匹配
        if (n.closest(EXCLUDE_SELECTOR_STRICT)) return false;

        // 中信号：cookie 条 / 付费墙 / 订阅框 / 广告 / 弹窗，沿整条祖先链匹配
        if (this.midExcluded(n)) return false;

        // 弱信号：布局类 class 子串，只在近几层祖先内生效
        if (this.nearExcluded(n)) return false;

        // 独立链接（不在块级正文里）才单独收集，且要够长
        if (n.tagName === 'A') {
            if (n.parentElement && n.parentElement.closest(BLOCK_ANCESTOR_SELECTOR)) return false;
            if (n.textContent.trim().length < MIN_ANCHOR_LENGTH) return false;
        }
        return true;
    }

    /**
     * 中信号排除：cookie 条 / 付费墙 / 订阅框 / 广告 / 弹窗覆盖层。
     * 这些类名不会出现在整页主体容器上，所以可以沿整条祖先链匹配。
     */
    midExcluded(node) {
        try {
            return !!node.closest(MID_EXCLUDE_SELECTOR);
        } catch (e) {
            return false;
        }
    }

    /**
     * 弱信号排除：只检查自身 + 近 depth 层祖先的 class 名子串。
     * 用 substring 而非 closest，是为了避免整页容器类名（--with-sidebar）
     * 把整篇正文误杀。返回命中说明应当跳过。
     */
    nearExcluded(node, depth = NEAR_EXCLUDE_DEPTH) {
        let el = node;
        for (let i = 0; el && el.nodeType === 1 && i <= depth; i++) {
            const cls = typeof el.className === 'string' ? el.className.toLowerCase() : '';
            if (cls) {
                for (const kw of NEAR_EXCLUDE_KEYWORDS) {
                    if (cls.includes(kw)) return true;
                }
            }
            el = el.parentElement;
        }
        return false;
    }

    /**
     * 取待翻文本。图标字体（Material Symbols 等）把图标名当文本渲染，
     * 直接取 textContent 会把 "chevron_right" 这类图标名混进译文，
     * 所以取文本前先把图标节点摘掉。
     */
    extractText(node) {
        const ICON = '[class*="icon" i], [class*="symbol" i]';
        if (!node.querySelector || !node.querySelector(ICON)) {
            return (node.textContent || '').trim();
        }
        const clone = node.cloneNode(true);
        clone.querySelectorAll(ICON).forEach(el => el.remove());
        return (clone.textContent || '').trim();
    }

    /**
     * 按「距当前视口中心的距离」排序：本地推理是串行的，
     * 让眼睛看着的地方先出译文，长页面体感差别很大。
     */
    sortByViewport(nodes) {
        if (nodes.length <= 1) return nodes;
        const center = window.scrollY + (window.innerHeight || 800) / 2;
        return nodes
            .map(n => ({ n, d: Math.abs(n.getBoundingClientRect().top + window.scrollY - center) }))
            .sort((a, b) => a.d - b.d)
            .map(x => x.n)
            .slice(0, MAX_PAGE_NODES);
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
