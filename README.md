<div align="center">

# 🚂 MagicLingua

**一个完全跑在你电脑上的翻译助手** — 翻网页 · 翻视频字幕 · 翻 PDF / EPUB / TXT 文档

不用账号 · 不上传内容 · 永久免费 · 开源

[![version](https://img.shields.io/badge/version-2.0.0-blue)](https://github.com/jinleiviva/magiclingua/releases)
[![license](https://img.shields.io/badge/code%20license-MIT-green)](LICENSE)
[![platform](https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey)](README.md)
[![price](https://img.shields.io/badge/%E4%BB%B7%E6%A0%BC-%E6%B0%B8%E4%B9%85%E5%85%8D%E8%B4%B9-brightgreen)](LICENSE)

</div>

> ⚠️ **使用地区**：翻译模型采用腾讯 HY 社区许可协议，**不适用于欧盟、英国、韩国**，请勿在上述地区使用或分发。许可全文见 [`MODEL_LICENSE.txt`](MODEL_LICENSE.txt)。

---

## 目录

- [这是什么](#这是什么)
- [它能做什么](#它能做什么)
- [效果一览](#效果一览)
- [怎么开始用](#怎么开始用)
- [日常怎么用](#日常怎么用)
- [常见问题](#常见问题)
- [给开发者](#给开发者)
- [参与贡献](#参与贡献)
- [致谢与许可证](#致谢与许可证)

---

## English Summary

MagicLingua is a browser translation assistant that runs **entirely on your own machine**: full-page web translation, YouTube bilingual subtitles, and PDF / EPUB / TXT document translation. No account, no cloud, no cost — your content never leaves your computer. Free and open source (MIT) forever.

Quick start: download the repo and run `./install.command` (one-click: venv + engine + model from ModelScope + auto-launch), or run `./setup_env.sh` / `./download_model.sh` / `./start_server_gguf.sh` manually — then load the `extension/` folder via `chrome://extensions` (Developer Mode). The Chinese docs below cover everything else; English issues and PRs are welcome.

> The bundled translation model is licensed under the *Tencent HY Community License Agreement*, which **does not apply to the EU, UK, or South Korea** — see [`MODEL_LICENSE.txt`](MODEL_LICENSE.txt).

---

## 这是什么

MagicLingua 是一个浏览器翻译插件，帮你轻松看懂英文网页、视频和文档。

和常见的在线翻译不一样，它**完全运行在你自己的电脑上**：

- 🔒 **隐私安全** — 翻译在本地完成，你浏览的内容、上传的文档**不会离开你的电脑**
- 💰 **永久免费** — 不注册、不订阅、无广告，开源可自用
- 🌍 **33 种语言互译** — 中 / 英 / 日 / 韩 / 法 / 德 / 西 / 俄 / 阿 / 葡 等
- 💻 **普通电脑就能跑** — 翻译模型约 1.1 GB，近五年内的电脑（有没有独立显卡都行）都能跑

**我为什么做它**：我每天要读大量英文内容，后来发现商用的翻译效果已经能在自己电脑上免费跑出来，就想——既然我自己能用上，为什么不把它开源给更多人？于是有了这个插件。我一个人能覆盖的场景有限，欢迎一起来完善它。

---

## 它能做什么

| 你想做的事 | 怎么办 |
| --- | --- |
| 看英文网页 | 打开网页 → 点插件图标 → 打开「启用翻译」。整页内容就地变成中文，排版不乱；再点一下开关就恢复原文 |
| 读一段英文 | 选中文字，点旁边的「译」小按钮 / 右键菜单 / 按 `Alt+T`，译文马上显示 |
| 看 YouTube 视频 | 播放器里打开字幕（CC），插件自动出中文，原字幕和译文同屏对照 |
| 看 B 站 / Netflix 等视频 | 打开视频原字幕，插件自动跟着翻译（这几个站点属实验支持） |
| 翻译 PDF 文章 | 浏览器打开 `http://localhost:18770/pdf`，上传 PDF，勾选想看的文章，翻译后下载成品，排版和图片原样保留 |
| 翻译电子书 / 文稿 / 字幕文件 | 同一个页面，选类型（EPUB / TXT / SRT / ASS），拖入文件，翻译完下载，默认双语对照 |
| 让专业术语翻得准 | 内置金融 / 科技 / 商业词库（勾选即生效），也可以自己添加术语，全文译法保持一致 |
| 调整译文样子 | 设置里改字幕底色和文字颜色，有 4 组现成配色一键切换 |

---

## 效果一览

<table>
  <tr>
    <td width="50%" align="center">
      <a href="docs/screenshot-web.png">
        <img src="docs/screenshot-web.png" width="340" alt="网页整页翻译">
      </a>
      <br>
      <sub>🌍 网页整页翻译 · Google News 就地替换，版式不乱</sub>
    </td>
    <td width="50%" align="center">
      <a href="docs/screenshot-youtube.jpg">
        <img src="docs/screenshot-youtube.jpg" width="340" alt="YouTube 双语字幕">
      </a>
      <br>
      <sub>🎬 YouTube 双语字幕 · 原字幕与译文同屏对照</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <a href="docs/screenshot-pdf.png">
        <img src="docs/screenshot-pdf.png" width="300" alt="PDF 翻译助手">
      </a>
      <br>
      <sub>📄 PDF 翻译助手 · 自动解析目录，勾选要翻的文章</sub>
    </td>
    <td width="50%" align="center">
      <a href="docs/screenshot-pdf-result.png">
        <img src="docs/screenshot-pdf-result.png" width="300" alt="PDF 翻译成品">
      </a>
      <br>
      <sub>📑 PDF 翻译成品 · 排版、配图、分栏原样保留</sub>
    </td>
  </tr>
</table>

<p align="center">
  <sub>⚙️ 插件面板：启用翻译开关 + 本地服务一键启停（点图标看大图）</sub>
</p>
<p align="center">
  <a href="docs/screenshot-popup.png">
    <img src="docs/screenshot-popup.png" width="220" alt="插件面板">
  </a>
</p>

---

## 怎么开始用

一共两步：**装本地翻译服务（一键）→ 装扩展**。

> 需要：**macOS（Apple Silicon）**、内存 4 GB+、磁盘约 3 GB。不需要 GPU。
> Windows / Linux 见文末「给开发者」；Intel Mac 会自动回退源码编译（耗时较长）。

### 第 1 步：装本地翻译服务（一次，约几分钟）

**macOS 用户推荐双击**（终端都省了）：

```
下载仓库 → 双击 install.command
```

或者打开终端（更稳妥，能看到进度）：

```bash
git clone https://github.com/jinleiviva/magiclingua.git && cd magiclingua
./install.command
```

脚本会**自动完成全部安装**：

1. 创建 Python 虚拟环境
2. 安装翻译引擎——Apple Silicon 自动用官方预编译 wheel（**免源码编译**，老的 7 分钟编译已不需要）
3. 从**魔搭社区**下载翻译模型（约 1.1 GB，国内直连快，带进度条，断点续传）
4. 注册开机自启（launchd）+ 浏览器原生宿主（装完扩展就能一键启停服务）

看到 `MagicLingua 安装完成` 就是装好了。**装完先完全退出 Chrome（Cmd+Q）再重开**。

> 手动分步装（不用一键脚本）：
> ```bash
> ./setup_env.sh          # 环境 + 引擎 + 依赖
> ./download_model.sh     # 下载模型（约 1.1 GB）
> ./start_server_gguf.sh  # 启动服务
> ```

### 第 2 步：装浏览器扩展（一次）

1. Chrome 打开 `chrome://extensions`
2. 打开右上角的「**开发者模式**」
3. 点「**加载已解压的扩展程序**」，选择仓库里的 `extension` 文件夹

装好后，浏览器右上角会出现插件图标。点开确认能看到面板，就装好了。

> 第 1 步已注册原生宿主，所以装完扩展**重启 Chrome** 后，点插件里的「启动 / 停止」就能控制服务，不用再碰终端。

<details>
<summary>更多说明（Windows / Linux、扩展 ID 变了怎么办）</summary>

- **Windows / Linux 一键启停**：目前没有安装脚本，需要手动注册（详见 `native_host/com.magiclingua.host.json` 里的说明），不注册不影响翻译功能本身。
- **扩展 ID 变了**：扩展 ID 由 `extension` 文件夹的位置决定，只要不移动 / 改名就不会变。万一变了，重跑一次 `./native_host/install.command` 即可自动适配。
- **服务端口被占用**：默认端口 18770。被占时用 `lsof -i :18770` 找到占用进程结束它，或设置环境变量 `HYMT_PORT` 换端口。

</details>

---

## 日常怎么用

### 翻网页

点插件图标 → 打开「**启用翻译**」。当前网页马上开始翻译：每个译文出现前先显示一个转圈提示，翻译好了就原地替换成中文。想恢复原文，再点一下开关，所有译文消失。

想看下一个网页？直接打开就行，服务会继续工作，不需要重新设置。翻过一次的网页再次打开，译文是秒出的（本地缓存）。

### 翻一段文字

- **划词**：选中一段英文，点选区旁出现的蓝色「译」按钮，或右键菜单选「翻译选中内容」，或按快捷键 `Alt+T`（快捷键可在浏览器扩展快捷键设置里改）
- **悬停**：按住 `Ctrl` 把鼠标停在段落上，译文就地出现；移开鼠标或松开按键，译文消失（不喜欢 Ctrl，可在设置里换成 `Alt`）

### 看视频

- **YouTube**：播放器里打开字幕（CC），插件会自动提前翻好，原字幕和译文同屏对照
- **B 站 / Netflix / Coursera / Udemy**（实验支持）：同样打开视频原字幕，字幕一变译文就跟着变
- **导出字幕**：在视频页点插件里的「导出字幕」，得到一个 `.srt` 字幕文件，可以直接拖进剪映等软件

### 翻文档和电子书

浏览器打开 **`http://localhost:18770/pdf`**（服务运行时才能访问）：

- **PDF**：上传后自动列出文章目录 → 勾选想翻的文章 → 开始翻译 → 下载成品。只翻你勾选的页，扫描版 PDF 也能翻（自动识别文字）。如果目录识别不准，可以手动填页码（如 `1,3-5`）
- **EPUB / TXT / 字幕文件**：选好文件类型拖进去 → 翻译完直接下载。默认是**双语对照**（原文 + 译文），也可以切换成只输出译文；长文档会自动分段翻译，个别段落失败不影响整篇

### 设置里能调什么

点插件图标 → 右上角 **⚙️**：

| 设置项 | 作用 |
| --- | --- |
| 译文样式 | 字幕底色 / 文字颜色，4 组现成配色，网页译文颜色跟随 |
| 术语表 | 勾选内置词库（金融 / 科技 / 商业），或添加自己的术语；支持从表格文件（CSV）批量导入导出 |
| 悬停触发键 | `Ctrl` 或 `Alt` 二选一 |
| 目标语言 / 显示模式 | 翻成什么语言、双语还是仅译文 |

---

## 常见问题

- **服务起不来？** 依次检查：模型下了没（`ls models/*.gguf`）、依赖装了没（`venv/bin/python -m pip list | grep llama-cpp`）、端口被占没（`lsof -i :18770`），然后看日志 `tail -f log_server.txt` 找原因。
- **提示找不到模型？** 跑一次 `./download_model.sh`；或自己指定路径：`export HYMT_MODEL_PATH=/你的/模型路径.gguf`。
- **翻译速度慢？** 首次翻译要加载模型会慢一点（10–20 秒），之后就快了；翻译过的内容有缓存，再翻是秒出。换更大或更小的模型可以调节质量与速度（见「给开发者」）。
- **我的电脑能跑吗？** 近五年的 Mac / Windows / Linux 都可以。有 NVIDIA 显卡更快，没有显卡纯 CPU 也能跑。手机和纯浏览器环境不支持。
- **翻译质量一般？** 1.8B 模型体积小、速度快，质量已接近商用服务；想要更好效果，可以换更大的模型（7B），速度会相应变慢。
- **为什么不支持某网站？** 网页翻译是通用的，绝大多数网站开「启用翻译」即可；少数特殊站点（视频站、特殊排版）需要针对性适配，欢迎反馈你常逛的站点。

---

## 给开发者

<details>
<summary>环境变量与模型配置</summary>

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `HYMT_PORT` | `18770` | 服务端口 |
| `HYMT_IDLE_EXIT` | `20` | 空闲多少分钟自动退出，`0` = 常驻 |
| `HYMT_MODEL_PATH` | 空 | 直接指定模型文件路径（优先于 `models/`） |
| `MODEL_NAMESPACE` / `MODEL_REPO` / `MODEL_FILE` | Tencent-Hunyuan / Hy-MT2-1.8B-GGUF / Q4_K_M | `download_model.sh` 的下载目标（默认魔搭社区） |
| `HF_ENDPOINT` | 空 | 设置后切换下载源到 HuggingFace / 镜像（如 `https://huggingface.co`） |

换更大模型（效果更好、速度更慢）——默认走魔搭社区：

```bash
MODEL_NAMESPACE=Tencent-Hunyuan MODEL_REPO=Hy-MT2-7B-GGUF MODEL_FILE=Hy-MT2-7B-Q4_K_M.gguf ./download_model.sh
```

切回 HuggingFace 官方源：

```bash
HF_ENDPOINT=https://huggingface.co MODEL_NAMESPACE=tencent MODEL_REPO=Hy-MT2-7B-GGUF MODEL_FILE=Hy-MT2-7B-Q4_K_M.gguf ./download_model.sh
```

Apple Silicon 加速：仓库自带的依赖安装已启用 Metal 加速；自行编译时设 `CMAKE_ARGS="-DGGML_METAL=ON"`。

</details>

<details>
<summary>项目结构</summary>

```
magiclingua/
├── install.command         # macOS 双击一键安装（普通用户入口）⭐
├── setup_env.sh            # 一键安装脚本（venv + 引擎 + 模型 + 自启注册）
├── download_model.sh       # 下载模型到 models/（默认魔搭社区，断点续传）
├── requirements-core.txt   # 翻译核心依赖（不含 PDF 解析组件）
├── requirements-pdf.txt    # PDF 翻译依赖（--with-pdf 按需安装）
├── start_server_gguf.sh    # 手动启动本地翻译服务（开发 / 调试）
├── server_gguf.py          # 服务主程序（OpenAI 兼容 API + PDF/文档翻译）⭐
├── ocr_engine.py           # 扫描件 OCR（macOS Vision）
├── pdf_toc.py              # PDF 目录解析
├── requirements.txt        # 完整运行依赖（含 PDF，锁版本）
├── config.example.json     # 配置样例（首次运行自动生成 config.json）
├── test_api.py             # API 冒烟测试
├── test_streaming.py       # 流式输出冒烟测试
├── pack_extension.py       # 打包 CRX（发布用）
├── ui-preview.html         # popup 样式预览页（开发用）
├── icons_src/              # 图标源素材
├── extension/              # Chrome 扩展（开发者模式加载）
├── native_host/            # 扩展 ↔ 本机服务的 Native Messaging 通道
├── LICENSE                 # 本项目代码许可证（MIT）
├── MODEL_LICENSE.txt       # 翻译模型许可证（Tencent HY Community License）
└── NOTICE                  # 许可与商标披露汇总
```

</details>

<details>
<summary>技术说明</summary>

- 扩展分三层：站点配置（`site_registry.js`）→ 适配器（网页 / 视频）→ 通用整页翻译兜底；划词翻译独立实现。
- 本地服务提供 OpenAI 兼容 API（`http://localhost:18770/v1/chat/completions`），可接入其他工具。
- 服务空闲 20 分钟自动退出释放内存，下次使用时由扩展一键拉起。
- 扩展 / 文档翻译的详细设计见 [`docs/`](docs/)（架构规划、竞品对比、功能方案）。

</details>

---

## 参与贡献

这个项目是我一个人业余时间做的，**一个人的场景有限，一群人的场景才是全部**。特别欢迎这些方向的贡献：

- 🌐 **更多站点适配** — 让你常逛的站点也能翻
- 📄 **PDF 场景打磨** — 更多版式（双栏论文 / 漫画 / 财报）的翻译效果优化
- 🎬 **更多视频平台** — B 站、Coursera、Netflix 等字幕翻译适配
- 🖥️ **平台支持** — Windows / Linux 的一键安装脚本（目前仅 macOS 自动化）
- 🐛 **问题反馈** — 翻错了、版式乱了、服务起不来，都请来提 Issue

提交 PR 前请确保 `server_gguf.py` 可通过 `python -m py_compile`、扩展改动在 `chrome://extensions` 实测可用。不会写代码也没关系——把你想让它支持的场景告诉我，同样是宝贵的贡献。

---

## 致谢与许可证

- [Tencent Hunyuan HY-MT](https://github.com/Tencent-Hunyuan/HY-MT) · [Hy-MT2 GGUF](https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF) — 翻译模型
- [llama.cpp](https://github.com/ggerganov/llama.cpp) / llama-cpp-python — 本地推理引擎
- [BabelDOC](https://github.com/funstory-ai/BabelDOC) — PDF 版面解析与双语排版
- [PyMuPDF](https://pymupdf.readthedocs.io/) — PDF 处理；[Flask](https://flask.palletsprojects.com/) — 本地服务

- **本项目代码**：[MIT License](LICENSE)
- **翻译模型**：[Tencent HY Community License Agreement](MODEL_LICENSE.txt) — 商业可用；月活超 1 亿需向腾讯单独申请授权；**不适用于欧盟 / 英国 / 韩国**；再分发须附协议副本与 `NOTICE`；禁止用于改进其他 AI 模型、军事或高风险自动决策等
- **声明**：MagicLingua 与腾讯及 Hunyuan 团队不存在从属、授权或背书关系

---

<div align="center">

**Powered by Tencent Hunyuan HY-MT + llama.cpp** 🚀

<sub>Made for personal use · Given to everyone · 永久免费开源</sub>

</div>
