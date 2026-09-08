# MagicLingua 代码审阅 · 改进清单

> 审阅范围：本地翻译运行时 `server_gguf.py`、Chrome 扩展（`background.js` / `core` / `adapters` / `selection_bubble` / `hover_translate` / `popup` / `site_registry` / `glossaries`）、`manifest.json`、依赖与构建脚本。
> 结论先行：**整体质量高**——安全边界（127.0.0.1 绑定 + Origin 白名单 + 无 CORS）、译文注入全程 `textContent`（无 XSS）、缓存/并发/空闲退出/OCR/PDF 裁剪/EPUB 重打包都做得到位。下面只列**值得改进**的点，按优先级排。

> **修复进度（2026-09-08）**：🔴 1–4、🟡 5–10 已全部落地（见 `server_gguf.py` 与新增 `templates/pdf.html`、`tests/test_core.py`）。其中 🟡 7 仅完成低风险部分——PDF 页 HTML 已抽到 `templates/pdf.html`；`server_gguf.py` 按模块进一步拆分（api/pdf/doc/core）属于更大的重构，已留作后续，未做以避免引入回归。

---

## 🔴 建议优先修（开源打磨前的安全/正确性）

### 1. 上传文件名路径穿越（PATH TRAVERSAL）✅ 已修复
- **证据**：`server_gguf.py`
  - `/v1/pdf/toc`：`path = os.path.join(udir, uploaded.filename)`（约 L1430）
  - `/v1/pdf/translate`：`input_path = os.path.join(workdir, uploaded.filename)`（约 L1549）
  - `/v1/doc/translate`：`input_path = os.path.join(workdir, uploaded.filename)`（约 L2088）
  - `uploaded.filename` 来自客户端 multipart，**未经 `basename`/`secure_filename` 清洗**。
- **影响**：攻击者在 `filename` 里塞 `../../../` 即可跳出 `pdf_jobs/_uploads/<uuid>` 工作目录写到项目外任意路径（受当前用户权限限制，但本机即你的账户）。配合 `_uploads_sweep` 的 `shutil.rmtree(os.path.dirname(path))` 还会放大破坏面。
- **修复**：统一用 `werkzeug.utils.secure_filename(uploaded.filename)`（Flask 已依赖 werkzeug），或 `os.path.basename(...)` + 扩展名白名单校验。

### 2. 上传文件大小无限制 ✅ 已修复
- **证据**：全文件未见 `app.config['MAX_CONTENT_LENGTH']` 配置；`/v1/pdf/toc`、`/v1/pdf/translate`、`/v1/doc/translate` 三个端点直接 `uploaded.save(...)`。
- **影响**：恶意/超大文件可占满磁盘与内存（Flask 默认无上限）。
- **修复**：`app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024`，并在 `save` 前对 `Content-Length` 把关。

### 3. PDF 端点未校验内容类型 ✅ 已修复
- **证据**：`/v1/pdf/toc` 仅 `if not uploaded.filename: return 400`（约 L1419），不校验扩展名或 `%PDF` 魔数。
- **影响**：传非 PDF 文件会一路走到 `pymupdf.open` / `extract_toc` 才报错，返回 500，体验差且暴露内部栈。
- **修复**：前置 `ext != '.pdf'` 直接 400；`/v1/pdf/toc` 可顺带读前 5 字节校验 `%PDF`。

### 4. `chat/completions` 的 `max_tokens`/`temperature` 来自客户端且未钳制 ✅ 已修复
- **证据**：`server_gguf.py` L1035 `max_tokens=data.get("max_tokens", 2048)`，未校验上限。
- **影响**：第三方调用可传极大 `max_tokens` 占满上下文/内存（本地串行锁下至少拖慢自身）。
- **修复**：服务端 clamp 到 `[1, 8192]`（与 `n_ctx=8192` 对齐）。

---

## 🟡 建议做（一致性 / 可维护性 / 资源）

### 5. 批量端点与实现/文档表述不一致 ✅ 已修复
- **证据**：`server_gguf.py` L775-777 注释写「批量翻译：整页翻译的提速来源」；但 `background.js` 的整页/划词翻译实际调**单条** `/v1/translate`（`callTranslateService` 约 L493），且实测本地批量推理不提速（记忆实证：本地批量比逐条慢 15–24%）。当前 `/v1/translate/batch` **没有任何客户端调用**，仅暴露给第三方 SDK。
- **影响**：注释/README 会让人误以为本地整页翻译在走批量提速。
- **修复**：改注释为「batch 端点仅面向云端/第三方 OpenAI SDK 调用；本地客户端因单模型串行、批量不提速，走单条」。若未来客户端要用 batch，需重新评估。

### 6. 依赖版本漂移（三份 requirements）✅ 已修复
- **证据**：`requirements.txt` numpy==2.3.5（含 babeldoc/PyMuPDF/opencv）；`requirements-core.txt` numpy==2.2.6（无 PDF 组件）；`requirements-pdf.txt` 又单独列一份。
- **影响**：多份独立维护易漂移，重建 venv 时 core 与 full 可能拉到不一致大版本。
- **修复**：以 `requirements-core.txt` 为基线单一事实来源，pdf 在其上 `pip install -r requirements-core.txt` 再叠加 babeldoc/PyMuPDF/opencv，或用生成脚本产出。

