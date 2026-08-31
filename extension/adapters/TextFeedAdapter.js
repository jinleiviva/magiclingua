/**
 * Text Feed Adapter
 * For sites like Reddit, X.com, Hacker News
 */
class TextFeedAdapter {
    constructor(siteConfig) {
        this.name = 'TextFeedAdapter';
        this.config = siteConfig;
        this.core = null;
        this.processedSet = new WeakSet(); // Track DOM nodes we've handled
        this.observer = null;
    }

    attach(core) {
        this.core = core;
        console.log('HY-MT: TextFeedAdapter attached');
        this.initIntersectionObserver();
    }

    initIntersectionObserver() {
        // Options: Trigger when element is 10% visible
        const options = {
            root: null, // viewport
            rootMargin: '500px 0px', // Pre-load 500px ahead (approx 1 screen)
            threshold: 0.1
        };

        this.observer = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    this.processNode(entry.target);
                    // Stop observing once processed to save performance
                    this.observer.unobserve(entry.target);
                }
            });
        }, options);

        // Start watching for new content
        this.watchDOM();
    }

    watchDOM() {
        // Watch for new posts loading (Infinite Scroll)
        const observer = new MutationObserver((mutations) => {
            mutations.forEach(mutation => {
                mutation.addedNodes.forEach(node => {
                    if (node.nodeType === 1) { // Element
                        // Check if node itself matches or children match
                        this.scanForTargets(node);
                    }
                });
            });
        });

        observer.observe(document.body, { childList: true, subtree: true });

        // Initial Scan
        this.scanForTargets(document.body);
    }

    scanForTargets(rootNode) {
        const targets = rootNode.querySelectorAll(this.config.containerSelector);
        targets.forEach(target => {
            if (!this.processedSet.has(target)) {
                this.processedSet.add(target);
                this.observer.observe(target); // Add to viewport observer
            }
        });
    }

    async processNode(container) {
        // Extract ALL text nodes matching selector (e.g., all <p> in a post)
        const textNodes = container.querySelectorAll(this.config.textSelector);
        if (!textNodes || textNodes.length === 0) return;

        // Process each paragraph independently
        for (const node of textNodes) {
            // 跳过被 exclude 规则命中的节点（付费墙、推荐位、广告、订阅框）
            if (this.isExcluded(node)) continue;
            this.processParagraph(node);
        }
    }

    /**
     * 判断节点是否落在站点配置的排除选择器里。
     * 同时检查节点自身与其祖先，因为推荐卡片常把 <p> 包在外层容器里。
     */
    isExcluded(node) {
        const exclude = this.config.excludeSelector;
        if (!exclude) return false;

        try {
            if (node.matches(exclude)) return true;
            if (node.closest(exclude)) return true;
        } catch (e) {
            // 选择器非法时保守放行，避免站点配置错误导致整站不翻
            console.warn('HY-MT: excludeSelector 无效', exclude, e);
        }
        return false;
    }

    async processParagraph(node) {
        // Skip if already translated (check for adjacent translation element)
        if (node.nextSibling && node.nextSibling.classList && node.nextSibling.classList.contains('hy-mt-text-feed-item')) {
            return;
        }

        const text = node.textContent.trim();
        if (text.length < 5) return; // Too short

        // 1. Show Loading Spinner
        const spinner = document.createElement('div');
        spinner.className = 'hy-mt-loading-spinner';
        node.appendChild(spinner);

        try {
            // 2. Translate
            const translated = await this.core.translate(text);

            // 3. Remove Spinner
            if (spinner.parentNode) spinner.parentNode.removeChild(spinner);

            if (translated) {
                this.render(node, translated);
            }
        } catch (e) {
            if (spinner.parentNode) spinner.parentNode.removeChild(spinner);
            this.showRetry(node);
        }
    }

    showRetry(targetNode) {
        if (targetNode.nextSibling && targetNode.nextSibling.classList && targetNode.nextSibling.classList.contains('hy-mt-retry-btn')) {
            return;
        }

        const retryBtn = document.createElement('div');
        retryBtn.className = 'hy-mt-retry-btn';
        retryBtn.title = 'Retry Translation';
        retryBtn.innerHTML = `
            <svg class="hy-mt-retry-icon" viewBox="0 0 24 24">
                <path d="M17.65 6.35C16.2 4.9 14.21 4 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08c-.82 2.33-3.04 4-5.65 4-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/>
            </svg>
        `;

        retryBtn.onclick = (e) => {
            e.stopPropagation();
            e.preventDefault();
            retryBtn.remove();
            this.processParagraph(targetNode);
        };

        // Insert after target
        if (targetNode.parentNode) {
            targetNode.parentNode.insertBefore(retryBtn, targetNode.nextSibling);
        }
    }

    render(targetNode, text) {
        // Double check to prevent duplicate injection
        if (targetNode.nextSibling && targetNode.nextSibling.classList && targetNode.nextSibling.classList.contains('hy-mt-text-feed-item')) {
            return;
        }

        const transDiv = document.createElement('div');
        transDiv.className = 'hy-mt-text-feed-item';
        transDiv.textContent = text;

        const mode = this.core.config.displayMode || 'append';

        if (mode === 'replace') {
            // Hide original content
            targetNode.style.display = 'none';
            // Mark as hidden by us for potential restoration
            targetNode.setAttribute('data-hy-mt-hidden', 'true');
        }

        // Insert after the paragraph
        if (targetNode.parentNode) {
            targetNode.parentNode.insertBefore(transDiv, targetNode.nextSibling);
        }
    }
}

// Export for usage
window.TextFeedAdapter = TextFeedAdapter;
