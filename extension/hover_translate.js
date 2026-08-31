/**
 * 段落悬停翻译：按住修饰键（默认 Ctrl）悬停段落 → 就地插入译文，移出/松键即消失
 *
 * 与划词翻译互补：划词查单词（浮层），悬停扫读整段（就地）。
 * 与适配器体系独立：不注册 adapter，只读配置（enabled + 黑名单 + hoverTranslate +
 * hoverModifier + targetLanguage），复用 background 的 translate（自动走持久缓存）。
 * 译文复用 .hy-mt-page-item 样式（配色可经设置视图「译文样式」调节）。
 *
 * 交互规则：
 *   - 默认开启，但必须按住修饰键才触发，不干扰正常浏览
 *   - 移出段落 0.5s 或松开修饰键 → 移除译文
 *   - 同段重复悬停命中页面内缓存，秒出
 *   - Esc 立即清除当前译文
 */
(() => {
    if (window.__hyMtHoverTranslateLoaded) return;
    window.__hyMtHoverTranslateLoaded = true;

    let config = null;
    let modifierPressed = false;
    let hoverTimer = null;   // 悬停防抖（找段落）
    let removeTimer = null;  // 移出段落后的延迟移除
    let currentEl = null;    // 当前悬停的段落元素
    let currentDiv = null;   // 当前插入的译文节点
    const hoverCache = new Map();  // 段落文本 -> 译文（页面内 L1，跨段重复悬停秒出）

    chrome.runtime.sendMessage({ action: 'getConfig' }, (resp) => {
        if (resp && resp.success) config = resp.config;
    });

    chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
        if (request.action === 'configUpdated' && request.config) {
            config = request.config;
            if (config.hoverTranslate === false) hideCurrent();
        }
        return false;
    });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') { hideCurrent(); return; }
        if (isModifierKey(e)) modifierPressed = true;
    }, true);
    document.addEventListener('keyup', (e) => {
        if (isModifierKey(e)) {
            modifierPressed = false;
            hideCurrent(); // 松开修饰键立即清理
        }
    }, true);
    document.addEventListener('mousemove', onMouseMove, true);

    function isModifierKey(e) {
        const want = (config && config.hoverModifier === 'alt') ? 'Alt' : 'Control';
        return e.key === want;
    }

    function available() {
        if (!config || config.enabled === false || config.hoverTranslate === false) return false;
        const host = location.hostname;
        return !(config.blacklist || []).some(d => host.includes(d));
    }

    function onMouseMove(e) {
        // 已显示译文：判断是否仍悬停在同一段落，移出 0.5s 后移除
        if (currentEl) {
            if (currentEl.contains(e.target)) {
                if (removeTimer) { clearTimeout(removeTimer); removeTimer = null; }
            } else if (!removeTimer) {
                removeTimer = setTimeout(hideCurrent, 500);
            }
        }

        if (!modifierPressed || !available()) return;

        if (hoverTimer) clearTimeout(hoverTimer);
        hoverTimer = setTimeout(() => {
            hoverTimer = null;
            handleHover(e.target);
        }, 250);
    }

    function handleHover(target) {
        if (!(target instanceof Element)) return;
        const el = target.closest('p, li, h1, h2, h3, h4, blockquote');
        if (!el || el === currentEl) return;
        // 不作用在自己的 UI 上（字幕浮层/视频容器内部）
        if (el.closest('#hy-mt-video-overlay')) return;

        const text = (el.textContent || '').trim();
        if (text.length < 2) return;

        hideCurrent();
        currentEl = el;

        // 该段已有译文（整页翻译或上次悬停）时直接复用，不重复翻译
        if (el.nextSibling && el.nextSibling.classList &&
            el.nextSibling.classList.contains('hy-mt-page-item')) {
            currentDiv = el.nextSibling;
            return;
        }

        if (hoverCache.has(text)) {
            insertTranslation(el, hoverCache.get(text));
            return;
        }

        chrome.runtime.sendMessage(
            {
                action: 'translate',
                text,
                targetLanguage: (config && config.targetLanguage) || 'Chinese',
                priority: 'normal'
            },
            (resp) => {
                // 响应回来时可能已经移走/换了段落，过期译文不插入
                if (!currentEl || currentEl !== el) return;
                const translation = resp && resp.success ? resp.translation : null;
                if (!translation) return;
                hoverCache.set(text, translation);
                insertTranslation(el, translation);
            }
        );
    }

    function insertTranslation(el, text) {
        hideCurrent();
        const div = document.createElement('div');
        div.className = 'hy-mt-page-item';
        div.textContent = text;
        el.parentNode.insertBefore(div, el.nextSibling);
        currentDiv = div;
        currentEl = el;
    }

    function hideCurrent() {
        if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
        if (removeTimer) { clearTimeout(removeTimer); removeTimer = null; }
        if (currentDiv) { currentDiv.remove(); currentDiv = null; }
        currentEl = null;
    }
})();
