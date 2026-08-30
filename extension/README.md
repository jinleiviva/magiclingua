# MagicLingua Chrome 扩展

MagicLingua 的浏览器端：网页整页翻译、YouTube 双语字幕、PDF 助手入口。
安装步骤、服务搭建与使用说明见[根目录 README](../README.md)，本文档只描述扩展本身的架构与排障。

## 架构

```
extension/
├── manifest.json            # MV3 配置：权限、content scripts、popup
├── background.js            # Service Worker：翻译请求转发、缓存、Native Messaging（com.magiclingua.host）
├── popup.html / popup.js / popup.css   # 设置面板（站点能力卡 + 本地服务启停卡）
├── site_registry.js         # 站点能力注册表（哪些站点启用哪些适配器）
├── core/
│   ├── UniversalCore.js     # 通用翻译核心：通信 / 缓存 / 渲染调度
│   └── LocalBridge.js       # 把 popup 的 PDF 开关同步到 localhost:18770 页面
├── adapters/
│   ├── VideoAdapter.js      # 视频站点：YouTube 字幕轨道模式 + 通用 DOM 观察模式
│   └── TextFeedAdapter.js   # 信息流站点（Reddit / X / HN 等）
├── injected/
│   └── yt-player-bridge.js  # MAIN world 桥：读取 ytInitialPlayerResponse 字幕轨道
└── icons/                   # 16 / 48 / 128 PNG
```

翻译链路：content script（core + adapters）→ `background.js` → 本地服务 `http://localhost:18770`（OpenAI 兼容 API）。
服务启停：popup → `background.js` → Native Messaging 宿主（`native_host/hymt_host.py`）→ launchd / 直接派生。

## 关键机制

- **扩展 ID**：开发者模式加载时由 `extension/` 目录路径哈希决定。目录不动 ID 恒定；移动 / 重命名目录后重跑 `../native_host/install.command` 会自动探测新 ID 并入白名单。**不要**在 manifest 里加固定 `key`，会与路径哈希冲突导致扩展被反复禁用。
- **配置**：popup 写入 `chrome.storage.sync`，`background.js` 的 `DEFAULT_CONFIG` 是出厂默认。
- **字幕来源**：优先读 YouTube 播放器自带的字幕轨道（含自动生成），没有字幕轨道的视频无法翻译。

## 排障

```bash
# 本地服务是否正常
curl http://localhost:18770/health
```

- 翻译不显示：确认视频已开 CC 字幕；popup 里服务状态为「运行中」；F12 控制台看报错。
- 面板提示宿主错误（如 `Native host has exited`）：重跑 `../native_host/install.command` 后**完全退出浏览器再开**（浏览器只在启动时扫描宿主清单）；仍不行就跑 `../native_host/diagnose.command`。
- 翻译延迟高：关闭其他吃 CPU 的程序；或调 `../server_gguf.py` 的 `n_threads`。

## 支持语言

中、英、日、韩、法、德、西、俄、阿、葡、意、荷、波、土、越、泰、印尼、马来、印地、孟加拉等 33 种互译（与 HY-MT 模型能力一致）。

---

**Powered by HY-MT + Chrome Extensions API** 🚀
