/**
 * 划词翻译：选中文字 → 浮动气泡显示译文
 *
 * 三个入口：
 *   1. 选中后出现在选区旁的「译」小按钮
 *   2. 右键菜单「翻译选中内容」（background 转发 translateSelection 消息）
 *   3. 快捷键（默认 Alt+T，chrome://extensions/shortcuts 可改）
 *
 * 与适配器体系独立：不注册 adapter，只读配置（总开关 enabled + 黑名单），
 * 目标语言跟随 popup 设置。Esc 或点击面板外可关闭。
 */
(() => {
    if (window.__hyMtSelectionBubbleLoaded) return;
    window.__hyMtSelectionBubbleLoaded = true;

    let config = null;
    let trigger = null;  // 选区旁的「译」按钮
    let panel = null;    // 译文面板

    injectStyles();

    chrome.runtime.sendMessage({ action: 'getConfig' }, (resp) => {
        if (resp && resp.success) config = resp.config;
    });

    chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
        if (request.action === 'translateSelection') {
            const sel = window.getSelection();
            const text = sel ? sel.toString().trim() : '';
            if (text.length >= 2) showPanel(text, getSelectionRect(sel));
            sendResponse({ ok: true });
        } else if (request.action === 'configUpdated' && request.config) {
            config = request.config;
        }
        return false;
    });

    document.addEventListener('mouseup', onMouseUp);
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') hideAll();
    }, true);
    // 点击面板/按钮之外的区域关闭面板（capture 阶段，避免被站点 stopPropagation 吞掉）
    document.addEventListener('mousedown', (e) => {
        if (panel && !panel.contains(e.target)) hideAll();
    }, true);

    function available() {
        if (!config || config.enabled === false) return false;
        const host = location.hostname;
        return !(config.blacklist || []).some(d => host.includes(d));
    }

    function getSelectionRect(sel) {
        if (sel && sel.rangeCount) {
            const rect = sel.getRangeAt(0).getBoundingClientRect();
            if (rect && (rect.width || rect.height)) return rect;
        }
        return null;
    }

    function onMouseUp(e) {
        // 点在自己的 UI 上时交给对应元素的处理器
        if ((panel && panel.contains(e.target)) || (trigger && trigger.contains(e.target))) return;

        setTimeout(() => {
            const sel = window.getSelection();
            const text = sel ? sel.toString().trim() : '';
            if (!text || text.length < 2) { hideTrigger(); return; }
            if (!available()) return;
            const rect = getSelectionRect(sel);
            if (!rect) return;
            showTrigger(rect, text);
        }, 10);
    }

    // --- 「译」按钮 ---

    function showTrigger(rect, text) {
        hideTrigger();
        trigger = document.createElement('div');
        trigger.className = 'hy-mt-select-trigger';
        trigger.textContent = '译';
        trigger.title = '翻译选中内容';
        trigger.style.left = (rect.right + window.scrollX + 6) + 'px';
        trigger.style.top = (rect.top + window.scrollY - 4) + 'px';
        trigger.addEventListener('mousedown', (e) => {
            e.preventDefault();
            e.stopPropagation();
            hideTrigger();
            showPanel(text, rect);
        });
        document.documentElement.appendChild(trigger);
    }

    function hideTrigger() {
        if (trigger) { trigger.remove(); trigger = null; }
    }

    // --- 译文面板 ---

    function showPanel(text, rect) {
        hideAll();

        panel = document.createElement('div');
        panel.className = 'hy-mt-select-panel';

        const header = document.createElement('div');
        header.className = 'hy-mt-select-header';
        const title = document.createElement('span');
        title.textContent = '划词翻译';
        const close = document.createElement('span');
        close.className = 'hy-mt-select-close';
        close.textContent = '×';
        close.title = '关闭 (Esc)';
        close.addEventListener('click', hideAll);
        header.appendChild(title);
        header.appendChild(close);

        const original = document.createElement('div');
        original.className = 'hy-mt-select-original';
        original.textContent = text.length > 200 ? text.slice(0, 200) + '…' : text;

        const body = document.createElement('div');
        body.className = 'hy-mt-select-body';
        body.textContent = '翻译中…';

        panel.appendChild(header);
        panel.appendChild(original);
        panel.appendChild(body);
        document.documentElement.appendChild(panel);
        positionPanel(rect);

        chrome.runtime.sendMessage(
            {
                action: 'translate',
                text,
                targetLanguage: (config && config.targetLanguage) || 'Chinese',
                priority: 'high'
            },
            (resp) => {
                if (!panel) return; // 面板已被关掉
                if (resp && resp.success && resp.translation) {
                    body.textContent = resp.translation;
                } else {
                    body.textContent = '翻译失败' + (resp && resp.error ? '：' + resp.error : '');
                    body.classList.add('hy-mt-select-error');
                }
            }
        );
    }

    function positionPanel(rect) {
        if (!panel) return;
        const margin = 8;
        const vw = document.documentElement.clientWidth;

        // 先挂载再量尺寸，默认放在选区下方；放不下且上方有空间时放上方
        panel.style.visibility = 'hidden';
        requestAnimationFrame(() => {
            if (!panel) return;
            const h = panel.offsetHeight;
            const w = panel.offsetWidth;
            const anchorTop = rect ? rect.top + window.scrollY : window.scrollY + 40;
            const anchorBottom = rect ? rect.bottom + window.scrollY : window.scrollY + 40;
            const anchorLeft = rect ? rect.left + window.scrollX : window.scrollX + 40;

            const below = anchorBottom + margin;
            const top = (rect && below + h > window.scrollY + window.innerHeight && rect.top > h + margin)
                ? anchorTop - h - margin
                : below;
            const left = Math.min(
                Math.max(window.scrollX + margin, anchorLeft),
                window.scrollX + vw - w - margin
            );

            panel.style.left = Math.max(window.scrollX + margin, left) + 'px';
            panel.style.top = top + 'px';
            panel.style.visibility = 'visible';
        });
    }

    function hideAll() {
        hideTrigger();
        if (panel) { panel.remove(); panel = null; }
    }

    // --- 样式 ---

    function injectStyles() {
        const style = document.createElement('style');
        style.id = 'hy-mt-select-bubble-styles';
        style.textContent = `
            .hy-mt-select-trigger {
                position: absolute;
                z-index: 2147483647;
                width: 24px;
                height: 24px;
                line-height: 24px;
                text-align: center;
                border-radius: 50%;
                background: #2563eb;
                color: #fff;
                font-size: 13px;
                font-family: system-ui, -apple-system, sans-serif;
                cursor: pointer;
                box-shadow: 0 2px 8px rgba(0, 0, 0, 0.25);
                user-select: none;
                transition: transform 0.15s ease;
            }
            .hy-mt-select-trigger:hover { transform: scale(1.15); }

            .hy-mt-select-panel {
                position: absolute;
                z-index: 2147483647;
                max-width: 420px;
                min-width: 200px;
                background: #ffffff;
                color: #1f2937;
                border: 1px solid rgba(0, 0, 0, 0.08);
                border-radius: 10px;
                box-shadow: 0 8px 30px rgba(0, 0, 0, 0.18);
                font-family: system-ui, -apple-system, sans-serif;
                font-size: 14px;
                line-height: 1.55;
                overflow: hidden;
            }

            .hy-mt-select-header {
                display: flex;
                align-items: center;
                justify-content: space-between;
                padding: 6px 12px;
                background: rgba(37, 99, 235, 0.06);
                font-size: 12px;
                color: #6b7280;
                user-select: none;
            }
            .hy-mt-select-close {
                cursor: pointer;
                font-size: 16px;
                line-height: 1;
                padding: 0 2px;
                color: #9ca3af;
            }
            .hy-mt-select-close:hover { color: #374151; }

            .hy-mt-select-original {
                padding: 8px 12px 0;
                font-size: 12px;
                color: #9ca3af;
                max-height: 72px;
                overflow-y: auto;
            }

            .hy-mt-select-body {
                padding: 6px 12px 10px;
                white-space: pre-wrap;
                word-break: break-word;
            }
            .hy-mt-select-body:empty { display: none; }
            .hy-mt-select-error { color: #dc2626; font-size: 13px; }
        `;
        document.documentElement.appendChild(style);
    }
})();
