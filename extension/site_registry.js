/**
 * HY-MT Site Registry
 *
 * 每个站点告诉适配器三件事：
 *   containerSelector  正文容器在哪
 *   textSelector       容器里哪些元素是正文段落
 *   excludeSelector    哪些区域必须跳过（付费墙、推荐位、订阅框、广告）
 *
 * 没有精确匹配的站点会走 GENERIC 配置（语义标签 <article>/<main>），
 * 而不是 AutoAdapter 的启发式打分——启发式在财经站上误伤严重，
 * 会把「相关阅读」「订阅我们的新闻通讯」当成正文翻译掉。
 */

const HY_MT_SITES = {
    // ------------------------------------------------------------------
    // 视频站点
    // ------------------------------------------------------------------
    'youtube.com': {
        type: 'video',
        optimizationLevel: 'verified',
        adapter: 'VideoAdapter',
        subtitleTrack: 'youtube',
        containerSelector: '.ytp-caption-window-container',
        textSelector: '.ytp-caption-segment',
        videoPlayerSelector: '.html5-video-player',
        injectCss: `
            .html5-video-player .ytp-caption-window-bottom,
            .html5-video-player .ytp-caption-window-container {
                bottom: 80px !important;
                margin-bottom: 0 !important;
                transition: bottom 0.1s ease-out;
            }
            .html5-video-player.hy-mt-mode-controls .ytp-caption-window-bottom,
            .html5-video-player.hy-mt-mode-controls .ytp-caption-window-container {
                bottom: 160px !important;
                margin-bottom: 0 !important;
            }
        `
    },
    'bilibili.com': {
        type: 'video',
        optimizationLevel: 'verified',
        adapter: 'VideoAdapter',
        containerSelector: '.bilibili-player-video-subtitle',
        textSelector: '.subtitle-item-text',
        videoPlayerSelector: '#bilibili-player'
    },

    // ------------------------------------------------------------------
    // 财经 / 新闻站点
    //
    // 选择器基于各站当前公开页面结构编写。前端改版后可能失效，
    // 届時只需改这里的字符串，不用动适配器代码。
    // ------------------------------------------------------------------
    'ft.com': {
        type: 'text_feed',
        optimizationLevel: 'verified',
        adapter: 'TextFeedAdapter',
        containerSelector: 'article, .article__content, [class*="article-body"]',
        textSelector: 'p, h1, h2, .article__subheading',
        excludeSelector: '.o-ads, .o-teaser, .article__aside, .newsletter-signup, .related-stories, .article__share, figcaption, .o-typography-caption'
    },
    'bloomberg.com': {
        type: 'text_feed',
        optimizationLevel: 'verified',
        adapter: 'TextFeedAdapter',
        containerSelector: 'article, [class*="body-content"], [class*="BodyContent"], main',
        textSelector: 'p, h1, h2',
        excludeSelector: '[class*="paywall"], [class*="Paywall"], [data-ad], [class*="newsletter"], [class*="subscribe"], [class*="related"], figcaption, aside'
    },
    'wsj.com': {
        type: 'text_feed',
        optimizationLevel: 'verified',
        adapter: 'TextFeedAdapter',
        containerSelector: 'article, [class*="article-content"], [class*="ArticleBody"], main',
        textSelector: 'p, h1, h2',
        excludeSelector: '[class*="paywall"], [class*="wsj-ad"], [class*="newsletter"], [class*="subscribe"], [class*="related"], [class*="promo"], figcaption, aside'
    },
    'reuters.com': {
        type: 'text_feed',
        optimizationLevel: 'verified',
        adapter: 'TextFeedAdapter',
        containerSelector: 'article, [data-testid*="ArticleBody"], main',
        textSelector: 'p, h1, h2',
        excludeSelector: '[class*="paywall"], [data-testid*="related"], [class*="newsletter"], [class*="subscribe"], [class*="promo"], figcaption, aside'
    },
    'economist.com': {
        type: 'text_feed',
        optimizationLevel: 'verified',
        adapter: 'TextFeedAdapter',
        containerSelector: 'article, [class*="article__body"], main',
        textSelector: 'p, h1, h2, h3',
        excludeSelector: '[class*="paywall"], [class*="newsletter"], [class*="subscribe"], [class*="related"], [class*="promo"], figcaption, aside, .advert'
    },
    'asia.nikkei.com': {
        type: 'text_feed',
        optimizationLevel: 'verified',
        adapter: 'TextFeedAdapter',
        containerSelector: 'article, [class*="article-body"], main',
        textSelector: 'p, h1, h2',
        excludeSelector: '[class*="paywall"], [class*="newsletter"], [class*="subscribe"], [class*="related"], figcaption, aside'
    },
    'cnbc.com': {
        type: 'text_feed',
        optimizationLevel: 'verified',
        adapter: 'TextFeedAdapter',
        containerSelector: 'article, [class*="ArticleBody"], [class*="article-body"], main',
        textSelector: 'p, h1, h2',
        excludeSelector: '[class*="paywall"], [class*="newsletter"], [class*="subscribe"], [class*="related"], [class*="promo"], figcaption, aside'
    },
    'nytimes.com': {
        type: 'text_feed',
        optimizationLevel: 'verified',
        adapter: 'TextFeedAdapter',
        containerSelector: 'article, [name="articleBody"], section[name="articleBody"], main',
        textSelector: 'p, h1, h2',
        excludeSelector: '[class*="paywall"], [class*="newsletter"], [class*="subscribe"], [class*="related"], [class*="promo"], figcaption, aside'
    },
    'theatlantic.com': {
        type: 'text_feed',
        optimizationLevel: 'verified',
        adapter: 'TextFeedAdapter',
        containerSelector: 'article, [class*="article-body"], main',
        textSelector: 'p, h1, h2',
        excludeSelector: '[class*="paywall"], [class*="newsletter"], [class*="subscribe"], [class*="related"], figcaption, aside'
    },
    'substack.com': {
        type: 'text_feed',
        optimizationLevel: 'verified',
        adapter: 'TextFeedAdapter',
        containerSelector: '.available-content, article, .post',
        textSelector: 'p, h1, h2, h3, li',
        excludeSelector: '.subscribe-widget, .footer, .captcha-container, .comment-list, [class*="subscribe"], [class*="paywall"], .post-footer, nav'
    },
    'medium.com': {
        type: 'text_feed',
        optimizationLevel: 'verified',
        adapter: 'TextFeedAdapter',
        containerSelector: 'article, main',
        textSelector: 'p, h1, h2, h3',
        excludeSelector: '[class*="paywall"], [class*="promo"], [class*="related"], figcaption, aside, nav, footer'
    },

    // ------------------------------------------------------------------
    // 社交 / 论坛
    // ------------------------------------------------------------------
    'news.google.com': {
        type: 'text_feed',
        optimizationLevel: 'verified',
        adapter: 'TextFeedAdapter',
        // Google News 标题是 <a class="gPFEn">，不是 p/h 系列标签
        containerSelector: 'article, c-wiz [role="main"], main',
        textSelector: 'a.gPFEn, article h3, article h4, article p',
        excludeSelector: '[class*="promo"], [class*="sponsored"]'
    },
    'reddit.com': {
        type: 'text_feed',
        optimizationLevel: 'verified',
        adapter: 'TextFeedAdapter',
        containerSelector: 'shreddit-post, shreddit-comment',
        textSelector: 'div[slot="text-body"] p, div[slot="comment"] p, #post-title, .md p, .RichTextJSON-root p',
        excludeSelector: 'shreddit-comment-action-row, [class*="promo"]'
    },
    'x.com': {
        type: 'text_feed',
        optimizationLevel: 'verified',
        adapter: 'TextFeedAdapter',
        containerSelector: '[data-testid="tweetText"]',
        textSelector: 'span',
        excludeSelector: '[class*="promoted"], [data-testid="placementTracking"]'
    },
    'twitter.com': {
        type: 'text_feed',
        optimizationLevel: 'verified',
        adapter: 'TextFeedAdapter',
        containerSelector: '[data-testid="tweetText"]',
        textSelector: 'span',
        excludeSelector: '[class*="promoted"], [data-testid="placementTracking"]'
    }
};

