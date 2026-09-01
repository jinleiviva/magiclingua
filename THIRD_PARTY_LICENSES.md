# 第三方软件许可证清单（Third-Party Licenses）

> 本项目自身代码以 **MIT License** 发布（见 `LICENSE`）。本文件列出运行 MagicLingua 所依赖的第三方开源组件及其许可证。各组件的许可证**独立于本项目 MIT 许可**，使用或分发时须同时遵守对应条款。
>
> ⚠️ **重点**：依赖中含 **AGPL-3.0** 强 copyleft 组件（PyMuPDF、BabelDOC），详见下文「AGPL-3.0 组件专项说明」。

## 依赖清单

| 组件 | 版本 | 许可证 | SPDX | 用途 | 来源 |
| --- | --- | --- | --- | --- | --- |
| PyMuPDF | 1.27.2.2 | GNU AGPL v3.0（Artifex 双重许可） | AGPL-3.0 | PDF 解析 / 目录提取 | https://pypi.org/project/PyMuPDF/ |
| BabelDOC | 0.6.4 | GNU AGPL v3.0 | AGPL-3.0 | PDF 版面解析与双语排版 | https://github.com/funstory-ai/BabelDOC |
| llama-cpp-python | 0.3.35 | MIT | MIT | 本地 GGUF 推理绑定 | https://pypi.org/project/llama-cpp-python/ |
| llama.cpp | （由 llama-cpp-python 绑定） | MIT | MIT | 推理引擎内核 | https://github.com/ggerganov/llama.cpp |
| Flask | 3.0.0 | BSD-3-Clause | BSD-3-Clause | 本地 HTTP 服务 | https://pypi.org/project/Flask/ |
| numpy | 2.3.5 | BSD-3-Clause | BSD-3-Clause | 数值计算 | https://pypi.org/project/numpy/ |
| opencv-python | 4.11.0.86 | Apache-2.0 | Apache-2.0 | 图像处理 | https://pypi.org/project/opencv-python/ |
| Pillow | 12.1.0 | MIT-CMU / HPND | MIT | 图像处理 | https://pypi.org/project/Pillow/ |
| huggingface_hub | 0.36.2 | Apache-2.0 | Apache-2.0 | 模型下载 | https://pypi.org/project/huggingface_hub/ |
| requests | 2.32.5 | Apache-2.0 | Apache-2.0 | HTTP 客户端 | https://pypi.org/project/requests/ |
| diskcache | （未锁定） | BSD-3-Clause | BSD-3-Clause | 翻译缓存 | https://pypi.org/project/diskcache/ |
| jinja2 | （未锁定） | BSD-3-Clause | BSD-3-Clause | 提示词模板 | https://pypi.org/project/Jinja2/ |
| typing-extensions | （未锁定） | PSF-2.0 | PSF-2.0 | 类型注解 | https://pypi.org/project/typing-extensions/ |

> 注：`requirements-core.txt` 中的 `numpy / diskcache / jinja2 / typing-extensions / huggingface_hub / requests` 当前未锁定版本（`requirements.txt` 与 `requirements-pdf.txt` 为锁定版本）。重建环境时可能解析到不同的次版本。

## AGPL-3.0 组件专项说明

**PyMuPDF** 与 **BabelDOC** 采用 **GNU Affero General Public License v3.0**（AGPL-3.0，全文：<https://www.gnu.org/licenses/agpl-3.0.html>）。AGPL 是强 copyleft 许可证，与本项目 MIT 许可**相互独立且约束更强**。关键义务：

- **§13 网络条款**：若你修改 AGPL 程序，并使其通过网络（如 Web API、SaaS）与用户交互，你必须向用户提供该修改版的完整对应源码。
- **§5 衍生 / 再分发**：以 AGPL 组件为基础的作品，其分发须整体遵守 AGPL-3.0（包括提供源码）。
- **商业替代**：PyMuPDF 由 Artifex 提供商业许可证（<https://artifex.com/licensing/pymupdf>），可替代 AGPL 义务；BabelDOC 的商业授权请向 funstory-ai 咨询。

**对本项目的影响**：

- ✅ **当前形态（MIT 全量开源 + 纯本地运行、未修改 AGPL 组件源码）**：不触发 AGPL 的额外义务。
- ❌ **若改为闭源分发 / 收费闭源版 / 云端翻译 API**：AGPL 会要求公开对应修改版源码，实质上无法闭源，除非为 PyMuPDF 取得 Artifex 商业许可、为 BabelDOC 取得商业授权。

## 宽松许可证汇总

MIT / BSD-3-Clause / Apache-2.0 / PSF-2.0 均为宽松许可证，允许与 MIT 代码一同分发、可用于闭源产品（Apache-2.0 含专利授权与通告保留要求）。其完整文本见各组件上游仓库或：

- MIT: <https://opensource.org/licenses/MIT>
- BSD-3-Clause: <https://opensource.org/licenses/BSD-3-Clause>
- Apache-2.0: <https://www.apache.org/licenses/LICENSE-2.0>
- PSF-2.0: <https://opensource.org/licenses/Python-2.0>

## 翻译模型许可证

翻译模型（HY-MT / Hy-MT 系列 GGUF）受 **Tencent HY Community License Agreement** 约束，独立于本文件所列软件许可证。条款（含地域限制、月活授权门槛、禁止改进其他模型、商标披露等）见 `MODEL_LICENSE.txt` 与 `NOTICE`。

## 重要说明

MIT 与 AGPL 相互兼容（AGPL 约束更强）。你可以将自有代码以 MIT 发布，同时包含 AGPL-3.0 依赖；但**包含 AGPL 组件的分发包，其接收方对该分发包整体享有 AGPL-3.0 项下权利**——即不能将整个项目视为「可随意闭源」的 MIT 软件。务必向接收方明示本清单。
