/**
 * Video Adapter
 * For sites like YouTube, Bilibili, Udemy
 *
 * 两种工作模式：
 *
 * 1. 轨道模式（YouTube 专属，subtitleTrack: 'youtube'）
 *    播放时逐句翻译 DOM 永远追不上说话速度（这也是沉浸式翻译等同类
 *    工具不用 DOM 监听的原因）。正确做法是拿到 YouTube 的 TimedText
 *    字幕轨道（带精确时间轴），把翻译提前跑到播放进度前面，播放时按
 *    currentTime 精准渲染。本地模型 0.25s/句，预翻译毫无压力。
 *
 * 2. DOM 观察模式（其他视频站 / 无字幕轨道时的兜底）
 *    MutationObserver 监听字幕容器变化，防抖后逐句翻译。
 */

// 服务端 prompt 的结构词 —— 译文里出现任意一个即视为指令回显，拒绝渲染
const PROMPT_ECHO_RE = /You are a professional|Translate the following text|TEXT TO TRANSLATE|RULES:|GLOSSARY|CONTEXT \(previous lines/;

// 目标语言 popup 配置名 -> ISO 代码（用于挑选字幕轨道）
const TARGET_LANG_CODES = {
    'Chinese': 'zh', 'English': 'en', 'Japanese': 'ja', 'Korean': 'ko',
    'French': 'fr', 'German': 'de', 'Spanish': 'es', 'Russian': 'ru'
};

// 轨道模式：提前翻译播放位置之后的 N 个字幕组
const TRACK_AHEAD_GROUPS = 25;

class VideoAdapter {
    constructor(siteConfig) {
        this.name = 'VideoAdapter';
        this.config = siteConfig;
        this.core = null;
        this.observer = null;
        this.lastText = '';
        // 上一句原文，作为上下文传给翻译服务。
        // HY-MT 1.8B 在孤立短句上容易误译（如把 "angst" 音译成人名），
        // 带上前一句能显著改善断句与语义判断。
        this.prevText = '';

        // 轨道模式状态
        this.trackMode = false;
        this.groups = null;                  // [{start, dur, text}]
        this.trackTranslations = new Map();  // groupIdx -> 译文（'' = 无需翻译）
        this.enqueuedGroups = new Set();
        this.currentGroupIdx = -2;           // -2 强制首次刷新
        this.timeSyncVideo = null;
    }

    attach(core) {
        this.core = core;
        console.log('HY-MT: VideoAdapter attached');
        this.injectSiteStyles();
        this.bindNavigationEvents();
        if (this.config.subtitleTrack === 'youtube') {
            this.initTrackMode();
        } else {
            this.startObserving();
        }
    }

    bindNavigationEvents() {
        // YouTube specific event for SPA navigation
        document.addEventListener('yt-navigate-finish', () => {
            console.log('HY-MT: Navigation detected (yt-navigate-finish)');
            this.reset();
        });
    }

    reset() {
        console.log('HY-MT: Resetting VideoAdapter state');
        this.lastText = '';
        this.prevText = '';
        this.hideTranslation();

        if (this.mutationTimer) {
            clearTimeout(this.mutationTimer);
            this.mutationTimer = null;
        }

        if (this.observer) {
            this.observer.disconnect();
            this.observer = null;
        }

        if (this.checkInterval) {
            clearInterval(this.checkInterval);
        }

        this.teardownTrackMode();

        // Restart for the new page structure
        if (this.config.subtitleTrack === 'youtube') {
            this.initTrackMode();
        } else {
            this.startObserving();
        }
    }

    injectSiteStyles() {
        if (this.config.injectCss) {
            const style = document.createElement('style');
            style.id = 'hy-mt-adapter-styles';
            style.textContent = this.config.injectCss;
            document.head.appendChild(style);
            console.log('HY-MT: Injected adapter styles');
        }
    }

    // =========================================================================
    // 模式一：TimedText 轨道预翻译（YouTube）
    // =========================================================================

    targetLangCode() {
        const name = this.core && this.core.config ? this.core.config.targetLanguage : 'Chinese';
        return TARGET_LANG_CODES[name] || 'zh';
    }

    async initTrackMode() {
        const tracks = await this.fetchCaptionTracks();
        if (!tracks || !tracks.length) {
            console.warn('HY-MT: 无可用字幕轨道，回退 DOM 观察模式');
            this.startObserving();
            return;
        }

        // 优先：非目标语言的人工字幕 > 非目标语言的 ASR > 第一个轨道
        const tc = this.targetLangCode();
        const track = tracks.find(t => !(t.languageCode || '').toLowerCase().startsWith(tc) && t.kind !== 'asr')
            || tracks.find(t => !(t.languageCode || '').toLowerCase().startsWith(tc))
            || tracks[0];

        try {
            const cues = await this.fetchTrackCues(track.baseUrl);
            const groups = cues && cues.length ? this.buildGroups(cues) : null;
            if (!groups || !groups.length) throw new Error('empty caption track');

            this.groups = groups;
            this.trackMode = true;
            this.bindTimeSync();
            console.log('HY-MT: 轨道模式就绪 — ' + groups.length + ' 段字幕（语言 ' + track.languageCode + '）');
        } catch (e) {
            console.warn('HY-MT: 字幕轨道拉取失败，回退 DOM 观察模式', e);
            this.teardownTrackMode();
            this.startObserving();
        }
    }

    /** 经页面主世界桥接脚本读取播放器的字幕轨道列表（SPA 切视频时最多重试一次） */
    fetchCaptionTracks() {
        const ask = () => new Promise((resolve) => {
            const root = document.documentElement;
            const timer = setTimeout(() => {
                root.removeEventListener('hy-mt-tracks-response', onResp);
                resolve(null);
            }, 2500);

            const onResp = (e) => {
                clearTimeout(timer);
                root.removeEventListener('hy-mt-tracks-response', onResp);
                try {
                    resolve(JSON.parse(e.detail));
                } catch (err) {
                    resolve(null);
                }
            };
            root.addEventListener('hy-mt-tracks-response', onResp);
            root.dispatchEvent(new CustomEvent('hy-mt-need-tracks'));
        });

        const matchesCurrentVideo = (data) => {
            if (!data || !data.tracks || !data.tracks.length) return false;
            // SPA 切换瞬间可能返回上一个视频的轨道，用 videoId 对齐当前 URL
            const m = location.search.match(/[?&]v=([\w-]+)/);
            if (data.videoId && m && m[1] && data.videoId !== m[1]) return false;
            return true;
        };

        return ask().then(data => {
            if (matchesCurrentVideo(data)) return data.tracks;
            // 换视频后播放器数据可能还没就绪，等 1.2s 再问一次
            return new Promise(r => setTimeout(r, 1200))
                .then(ask)
                .then(d => matchesCurrentVideo(d) ? d.tracks : null);
        });
    }

    /** 拉取轨道全文（json3 格式，带毫秒时间轴） */
    async fetchTrackCues(baseUrl) {
        const sep = baseUrl.includes('?') ? '&' : '?';
        const resp = await fetch(baseUrl + sep + 'fmt=json3');
        if (!resp.ok) throw new Error('timedtext HTTP ' + resp.status);
        const data = await resp.json();

        const cues = [];
        for (const ev of (data.events || [])) {
            if (!ev.segs) continue;
            const text = ev.segs.map(s => s.utf8 || '').join('').replace(/\s+/g, ' ').trim();
            if (!text) continue;
            cues.push({ start: ev.tStartMs || 0, dur: ev.dDurationMs || 2000, text });
        }
        return cues;
    }

    /**
     * 把碎片化 cue（ASR 尤其碎）合并成短句组再翻译：
     * 显示和翻译都以组为单位，语义完整、请求量减半。
     * 规则：组时长 < 6s、cue 间隔 < 1.2s、最多 4 条 cue，满足其一即断组。
     */
    buildGroups(cues) {
        const groups = [];
        let cur = null;
        for (const c of cues) {
            if (cur && (
                c.start - (cur.start + cur.dur) > 1200 ||
                (c.start + c.dur - cur.start) > 6000 ||
                cur.count >= 4
            )) {
                groups.push(cur);
                cur = null;
            }
            if (!cur) {
                cur = { start: c.start, dur: c.dur, text: c.text, count: 1 };
            } else {
                cur.dur = (c.start + c.dur) - cur.start;
                cur.text += ' ' + c.text;
                cur.count++;
            }
        }
        if (cur) groups.push(cur);
        return groups;
    }

    bindTimeSync() {
        const findVideo = () => {
            const v = document.querySelector('video.html5-main-video') || document.querySelector('video');
            if (!v) return false;
            this.timeSyncVideo = v;
            this._onTimeUpdate = () => this.onTimeUpdate();
            this._onSeeked = () => { this.currentGroupIdx = -2; };
            v.addEventListener('timeupdate', this._onTimeUpdate);
            v.addEventListener('seeked', this._onSeeked);
            this.onTimeUpdate(); // 立刻刷一次，不等第一个 timeupdate
            return true;
        };
        if (findVideo()) return;
        this.videoFindInterval = setInterval(() => {
            if (findVideo()) clearInterval(this.videoFindInterval);
        }, 500);
    }

    onTimeUpdate() {
        if (!this.trackMode || !this.groups || !this.timeSyncVideo) return;

        const t = this.timeSyncVideo.currentTime * 1000;
        const idx = this.findGroupIndex(t);

        if (idx !== this.currentGroupIdx) {
            this.currentGroupIdx = idx;
            const g = idx >= 0 ? this.groups[idx] : null;
            if (!g) {
                this.hideTranslation();
            } else if (!this.trackTranslations.has(idx)) {
                // 译文未就绪：先隐藏避免错位，同时立刻补翻
                this.hideTranslation();
                this.enqueueGroups([idx]);
            } else {
                const tr = this.trackTranslations.get(idx);
                if (tr) {
                    this.render(tr, g.text);
                } else {
                    this.hideTranslation(); // 无需翻译或翻译失败
                }
            }
        }

        if (idx >= 0) this.ensureAhead(idx);
    }

    /** 二分查找当前时间所在的字幕组；组间空隙返回 -1 */
    findGroupIndex(t) {
        let lo = 0, hi = this.groups.length - 1, ans = -1;
        while (lo <= hi) {
            const mid = (lo + hi) >> 1;
            if (t < this.groups[mid].start) {
                hi = mid - 1;
            } else {
                ans = mid;
                lo = mid + 1;
            }
        }
        if (ans === -1) return -1;
        const g = this.groups[ans];
        return (t <= g.start + g.dur + 500) ? ans : -1;
    }

    /** 保证播放位置之后 TRACK_AHEAD_GROUPS 个字幕组都在翻译队列里 */
    ensureAhead(idx) {
        const end = Math.min(idx + TRACK_AHEAD_GROUPS, this.groups.length - 1);
        const todo = [];
        for (let i = idx; i <= end; i++) {
            if (!this.enqueuedGroups.has(i)) todo.push(i);
        }
        if (todo.length) this.enqueueGroups(todo);
    }

    enqueueGroups(idxs) {
        for (const i of idxs) {
            if (this.enqueuedGroups.has(i)) continue;
            this.enqueuedGroups.add(i);
            const g = this.groups[i];
            if (!g || !g.text) {
                this.trackTranslations.set(i, '');
                continue;
            }
            this.core.translate(g.text, { priority: 'high' })
                .then(tr => {
                    // null = 同语言无需翻译或失败；'' 表示已处理完，避免反复重试
                    this.trackTranslations.set(i, tr || '');
                })
                .catch(() => {
                    this.trackTranslations.set(i, '');
                });
        }
    }

    teardownTrackMode() {
        if (this.timeSyncVideo && this._onTimeUpdate) {
            this.timeSyncVideo.removeEventListener('timeupdate', this._onTimeUpdate);
            this.timeSyncVideo.removeEventListener('seeked', this._onSeeked);
        }
        if (this.videoFindInterval) clearInterval(this.videoFindInterval);
        this.timeSyncVideo = null;
        this.trackMode = false;
        this.groups = null;
        this.trackTranslations = new Map();
        this.enqueuedGroups = new Set();
        this.currentGroupIdx = -2;
    }

    // =========================================================================
    // 模式二：DOM 观察兜底（其他视频站 / 无字幕轨道）
    // =========================================================================

    startObserving() {
        const containerSelector = this.config.containerSelector;

        // Clear any existing interval
        if (this.checkInterval) clearInterval(this.checkInterval);

        // Polling to wait for player load
        this.checkInterval = setInterval(() => {
            const container = document.querySelector(containerSelector);
            if (container) {
                clearInterval(this.checkInterval);
                this.checkInterval = null;
                console.log(`HY-MT: Subtitle container found: ${containerSelector}`);
                this.observeContainer(container);
            }
        }, 1000);
    }

    observeContainer(container) {
        this.observer = new MutationObserver(() => {
            this.handleMutation(container);
        });

        this.observer.observe(container, {
            childList: true,
            subtree: true,
            characterData: true
        });
    }

    handleMutation(container) {
        // 字幕快速变化（连续说话逐词刷新、拖进度条）时抖动合并：
        // 250ms 内只取最终状态，等说话节奏的间隙再送翻译
        if (this.mutationTimer) clearTimeout(this.mutationTimer);
        this.mutationTimer = setTimeout(() => {
            this.mutationTimer = null;
            this.extractAndTranslate(container);
        }, 250);
    }

    extractAndTranslate(container) {
        // 拖动进度条过程中字幕会飞速掠过，逐条翻译既浪费推理又容易触发
        // 模型指令回显；等 seek 结束后由最后一次 mutation 正常触发。
        const video = document.querySelector('video');
        if (video && video.seeking) return;

        // Extract text based on selector
        const textNodes = container.querySelectorAll(this.config.textSelector);
        if (!textNodes || textNodes.length === 0) {
            this.hideTranslation();
            return;
        }

        const fullText = Array.from(textNodes)
            .map(node => node.textContent.trim())
            .join(' ')
            .trim();

        if (fullText && fullText !== this.lastText) {
            this.lastText = fullText;
            this.triggerTranslation(fullText);
        }
    }

    async triggerTranslation(text) {
        // 记录本次请求对应的字幕，响应回来时如果字幕已换（或已在 seek），
        // 这份过期译文就不再渲染，避免字幕闪回旧内容
        const requestText = text;
        const translated = await this.core.translate(text, {
            priority: 'high',
            context: this.prevText
        });

        // 无论成败都推进上下文，避免同一句反复作为 context
        this.prevText = text;

        if (!translated) return;
        // 服务端漏网的指令回显，渲染前最后拦截
        if (PROMPT_ECHO_RE.test(translated)) {
            console.warn('HY-MT: 丢弃疑似 prompt 回显的译文:', translated.slice(0, 80));
            return;
        }
        // 字幕已经切到下一句了，过期译文不显示
        if (this.lastText !== requestText) return;

        this.render(translated, requestText);
    }

    // =========================================================================
    // 渲染（两种模式共用）：滚动双行字幕
    // =========================================================================

    /**
     * 滚动双行字幕：上一句译文淡化显示在上方，当前句高亮在下方。
     * 连续说话时字幕文字会持续更新，只显示单行会不停"闪换"无法阅读；
     * 保留上一句让视线有落点，阅读体验接近专业双语字幕。
     * @param {string} translated 译文
     * @param {string} original   原文（双语模式下显示在最上方）
     */
    render(translated, original) {
        let overlay = document.getElementById('hy-mt-video-overlay');
        if (!overlay) {
            overlay = document.createElement('div');
            overlay.id = 'hy-mt-video-overlay';
            overlay.className = 'hy-mt-video-overlay';
            overlay.style.bottom = '15px';

            const player = document.querySelector(this.config.videoPlayerSelector || 'body');
            if (player) {
                player.appendChild(overlay);
                this.startPositionSync(overlay, player);
            } else {
                document.body.appendChild(overlay);
                overlay.style.position = 'fixed';
                overlay.style.bottom = '15px';
            }
        }

        overlay.textContent = '';

        // 双语模式：原文（半透明小字）在最上方
        if (this.core.config.bilingualSubtitle && original) {
            const origLine = document.createElement('div');
            origLine.className = 'hy-mt-video-text hy-mt-video-original';
            origLine.textContent = original;
            overlay.appendChild(origLine);
        }

        const transLine = document.createElement('div');
        transLine.className = 'hy-mt-video-text';
        transLine.textContent = translated;
        overlay.appendChild(transLine);

        overlay.style.display = 'block';
    }

    startPositionSync(overlay, player) {
        if (this.syncLoop) return;

        const loop = () => {
            // Check for YouTube controls visibility
            const controlsVisible = !player.classList.contains('ytp-autohide');

            // 1. Toggle state class on player for CSS to use
            if (controlsVisible) {
                player.classList.add('hy-mt-mode-controls');
            } else {
                player.classList.remove('hy-mt-mode-controls');
            }

            // 2. Set Overlay Position based on state
            // We use fixed comfortable positions instead of chasing pixels
            if (controlsVisible) {
                // Lift high enough to clear the progress bar & timestamp
                overlay.style.bottom = '85px';
            } else {
                // Low but not too low (comfortable reading)
                overlay.style.bottom = '15px';
            }

            this.syncLoop = requestAnimationFrame(loop);
        };
        this.syncLoop = requestAnimationFrame(loop);
    }

    hideTranslation() {
        const overlay = document.getElementById('hy-mt-video-overlay');
        if (overlay) {
            overlay.style.display = 'none';
        }
    }
}

// Export for usage
window.VideoAdapter = VideoAdapter;