### 7. 服务端单体文件过大 + PDF 页面内联（部分修复：HTML 已抽离）
- **证据**：`server_gguf.py` 单文件约 2900 行；整页 PDF Web 界面（`PDF_PAGE` 模板，约 L2144-2600+，~600 行 HTML/CSS/JS）直接内联在 Python 字符串里。
- **影响**：难维护、diff 难读、改样式要动 Python。
- **修复**：把 PDF 页拆成 `templates/pdf.html` 静态文件，路由用 `send_file`/`render_template_string` 加载；按 api / pdf / doc / core 拆分模块。

### 8. 核心纯函数缺自动化测试 ✅ 已修复
- **证据**：仅 `test_api.py` / `test_streaming.py` 冒烟；README 要求 PR 过 `python -m py_compile`（仅语法）。`build_translate_prompt`、`looks_like_echo`、`parse_numbered_output`、`is_same_language`、`_parse_pages` 都是不依赖 Flask 的纯逻辑，却无单测。
- **影响**：prompt 构造/回显检测规则一改就无回归保护，正是项目最易藏 bug 的地方。
- **修复**：加 `pytest` 覆盖上述纯函数（它们已无 Flask 依赖，极易测）。

### 9. `n_threads=4` 硬编码 ✅ 已修复
- **证据**：`server_gguf.py` L357 `n_threads=4`。
- **影响**：纯 CPU（无 Metal/GPU）机器只跑 4 线程，未充分利用多核；Metal 路径下该值影响很小。
- **修复**：CPU 路径按 `min(os.cpu_count() or 4, 8)` 自适应（GPU offload 时保持 4 即可）。

### 10. 历史任务无自动清理策略 ✅ 已修复
- **证据**：`PDF_JOBS`/`DOC_JOBS` 持久化到 `pdf_jobs/jobs.json` 与工作目录 `<uuid>`，仅 `DELETE /v1/pdf/jobs/<id>` 手动清理；无 TTL/容量上限。
- **影响**：长期积累占磁盘（尤其大 PDF 产物）。
- **修复**：加 TTL（如 7 天）自动清理 + 总容量上限，或提供「清空历史」按钮。

---

## 🟢 可优化（体验 / 工程细节）

### 11. 整页翻译 `MAX_PAGE_NODES=400` 硬上限
- **证据**：`UniversalCore.js` L71。超长页面只翻视口附近前 400 段，其余不翻且无滚动增量翻译。
- **建议**：监听滚动，对视口附近未翻段落做增量翻译（现有视口排序逻辑可复用）。

### 12. `pdf_dpi` 等配置键未在 `DEFAULT_CONFIG` 声明
- **证据**：`run_babeldoc` 用 `user_config.get("pdf_dpi", 200)`（约 L1208），但 `DEFAULT_CONFIG`（L378-398）无该键。
- **影响**：功能正常（默认 200），但缺省未文档化、不会被写入 `config.json`。
- **修复**：补到 `DEFAULT_CONFIG` 或在 README「给开发者」列出。

### 13. 实验站点选择器未实测
- **证据**：`site_registry.js` 中 netflix / coursera / udemy 标注 `optimizationLevel: 'experimental'`，注释明说「登录墙未实测、选择器来自社区资料」。
- **建议**：README/UI 对这几站明确标注「实验支持」，降低用户预期。

### 14. `manifest.json` 的 `<all_urls>` 权限较宽
- **证据**：`host_permissions` 与 content_scripts 均含 `<all_urls>`（功能必需：任意站整页翻译）。
- **建议**：对外分发（Chrome 商店）时强化声明「内容脚本只与本机 `localhost:18770` 通信、不上传任何内容」（README 已有，可在商店说明里更醒目）。

---

## ✅ 已验证良好（确认，不必改）
- **绑定安全**：`app.run(host="127.0.0.1", ...)`（L2864），非 `0.0.0.0`；`_origin_guard` 白名单拦截跨源；刻意不使用 `flask_cors`。
- **无 XSS**：所有译文注入（`UniversalCore`/`VideoAdapter`/`TextFeedAdapter`/`selection_bubble`/`hover_translate`）均用 `textContent` 或 `createElementNS`，无 `innerHTML` 拼接用户文本；Google News 等严格 CSP/Trusted Types 页面也用 DOM 方法规避。
- **缓存**：`background.js` 内存 L1 + `chrome.storage.local` L2 持久化，命中逻辑与失效（上下文/词库变化）正确。
- **语言表三端对齐**：popup 下拉 38 种、`background.LANG_CODE_MAP` 38 种、`server_gguf.LANG_NAMES` 全量，一致。
- **健壮性细节**：空闲看门狗不在「常驻」线程退出、切换策略重置时间戳；PDF 先裁选中页再送 BabelDOC（省 OCR/版面解析）；EPUB 重打包 `mimetype` 首项不压缩；回显检测 + 低温重试 + 解析失败降级单条重翻。