/**
 * 通用文章配置。
 *
 * 未精确匹配的站点走这里。策略是用语义标签（<article> / <main> / [role=main]）
 * 定位正文，再靠一份比较全的排除名单挡掉非正文区域。
 * 这比 AutoAdapter 靠 class 名猜「像不像导航栏」可靠得多。
 */
const GENERIC_ARTICLE = {
    type: 'text_feed',
    optimizationLevel: 'generic',
    adapter: 'TextFeedAdapter',
    containerSelector: 'article, main, [role="main"], [class*="article-body"], [class*="articleBody"], [class*="post-content"], [class*="entry-content"]',
    textSelector: 'p, h1, h2, h3',
    excludeSelector: [
        'nav', 'aside', 'footer', 'header',
        '[role="navigation"]', '[role="complementary"]',
        '[class*="paywall"]', '[class*="newsletter"]', '[class*="subscribe"]',
        '[class*="related"]', '[class*="recommend"]', '[class*="promo"]',
        '[class*="social"]', '[class*="share"]', '[class*="comment"]',
        '[class*="advert"]', '[class*="cookie"]', '[class*="sidebar"]',
        'figcaption', 'form'
    ].join(', ')
};

// Bootstrap Logic
(function bootstrap() {
    const hostname = window.location.hostname;
    let config = null;

    // 最长后缀优先匹配，避免 'x.com' 误命中 'substack.com' 之类
    const domains = Object.keys(HY_MT_SITES).sort((a, b) => b.length - a.length);
    for (const domain of domains) {
        if (hostname === domain || hostname.endsWith('.' + domain)) {
            config = HY_MT_SITES[domain];
            break;
        }
    }

    // 没有精确匹配时走通用文章配置，而不是让 AutoAdapter 去猜
    if (!config) {
        console.log(`HY-MT: [${hostname}] 无专属配置 -> 使用通用文章适配器`);
        config = GENERIC_ARTICLE;
    }

    const isVerified = config.optimizationLevel === 'verified';
    console.log(`HY-MT: 加载 [${config.adapter}] for [${hostname}] (专属配置: ${isVerified})`);

    const init = () => {
        if (window.HY_MT_CORE && window[config.adapter]) {
            const adapter = new window[config.adapter](config);
            window.HY_MT_CORE.registerAdapter(adapter);
        } else {
            setTimeout(init, 100);
        }
    };
    init();
})();
