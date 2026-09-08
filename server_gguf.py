#!/usr/bin/env python3
"""
统一翻译服务 (Unified Translation Service)

一个进程同时服务三个场景：
  1. 浏览器看新闻  -> POST /v1/translate      段落级翻译
  2. YouTube 字幕  -> POST /v1/translate      低延迟 + 流式
  3. PDF 翻译排版  -> POST /v1/pdf/translate  走 BabelDOC 版面还原

模型: Hy-MT2-1.8B GGUF (Q4_K_M) + llama.cpp Metal 加速
"""

import hashlib
import io
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile

from html.parser import HTMLParser

from flask import Flask, Response, jsonify, request, send_file
from llama_cpp import Llama
from werkzeug.utils import secure_filename

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 安全 / 资源上限常量（必须在 app 创建前定义，app.config 会引用）
# --------------------------------------------------------------------------
MAX_UPLOAD_BYTES = 200 * 1024 * 1024   # 上传文件硬上限 200MB，防超大文件占满磁盘/内存
MAX_CHAT_TOKENS = 8192                  # chat/completions 的 max_tokens 上限，钳制第三方极大值
JOBS_TTL = 24 * 3600                    # 历史翻译任务（PDF / 文档）保留 24 小时，过期自动清理

app = Flask(__name__)
# 刻意不使用 flask_cors（默认放行所有来源）：任何网页的 JS 都能直接 POST
# 本地端口——白嫖推理打满 CPU、调 /shutdown 关服务、传 PDF 占磁盘。
# 来源白名单见下方 _origin_guard。

# 上传文件硬上限：超限直接 413，不让超大请求读进内存/落盘占满磁盘。
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


@app.errorhandler(413)
def _request_too_large(_err):
    return jsonify({"error": f"上传文件过大（上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB）"}), 413

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
PDF_JOBS = {}
PDF_JOBS_FILE = os.path.join(BASE_DIR, "pdf_jobs", "jobs.json")

# 轻量文档翻译任务（TXT / SRT / ASS / EPUB，标准库实现，不走 BabelDOC）
DOC_JOBS = {}
DOC_JOBS_DIR = os.path.join(BASE_DIR, "doc_jobs")

# --------------------------------------------------------------------------
# PDF 上传暂存 + 目录缓存
# --------------------------------------------------------------------------
# 目录提取必须和"开始翻译"拆成两次请求：用户要先看到文章清单才能勾选。
# 所以文件先落到磁盘，用 upload_id 把两次请求串起来。
PDF_UPLOADS = {}
UPLOAD_DIR = os.path.join(BASE_DIR, "pdf_jobs", "_uploads")
UPLOADS_FILE = os.path.join(UPLOAD_DIR, "uploads.json")
TOC_CACHE = {}  # sha256 -> 目录结果，同一个文件重复上传直接命中，不用重解析
UPLOAD_TTL = 24 * 3600  # 上传件保留 24 小时


def _safe_upload_name(filename):
    """剥离路径分隔符与上级目录跳转，只保留基础文件名，杜绝路径穿越写盘。

    攻击者在 filename 里塞 `../../../etc/passwd` 时，secure_filename 会把
    `../` 变成 `_.._`、再 basename 兜底，最终只得到 `passwd` 这类纯文件名。
    """
    name = secure_filename(filename or "")
    name = os.path.basename(name)  # 再保险一层：强制取最后一段
    if not name:
        name = f"upload_{uuid.uuid4().hex[:8]}"
    return name


def _is_pdf_file(path):
    """校验文件确为 PDF：扩展名 + 头部 %PDF 魔数，避免 pymupdf.open 时爆 500。"""
    if not str(path).lower().endswith(".pdf"):
        return False
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except Exception:
        return False


def _clamp_max_tokens(v, default=2048):
    """把客户端传入的 max_tokens 钳制到 [1, MAX_CHAT_TOKENS]，防占满上下文。"""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return default
    return max(1, min(v, MAX_CHAT_TOKENS))


def _load_pdf_page():
    """PDF 翻译页 HTML 抽离到 templates/pdf.html，改样式不必动 Python。"""
    p = os.path.join(BASE_DIR, "templates", "pdf.html")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        logger.error(f"加载 PDF 页面模板失败: {e}")
        return "<h1>PDF 页面模板缺失</h1>"


def _sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _uploads_save():
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        with open(UPLOADS_FILE, "w") as f:
            json.dump(PDF_UPLOADS, f, ensure_ascii=False, indent=1)
    except Exception as e:
        logger.warning(f"保存上传记录失败: {e}")


def _uploads_load():
    global PDF_UPLOADS
    PDF_UPLOADS = {}
    try:
        with open(UPLOADS_FILE) as f:
            for k, v in json.load(f).items():
                if os.path.exists(v.get("path", "")):
                    PDF_UPLOADS[k] = v
    except Exception:
        pass


def _uploads_sweep():
    """清掉过期上传件，避免用户传完关掉页面留下垃圾。"""
    try:
        now = time.time()
        for k, v in list(PDF_UPLOADS.items()):
            if now - v.get("created_at", 0) > UPLOAD_TTL:
                shutil.rmtree(os.path.dirname(v.get("path", "")), ignore_errors=True)
                PDF_UPLOADS.pop(k, None)
        # 记录里没有、目录里却存在的孤儿目录也一并清掉
        if os.path.isdir(UPLOAD_DIR):
            known = {os.path.basename(os.path.dirname(v["path"]))
                     for v in PDF_UPLOADS.values() if v.get("path")}
            for name in os.listdir(UPLOAD_DIR):
                p = os.path.join(UPLOAD_DIR, name)
                if os.path.isdir(p) and name not in known:
                    shutil.rmtree(p, ignore_errors=True)
        _uploads_save()
    except Exception as e:
        logger.warning(f"清理上传件失败: {e}")


def _jobs_sweep():
    """清理过期的历史翻译任务（PDF / 文档），避免工作目录无限堆积占满磁盘。

    只清 completed/failed 且超过 JOBS_TTL 的任务；正在 processing 的任务不碰，
    以免误删进行中的翻译。过期但卡在 pending 的也一并回收（视为异常残留）。
    """
    now = time.time()

    def _sweep(store, base_dir):
        removed = 0
        for k, v in list(store.items()):
            age = now - v.get("created_at", 0)
            if age <= JOBS_TTL:
                continue
            status = v.get("status")
            if status == "processing":
                continue  # 进行中不删
            wd = v.get("workdir") or (os.path.join(base_dir, k) if base_dir else None)
            if wd and os.path.isdir(wd):
                shutil.rmtree(wd, ignore_errors=True)
            store.pop(k, None)
            removed += 1
        return removed

    try:
        n1 = _sweep(PDF_JOBS, os.path.join(BASE_DIR, "pdf_jobs"))
        n2 = _sweep(DOC_JOBS, DOC_JOBS_DIR)
        if n1 or n2:
            logger.info(f"清理历史任务: PDF {n1} 个 / 文档 {n2} 个")
            _pdf_jobs_save()
    except Exception as e:
        logger.warning(f"清理历史任务失败: {e}")


def _jobs_sweep_loop():
    """每小时跑一次任务清理的后台线程。"""
    while True:
        time.sleep(3600)
        try:
            _jobs_sweep()
        except Exception:
            pass


def _pdf_jobs_save():
    """任务记录持久化到 pdf_jobs/jobs.json，服务重启后历史不丢。"""
    try:
        os.makedirs(os.path.dirname(PDF_JOBS_FILE), exist_ok=True)
        with open(PDF_JOBS_FILE, "w") as f:
            json.dump(PDF_JOBS, f, ensure_ascii=False, indent=1)
    except Exception as e:
        logger.warning(f"保存任务记录失败: {e}")


def _pdf_jobs_load():
    global PDF_JOBS
    PDF_JOBS = {}
    try:
        with open(PDF_JOBS_FILE) as f:
            loaded = json.load(f)
        # 只恢复 pending/processing/completed；completed 必须真有译文产物
        def _has_output(v):
            wd = v.get("workdir")
            if not wd or not os.path.isdir(wd):
                return False
            return any(
                f.endswith((".mono.pdf", ".dual.pdf")) and "watermarked" not in f
                for f in os.listdir(wd)
            )

        kept = {}
        for k, v in loaded.items():
            if v.get("status") == "failed":
                continue
            if v.get("status") == "completed" and not _has_output(v):
                continue
            kept[k] = v
        PDF_JOBS = kept
    except Exception:
        pass

    # 扫描工作目录：把磁盘上有产物但没记录的任务补回来
    # （旧版本没有持久化，或记录被删而目录残留）
    base = os.path.join(BASE_DIR, "pdf_jobs")
    try:
        for name in os.listdir(base):
            d = os.path.join(base, name)
            if not os.path.isdir(d) or name in PDF_JOBS or name == "smoke":
                continue
            pdfs = [f for f in os.listdir(d) if f.endswith(".pdf")]
            mono = [f for f in pdfs if ".mono.pdf" in f and "watermarked" not in f]
            dual = [f for f in pdfs if ".dual.pdf" in f and "watermarked" not in f]
            # 没有翻译产物（只有上传的原 PDF）的任务不值得恢复
            if not mono and not dual:
                continue
            result = os.path.join(d, mono[0]) if mono else os.path.join(d, dual[0])
            stem = mono[0] if mono else dual[0]
            for suffix in (".no_watermark.zh.mono.pdf", ".no_watermark.zh.dual.pdf", ".zh.mono.pdf", ".zh.dual.pdf"):
                if stem.endswith(suffix):
                    stem = stem[: -len(suffix)]
                    break
            fname = stem + ".pdf" if not stem.endswith(".pdf") else stem
            PDF_JOBS[name] = {
                "status": "completed",
                "progress": "完成（历史恢复）",
                "filename": fname,
                "mode": "mono",
                "workdir": d,
                "created_at": os.path.getmtime(d),
                "result": result,
            }
    except Exception:
        pass

    if PDF_JOBS:
        _pdf_jobs_save()

llm = None
inference_lock = threading.Lock()

# --------------------------------------------------------------------------
# 空闲自动退出
#
# 1.8B 模型常驻约 1GB 内存。看新闻、看视频都是一阵一阵的，没必要一直挂着。
# 连续 IDLE_EXIT_MIN 分钟没有真实请求就自己退出，内存归零；
# 需要时由浏览器插件经 Native Messaging 重新拉起。
# 设 HYMT_IDLE_EXIT=0 可关闭该行为（常驻）。
#
# IDLE_EXIT_MIN 是运行时可变的：插件面板的「省电 / 常驻」开关通过
# POST /v1/config {"idle_exit_min": 0} 直接改它，并持久化到 config.json，
# 下次启动自动沿用（见下方 user_config 加载后的覆盖）。
# --------------------------------------------------------------------------

IDLE_EXIT_MIN = float(os.getenv("HYMT_IDLE_EXIT", "20"))
START_TIME = time.time()

_last_activity = time.time()
_activity_lock = threading.Lock()
_active_jobs = 0

# 探活/查状态类请求不算「使用」，否则插件开着面板就永远退不出去
_NO_TOUCH_PATHS = ("/health", "/v1/status", "/v1/models", "/v1/config")


# 来源白名单。Origin 是浏览器保护的请求头，网页 JS 伪造不了，可靠：
#   放行 ① 扩展自身（chrome-extension:// / moz-extension://，ID 随目录路径
#          变化，不做精确匹配——任意扩展来源都比任意网页可信）；
#        ② 本服务自己的页面（/pdf 等同源 POST 也带 Origin）；
#        ③ 无 Origin 的请求（curl / test_api.py / 本地脚本等非浏览器客户端）。
#   其余一律 403：浏览器跨源请求全部拦下；DNS rebinding 的请求 Origin 是
#   攻击者域名，同样被拦。（扩展 ID 无法预先写死：开发者模式 ID 由目录哈希决定）
@app.before_request
def _origin_guard():
    origin = request.headers.get("Origin", "")
    if not origin:
        return None
    if origin.startswith(("chrome-extension://", "moz-extension://")):
        return None
    if origin in (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
        return None
    logger.warning(f"已拒绝跨源请求: Origin={origin} path={request.path}")
    return jsonify({"error": "forbidden_origin", "origin": origin}), 403


@app.before_request
def _touch_activity():
    global _last_activity
    path = request.path
    if path in _NO_TOUCH_PATHS or path.startswith("/v1/pdf/status/"):
        return
    with _activity_lock:
        _last_activity = time.time()


def _exit_clean(code=0):
    """以退出码 0 结束进程：launchd 的 KeepAlive 不会把它拉回来。"""
    time.sleep(0.3)
    try:
        logging.shutdown()
    except Exception:
        pass
    os._exit(code)


def apply_idle_exit_minutes(value):
    """运行时切换空闲退出策略（插件面板「省电 / 常驻」）。

    value: 分钟数，0 = 常驻不自动退出。
    切换时重置活动时间戳：否则从常驻切回限时后，会拿「上一次真实请求」的
    陈旧时间戳计算空闲时长，可能在切换后的第一个检查周期就立刻退出。
    """
    global IDLE_EXIT_MIN, _last_activity

    try:
        minutes = float(value)
    except (TypeError, ValueError):
        logger.warning(f"忽略非法 idle_exit_min: {value!r}")
        return False
    if minutes < 0:
        minutes = 0.0

    IDLE_EXIT_MIN = minutes
    with _activity_lock:
        _last_activity = time.time()
    logger.info(
        "空闲退出策略已更新: %s",
        "常驻（不自动退出）" if minutes <= 0 else f"{minutes:.0f} 分钟",
    )
    return True


def _idle_watchdog():
    # 常驻模式下线程继续空转而不是退出：用户在面板里随时能切回省电，
    # 线程提前结束就再也收不回来了。每轮重新读 IDLE_EXIT_MIN。
    while True:
        time.sleep(30)
        with _activity_lock:
            idle = time.time() - _last_activity
            busy = _active_jobs > 0
        limit = IDLE_EXIT_MIN * 60
        if limit <= 0 or busy or idle < limit:
            continue
        logger.info(
            "已空闲 %.0f 分钟，自动退出以释放模型内存（下次由插件按需拉起）",
            idle / 60,
        )
        _exit_clean(0)


# --------------------------------------------------------------------------
# 模型加载
# --------------------------------------------------------------------------

def resolve_model_path():
    """按优先级查找本地 GGUF 模型，避免硬编码单一路径。"""
    candidates = []

    env_path = os.getenv("HYMT_MODEL_PATH")
    if env_path:
        candidates.append(env_path)

    candidates.append(os.path.join(BASE_DIR, "models"))

    names = [
        "Hy-MT2-1.8B.Q4_K_M.gguf",
        "HY-MT1.5-1.8B.Q4_K_M.gguf",
    ]

    for folder in candidates:
        if not folder or not os.path.isdir(folder):
            continue
        for name in names:
            full = os.path.join(folder, name)
            if os.path.exists(full):
                return full
        for f in sorted(os.listdir(folder)):
            if f.lower().endswith(".gguf"):
                return os.path.join(folder, f)

    return None


def load_gguf_model():
    global llm

    model_path = resolve_model_path()

    if model_path:
        logger.info("=" * 60)
        logger.info(f"本地模型: {model_path}")
        logger.info(f"大小: {os.path.getsize(model_path) / (1024 ** 3):.2f} GB")
        logger.info("=" * 60)
    else:
        logger.error("未找到本地 GGUF 模型。请设置环境变量 HYMT_MODEL_PATH，"
                     "或将 .gguf 文件放入 models/ 目录。")
        return False

    try:
        # n_threads=0 让 llama.cpp 自动吃满所有 CPU 核（原硬编码 4 在纯 CPU
        # 机器上跑不满核）；可用环境变量 HYMT_N_THREADS 显式覆盖。
        n_threads = int(os.environ.get("HYMT_N_THREADS") or 0)
        llm = Llama(
            model_path=model_path,
            n_ctx=8192,
            n_threads=n_threads,
            n_gpu_layers=-1,
            verbose=False,
        )
        logger.info("模型加载成功 (Metal 加速已启用)")
        return True
    except Exception as e:
        logger.error(f"模型加载失败: {e}")
        logger.error(traceback_format())
        return False


def traceback_format():
    import traceback
    return traceback.format_exc()


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    # 源语言不设固定项：HY-MT 自动识别源语言（模型能力），历史配置里的
    # source_lang 键读进来也只是无人使用的残留，不影响行为
    "target_lang": "zh",
    "font_size": 28,
    "theme_mode": "dark",
    "display_mode": "append",
    "bilingual_subtitle": False,
    "stream_output": True,
    "idle_exit_min": 20,
    "pdf_engine": "babeldoc",
    "pdf_qps": 3,
    "enabled_websites": {"youtube": True, "twitter": True},
    "blacklist": [
        "google.com", "bing.com", "baidu.com", "duckduckgo.com",
        "localhost", "127.0.0.1", "0.0.0.0",
        "github.com", "gitlab.com", "stackoverflow.com", "npmjs.com",
        "figma.com", "canva.com", "notion.so",
        "sheets.google.com", "docs.google.com",
    ],
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            config = DEFAULT_CONFIG.copy()
            config.update(saved)
            return config
        except Exception as e:
            logger.error(f"配置文件读取失败，回退默认值: {e}")

    config = DEFAULT_CONFIG.copy()
    save_config(config)
    return config


def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)


user_config = load_config()

# 面板里选过空闲策略就沿用（config.json 持久化的值优先于环境变量）。
# 环境变量 HYMT_IDLE_EXIT 仍然有效，只是作为「从未在面板设置过」时的默认值。
_saved_idle_exit = user_config.get("idle_exit_min")
if _saved_idle_exit is not None:
    try:
        IDLE_EXIT_MIN = max(0.0, float(_saved_idle_exit))
    except (TypeError, ValueError):
        logger.warning(f"config.json 里 idle_exit_min 非法（{_saved_idle_exit!r}），回退环境变量值")


# --------------------------------------------------------------------------
# Prompt 构造（统一收在服务端，三个场景共用）
# --------------------------------------------------------------------------

# HY-MT 官方支持的语言表（33 语种 + 5 民汉变体，含常用别名）。
# LANG_NAMES：长文 prompt 用的英文名；LANG_NAMES_ZH：官方极简模板用中文名。
LANG_NAMES = {
    "zh": "Chinese", "zh-cn": "Chinese (Simplified)", "zh-CN": "Chinese (Simplified)",
    "zh-hant": "Traditional Chinese", "zh-Hant": "Traditional Chinese",
    "yue": "Cantonese", "en": "English", "en-us": "English",
    "ja": "Japanese", "ko": "Korean", "fr": "French",
    "de": "German", "es": "Spanish", "pt": "Portuguese", "it": "Italian",
    "ru": "Russian", "uk": "Ukrainian", "pl": "Polish", "cs": "Czech", "nl": "Dutch",
    "tr": "Turkish", "ar": "Arabic", "fa": "Persian", "he": "Hebrew",
    "hi": "Hindi", "ur": "Urdu", "bn": "Bengali", "gu": "Gujarati",
    "mr": "Marathi", "ta": "Tamil", "te": "Telugu",
    "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "ms": "Malay",
    "tl": "Filipino", "km": "Khmer", "my": "Burmese",
    "bo": "Tibetan", "kk": "Kazakh", "mn": "Mongolian", "ug": "Uyghur",
}
LANG_NAMES_ZH = {
    "zh": "中文", "zh-cn": "中文", "zh-CN": "中文",
    "zh-hant": "繁体中文", "zh-Hant": "繁体中文", "yue": "粤语",
    "en": "英文", "en-us": "英文",
    "ja": "日文", "ko": "韩文", "fr": "法文",
    "de": "德文", "es": "西班牙文", "pt": "葡萄牙文", "it": "意大利文",
    "ru": "俄文", "uk": "乌克兰文", "pl": "波兰文", "cs": "捷克文", "nl": "荷兰文",
    "tr": "土耳其文", "ar": "阿拉伯文", "fa": "波斯文", "he": "希伯来文",
    "hi": "印地文", "ur": "乌尔都文", "bn": "孟加拉文", "gu": "古吉拉特文",
    "mr": "马拉地文", "ta": "泰米尔文", "te": "泰卢固文",
    "th": "泰文", "vi": "越南文", "id": "印尼文", "ms": "马来文",
    "tl": "菲律宾文", "km": "高棉文", "my": "缅甸文",
    "bo": "藏文", "kk": "哈萨克文", "mn": "蒙古文", "ug": "维吾尔文",
}

# 反音译指令：HY-MT 1.8B 在短标题/专有名词上倾向音译（如 angst -> 安格斯），
# 必须显式约束，否则财经/新闻阅读体验很差。
ANTI_TRANSLITERATION_RULES = """\
RULES:
- Output ONLY the translation. No explanation, no notes, no quotation marks.
- Keep proper nouns, person names, brand names, publication names and organizations in their ORIGINAL form. Never transliterate them into Chinese characters.
- Preserve all numbers, units, dates and percentages exactly as written.
- Keep the original paragraph and line structure.
- Translate every sentence. Do not summarize or omit content."""


def build_translate_prompt(text, target_lang="zh", context=None, glossary=None):
    """
    构造翻译 prompt。

    关键认知：HY-MT（Hunyuan-MT）只在官方极简模板上训练过——
        把下面的文本翻译成<语言>，不要额外解释。
    长英文指令属于分布外输入，是模型回显 prompt 的主要根源。
    因此短文本（字幕、标题、段落）一律走官方模板；
    长文（>300 字符的整篇内容）才用带术语表和反音译规则的完整 prompt。
    """
    target_zh = LANG_NAMES_ZH.get(str(target_lang).lower(), "中文")

    # 短文本：官方极简模板（分布内，回显概率最低）
    if len(text) <= 300 and not glossary:
        return f"把下面的文本翻译成{target_zh}，不要额外解释。\n{text}"

    # 长文本：完整规则版
    target_name = LANG_NAMES.get(str(target_lang).lower(), target_lang)
    parts = [
        f"You are a professional translation engine. "
        f"Translate the following text into {target_name}.",
        "",
        ANTI_TRANSLITERATION_RULES,
    ]

    if glossary:
        # 上限与客户端（background.js GLOSSARY_MAX_ENTRIES）一致，防 prompt 过长
        lines = [f"- {k} = {v}" for k, v in list(glossary.items())[:80]]
        parts += ["", "GLOSSARY (use these exact translations):", *lines]

    if context:
        parts += ["", "CONTEXT (previous lines, for reference only):", context]

    parts += ["", "TEXT TO TRANSLATE:", text]
    return "\n".join(parts)


def clean_translation(text):
    """去掉模型偶尔吐出的角色标记和提示语残留。"""
    if not text:
        return text
    result = text
    for pattern in [
        # 指令回显：模型把 prompt 的任意一行当译文输出
        r"^\s*You are a professional[^\n]*\n?",
        r"^\s*Translate the following text[^\n]*\n?",
        r"RULES:.*?(?=TEXT TO TRANSLATE:|$)",
        r"^TEXT TO TRANSLATE:\s*",
        r"^CONTEXT \(previous lines[^)]*\):\s*\n?",
        r"GLOSSARY \(use these exact translations\):.*?(?=TEXT TO TRANSLATE:|$)",
    ]:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE | re.MULTILINE)
    result = re.sub(r"<[^>]+>", "", result)
    return result.strip()


# 指令回显检测：输出里出现 prompt 结构词，或长度远超输入，都视为废译文
ECHO_MARKERS = (
    "You are a professional",
    "Translate the following text",
    "TEXT TO TRANSLATE",
    "RULES:",
    "GLOSSARY",
    "CONTEXT (previous lines",
)

# 模型开始复读 prompt 结构时尽早截断，别浪费 token
ECHO_STOP = ["TEXT TO TRANSLATE:", "RULES:", "GLOSSARY", "CONTEXT (previous lines"]


def looks_like_echo(text, source=""):
    if not text or not text.strip():
        return True
    for marker in ECHO_MARKERS:
        if marker in text:
            return True
    # 译文远长于原文（>4 倍且超过 120 字符）大概率是回显/复读
    if source and len(text) > 120 and len(text) > len(source) * 4:
        return True
    return False


def is_same_language(text, target_lang):
    """目标语言已是原文语言时跳过翻译，省下推理时间。"""
    if not text:
        return False
    lang = str(target_lang).lower()

    # 只保留「字母」和「汉字」，其余（空白、标点、数字）一律剔除，
    # 用剩余字符的语言构成来判断源语言。
    stripped = re.sub(r"[^a-zA-Z\u4e00-\u9fff]", "", text)
    if not stripped:
        return False

    if lang.startswith("zh"):
        cjk = len(re.findall(r"[\u4e00-\u9fff]", stripped))
        return cjk / len(stripped) > 0.3
    if lang.startswith("en"):
        latin = len(re.findall(r"[a-zA-Z]", stripped))
        return latin / len(stripped) > 0.6
    return False


# --------------------------------------------------------------------------
# 基础端点
# --------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok" if llm is not None else "loading"})


@app.route("/v1/status", methods=["GET"])
def status():
    """给插件面板用的详细状态：运行了多久、空闲了多久、还有多久自动退出。"""
    with _activity_lock:
        idle = time.time() - _last_activity
        jobs = _active_jobs

    remain = max(0.0, IDLE_EXIT_MIN * 60 - idle) if IDLE_EXIT_MIN > 0 else None

    return jsonify({
        "status": "ok" if llm is not None else "loading",
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "idle_seconds": round(idle, 1),
        "idle_exit_minutes": IDLE_EXIT_MIN,
        "seconds_to_idle_exit": round(remain, 1) if remain is not None else None,
        "active_jobs": jobs,
        "port": PORT,
    })


@app.route("/v1/models", methods=["GET"])
def list_models():
    return jsonify({
        "object": "list",
        "data": [{"id": "hunyuan-mt", "object": "model", "owned_by": "tencent"}],
    })


@app.route("/v1/config", methods=["GET", "POST"])
def handle_config():
    global user_config
    if request.method == "POST":
        new_config = request.json
        if new_config:
            # 空闲策略特殊处理：除写入持久化配置外，还要立即作用于运行中的服务
            if "idle_exit_min" in new_config:
                apply_idle_exit_minutes(new_config["idle_exit_min"])
            user_config.update(new_config)
            save_config(user_config)
            logger.info("配置已更新")
        return jsonify({"success": True, "config": user_config})
    return jsonify(user_config)


@app.route("/shutdown", methods=["POST"])
@app.route("/v1/shutdown", methods=["POST"])
def shutdown():
    logger.info("接收到关闭信号，服务即将退出")
    # 延迟一点点，确保 HTTP 响应先发出去
    threading.Timer(0.4, _exit_clean, args=(0,)).start()
    return jsonify({"status": "stopping"})


# --------------------------------------------------------------------------
# 翻译端点（三个场景统一走这里）
# --------------------------------------------------------------------------

def estimate_max_tokens(text, floor=512, cap=4096):
    """按输入长度动态放宽输出 token 上限。

    旧逻辑固定 512：长段落译文会被静默截断，用户看到半句话且无任何报错。
    译文 token 数与原文长度大致同量级，len*2 已留足余量；
    cap 4096 是因为 n_ctx 默认 8192，还要给系统 prompt 留空间。
    """
    return max(floor, min(len(text) * 2, cap))


def _infer_translate(text, target_lang, context=None, glossary=None, max_tokens=None):
    """单条翻译的完整推理流程（含指令回显低温重试）。单条与批量端点共用。

    回显重试仍救不回来时抛 ValueError("translation_garbled")，由调用方决定返回方式。
    """
    prompt = build_translate_prompt(text, target_lang, context, glossary)
    mt = max_tokens or estimate_max_tokens(text)

    with inference_lock:
        response = llm(
            prompt,
            max_tokens=mt,
            temperature=0.3,
            top_p=0.6,
            top_k=20,
            repeat_penalty=1.05,
            echo=False,
            stop=ECHO_STOP,
        )

    # 输出顶满上限说明译文被掐断（动态计算后仍可能撞 cap），留下日志便于排查
    if response["choices"][0].get("finish_reason") == "length":
        logger.warning(f"译文可能被截断: max_tokens={mt}, finish_reason=length")

    translation = clean_translation(response["choices"][0]["text"])

    # 指令回显防护：检测到废译文就用官方极简模板低温重试一次
    if looks_like_echo(translation, text):
        logger.warning(f"检测到指令回显({len(translation)}字符)，低温重试")
        target_zh = LANG_NAMES_ZH.get(str(target_lang).lower(), "中文")
        retry_prompt = f"把下面的文本翻译成{target_zh}，不要额外解释。\n{text}"
        with inference_lock:
            response = llm(
                retry_prompt,
                max_tokens=max(128, len(text) * 2),
                temperature=0.1,
                top_p=0.6,
                top_k=20,
                repeat_penalty=1.05,
                echo=False,
                stop=ECHO_STOP,
            )
        translation = clean_translation(response["choices"][0]["text"])
        if looks_like_echo(translation, text):
            logger.error("重试仍为回显，放弃本次译文")
            raise ValueError("translation_garbled")

    return translation


@app.route("/v1/translate", methods=["POST"])
def translate():
    """
    结构化翻译端点。客户端只传文本，prompt 由服务端统一构造。

    body: {
      "text":        "要翻译的文本",
      "target_lang": "zh",          # 可选，默认取配置
      "context":     "上一句",        # 可选，YouTube 字幕用
      "glossary":    {"angst": "焦虑"},  # 可选
      "stream":      false           # 可选
    }
    """
    if llm is None:
        return jsonify({"error": "Model not loaded"}), 503

    data = request.json or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "No text provided"}), 400

    target_lang = data.get("target_lang") or user_config.get("target_lang", "zh")
    context = data.get("context")
    glossary = data.get("glossary")
    stream = bool(data.get("stream"))

    if is_same_language(text, target_lang):
        return jsonify({
            "translation": text,
            "skipped": True,
            "reason": "same_language",
            "elapsed": 0,
        })

    started = time.time()

    # 客户端未显式传 max_tokens 时按输入长度动态计算，避免长段落截断；
    # 显式传入则钳制到上限，防第三方传极大值占满上下文
    req_max = data.get("max_tokens")
    max_tokens = _clamp_max_tokens(req_max) if req_max else estimate_max_tokens(text)

    if stream:
        prompt = build_translate_prompt(text, target_lang, context, glossary)
        return Response(
            stream_translation(prompt, max_tokens),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        translation = _infer_translate(text, target_lang, context, glossary, max_tokens)
        elapsed = time.time() - started
        logger.info(f"翻译完成 {elapsed:.2f}s ({len(text)} -> {len(translation)} 字符)")
        return jsonify({
            "translation": translation,
            "skipped": False,
            "elapsed": round(elapsed, 3),
        })
    except ValueError as e:
        if str(e) == "translation_garbled":
            return jsonify({"error": "translation_garbled", "elapsed": round(time.time() - started, 3)}), 502
        raise
    except Exception as e:
        logger.error(f"翻译失败: {e}")
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------------------------------
# 批量翻译端点：多条文本合并成一次推理，摊薄每条一次的完整 prefill。
#
# 注意（实测结论，勿被旧注释误导）：本地 GGUF 推理里「批量」并不会提速——
# 1.8B 解码极快，耗时大头是解码量；批量只是把多条拼进同一次 prefill，不减少
# 总解码量，反而因编号协议解析/降级开销比逐条还慢约 15–24%（见项目工作记忆
# 2026-08-31 实测）。本端点目前没有任何客户端调用，仅作为 OpenAI 兼容的
# 备用能力保留；整页翻译的提速杠杆是「缓存持久化」而非批量。
# --------------------------------------------------------------------------

BATCH_MAX_ITEMS = 20          # 单批条数上限（沉浸式翻译为 25，这里保守一些）
BATCH_MAX_TOTAL_CHARS = 4000  # 单批总字符上限，给 n_ctx=8192 留出输出空间

# 条目自带的行首编号会干扰拆号解析，统一剥掉
_LEADING_NUMBER_RE = re.compile(r"^\s*\(?\d{1,2}\s*[.、．)）:：]\s*")
# 输出解析：行首编号（兼容 1. / 1、 / 1) / (1) / 1：等写法）
_NUMBER_LINE_RE = re.compile(r"^\s*\(?(?P<num>\d{1,2})\s*[.、．)）:：]\s*(?P<body>.*)$")


def build_batch_prompt(texts, target_zh):
    """编号协议 prompt。

    第一行保持 HY-MT 官方极简模板原文（分布内），编号说明放第二行；
    条目内部的换行统一压平，避免破坏「每条一行」的结构。
    """
    lines = [
        f"把下面的文本翻译成{target_zh}，不要额外解释。",
        f"原文共{len(texts)}条，每条以数字编号开头。逐条翻译，输出也用同样的数字编号，每条译文一行。",
    ]
    for i, text in enumerate(texts, 1):
        flat = " ".join(text.split())
        flat = _LEADING_NUMBER_RE.sub("", flat).strip()
        if not flat:
            flat = text.strip() or " "
        lines.append(f"{i}. {flat}")
    return "\n".join(lines)


def parse_numbered_output(raw, count):
    """按编号把模型的输出拆回单条译文。

    返回 {序号: 译文或None}。解析不到的条目为 None，由调用方降级单条重翻。
    """
    pieces = {}
    current = None
    buf = []
    for line in raw.splitlines():
        m = _NUMBER_LINE_RE.match(line)
        if m and 1 <= int(m.group("num")) <= count:
            if current is not None:
                pieces[current] = " ".join(buf).strip()
            current = int(m.group("num"))
            buf = [m.group("body")]
        elif current is not None:
            buf.append(line)
    if current is not None:
        pieces[current] = " ".join(buf).strip()

    result = {}
    for i in range(1, count + 1):
        piece = pieces.get(i) or ""
        result[i] = clean_translation(piece) if piece.strip() else None
    return result


@app.route("/v1/translate/batch", methods=["POST"])
def translate_batch():
    """
    批量翻译端点：一次推理翻多条。

    body: {
      "items":       [{"id": "p0", "text": "..."}],   # id 原样回传，类型不限
      "target_lang": "zh"
    }
    resp: {
      "translations": {"p0": "译文", ...},   # 一定包含每个 id（含 skipped 的原文）
      "skipped":      {"p1": true, ...},     # 源语言与目标语言一致而跳过的条目
      "fallbacks":    2,                     # 编号解析失败降级单条重翻的条数
      "elapsed":      1.23
    }

    编号协议对 1.8B 小模型是分布外输入：拆号失败或疑似回显的条目
    自动降级走单条翻译链路（含回显重试），保证每条都有结果或明确报错。
    """
    if llm is None:
        return jsonify({"error": "Model not loaded"}), 503

    data = request.json or {}
    raw_items = data.get("items") or []
    target_lang = data.get("target_lang") or user_config.get("target_lang", "zh")
    started = time.time()

    if not raw_items:
        return jsonify({"error": "No items provided"}), 400
    if len(raw_items) > BATCH_MAX_ITEMS:
        return jsonify({"error": f"Too many items (max {BATCH_MAX_ITEMS})"}), 400

    # 规范化 + 按文本去重（同文本只翻一次，结果回填到所有相同 id）
    items = []
    seen = {}
    for item in raw_items:
        if not isinstance(item, dict) or "text" not in item:
            return jsonify({"error": "Each item must be an object with 'text'"}), 400
        text = (item.get("text") or "").strip()
        if not text:
            return jsonify({"error": "Item text is empty"}), 400
        if len(text) > 5000:
            return jsonify({"error": "Item text too long (max 5000 chars)"}), 400
        if text in seen:
            seen[text].append(item.get("id"))
        else:
            seen[text] = [item.get("id")]
            items.append(text)

    total_chars = sum(len(t) for t in items)
    if total_chars > BATCH_MAX_TOTAL_CHARS:
        return jsonify({
            "error": f"Batch too large: {total_chars} chars (max {BATCH_MAX_TOTAL_CHARS})"
        }), 400

    translations = {}   # id -> 译文
    skipped = {}        # id -> True（同语言跳过）
    to_translate = []   # [(index, text)] 需要真正推理的

    for text in items:
        if is_same_language(text, target_lang):
            for iid in seen[text]:
                translations[str(iid)] = text
                skipped[str(iid)] = True
        else:
            to_translate.append(text)

    fallbacks = 0
    if to_translate:
        target_zh = LANG_NAMES_ZH.get(str(target_lang).lower(), "中文")
        prompt = build_batch_prompt(to_translate, target_zh)
        # 输出上限 = 各条估和（宁多勿少，避免批次内后面的条目被掐断）
        max_tokens = max(256, min(sum(len(t) for t in to_translate) * 2 + 64 * len(to_translate), 4096))

        try:
            with inference_lock:
                response = llm(
                    prompt,
                    max_tokens=max_tokens,
                    temperature=0.3,
                    top_p=0.6,
                    top_k=20,
                    repeat_penalty=1.05,
                    echo=False,
                    stop=ECHO_STOP,
                )
            parsed = parse_numbered_output(response["choices"][0]["text"], len(to_translate))
        except Exception as e:
            logger.error(f"批量推理失败: {e}")
            parsed = {}

        # 解析失败 / 疑似回显的条目降级为单条翻译（含回显重试链路）
        retry_texts = []
        for idx, text in enumerate(to_translate, 1):
            piece = parsed.get(idx)
            if not piece or looks_like_echo(piece, text):
                retry_texts.append(text)
            else:
                for iid in seen[text]:
                    translations[str(iid)] = piece

        for text in retry_texts:
            fallbacks += 1
            try:
                piece = _infer_translate(text, target_lang)
                for iid in seen[text]:
                    translations[str(iid)] = piece
            except ValueError:
                logger.error(f"批量降级重翻仍为回显: {text[:40]}...")
            except Exception as e:
                logger.error(f"批量降级重翻失败: {e}")

    elapsed = time.time() - started
    logger.info(
        f"批量翻译完成 {elapsed:.2f}s: {len(raw_items)}条 / {len(items)}去重 / "
        f"{fallbacks}条降级重翻 ({total_chars}字符)"
    )
    return jsonify({
        "translations": translations,
        "skipped": skipped,
        "fallbacks": fallbacks,
        "elapsed": round(elapsed, 3),
    })


def stream_translation(prompt, max_tokens=512):
    """SSE 流式输出，YouTube 字幕逐字显示。"""
    def generate():
        try:
            with inference_lock:
                for chunk in llm(
                    prompt,
                    max_tokens=max_tokens,
                    temperature=0.3,
                    top_p=0.6,
                    top_k=20,
                    repeat_penalty=1.05,
                    echo=False,
                    stream=True,
                    stop=ECHO_STOP,
                ):
                    piece = chunk["choices"][0]["text"]
                    if piece:
                        yield f"data: {json.dumps({'delta': piece}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            logger.error(f"流式翻译失败: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return generate()


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    """
    OpenAI 兼容端点。
    保留此端点是为了让 BabelDOC 能直接把本服务当作翻译后端调用。
    """
    if llm is None:
        return jsonify({"error": "Model not loaded"}), 503

    data = request.json or {}
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"error": "No messages provided"}), 400

    system_prompt = ""
    user_prompt = ""
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            system_prompt = content
        else:
            user_prompt = content

    stream = bool(data.get("stream"))

    # 若调用方已在 system 里给了指令（BabelDOC 的 custom-system-prompt），
    # 就尊重它；否则套用我们的标准指令。
    if system_prompt and len(system_prompt) > 20:
        prompt = f"{system_prompt}\n\n{user_prompt}"
    else:
        prompt = build_translate_prompt(
            user_prompt,
            user_config.get("target_lang", "zh"),
        )

    if stream:
        return Response(
            stream_chat(prompt, data),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        with inference_lock:
            response = llm(
                prompt,
                max_tokens=_clamp_max_tokens(data.get("max_tokens", 2048)),
                temperature=data.get("temperature", 0.3),
                top_p=0.6,
                top_k=20,
                repeat_penalty=1.05,
                echo=False,
            )
        text = clean_translation(response["choices"][0]["text"])
        return jsonify({
            "id": "chatcmpl-hymt",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "hunyuan-mt",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": response.get("usage", {}),
        })
    except Exception as e:
        logger.error(f"chat 请求失败: {e}")
        return jsonify({"error": str(e)}), 500


def stream_chat(prompt, data):
    def generate():
        try:
            with inference_lock:
                for chunk in llm(
                    prompt,
                    max_tokens=_clamp_max_tokens(data.get("max_tokens", 2048)),
                    temperature=data.get("temperature", 0.3),
                    top_p=0.6,
                    top_k=20,
                    repeat_penalty=1.05,
                    echo=False,
                    stream=True,
                ):
                    piece = chunk["choices"][0]["text"]
                    payload = {
                        "id": "chatcmpl-hymt",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "hunyuan-mt",
                        "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            final = {
                "id": "chatcmpl-hymt",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "hunyuan-mt",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(final)}\n\n"
        except Exception as e:
            logger.error(f"流式 chat 失败: {e}")
            yield f"data: {json.dumps({'error': {'message': str(e)}})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return generate()


# --------------------------------------------------------------------------
# PDF 翻译（BabelDOC 版面还原引擎）
# --------------------------------------------------------------------------

BABELDOC_SYSTEM_PROMPT = (
    "You are a professional, authentic machine translation engine. "
    "Keep proper nouns, person names, brand names, publication names and "
    "organizations in their original form. Never transliterate them. "
    "Preserve numbers, units and dates exactly. Output only the translation."
)


def find_babeldoc_executable():
    exe = shutil.which("babeldoc")
    if exe:
        return [exe]
    venv_exe = os.path.join(BASE_DIR, "venv", "bin", "babeldoc")
    if os.path.exists(venv_exe):
        return [venv_exe]
    return [sys.executable, "-m", "babeldoc.main"]


def _parse_pages(spec, page_count):
    """解析 BabelDOC 风格页码：'1-10' '1,3-5' '-3' '1-'，返回 1-based 列表。"""
    out = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo = int(a) if a else 1
            hi = int(b) if b else page_count
            out.update(range(lo, hi + 1))
        else:
            out.add(int(part))
    return sorted(i for i in out if 1 <= i <= page_count)


def _extract_pages(src_path, page_numbers, dst_path):
    """按 1-based 页码抽出页面，生成一个新 PDF。返回实际抽出的页数。

    这是整个功能的枢纽：把"翻译完整本再裁产物"反过来，改成
    **先把输入裁成只有选中页，再送 BabelDOC**。好处有三：
      1. OCR（0.72s/页）只跑选中页——扫描件选 12 页能省掉几十秒
      2. 版面解析（ONNX 模型）也只跑选中页
      3. 产物天然就是选中页且连续编号，不再需要脆弱的后处理裁剪
    """
    import pymupdf
    src = pymupdf.open(src_path)
    out = pymupdf.open()
    for p in page_numbers:
        i = p - 1
        if 0 <= i < src.page_count:
            out.insert_pdf(src, from_page=i, to_page=i)
    n = out.page_count
    if n:
        out.save(dst_path, garbage=4, deflate=True)
    out.close()
    src.close()
    return n


def _apply_bookmarks(pdf_path, bookmarks):
    """把选中文章写回成品 PDF 的书签，重组后的文件可以直接跳文章。

    bookmarks: [{"title": str, "page": int}]，page 为成品里的 1-based 页码。
    杂志类 PDF 原本没有书签，这是翻译后顺手补上的导航。
    """
    if not bookmarks:
        return
    try:
        import pymupdf
        doc = pymupdf.open(pdf_path)
        toc = [[1, b["title"], max(1, min(int(b["page"]), doc.page_count))]
               for b in bookmarks if b.get("title")]
        if toc:
            doc.set_toc(toc)
            doc.save(pdf_path, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
        doc.close()
    except Exception as e:
        logger.warning(f"写回书签失败 {pdf_path}: {e}")


def run_babeldoc(job_id, input_path, lang_out="zh"):
    """后台任务：调用 BabelDOC CLI，翻译后端指回本服务的 OpenAI 兼容端点。

    注意：input_path 传进来的已经是**裁剪过**的 PDF（只含用户勾选的页），
    所以这里不再给 BabelDOC 传 --pages，也不做事后裁剪。
    """
    job = PDF_JOBS[job_id]
    workdir = os.path.dirname(input_path)

    try:
        job["status"] = "processing"
        job["progress"] = "启动 BabelDOC 排版引擎"

        # 先用 pymupdf 探测 PDF 是否有文字层。
        # 扫描件（图片型 PDF）必须先 OCR，否则 BabelDOC 会报 "no paragraphs"。
        # 扫描件：自动用 Apple Vision OCR 加文字层
        try:
            from ocr_engine import needs_ocr, ocr_pdf_to_searchable
            if needs_ocr(input_path):
                logger.info(f"[{job_id}] 扫描件，启动 Apple Vision OCR ...")
                job["progress"] = "扫描件，启动 Apple Vision OCR 加文字层"
                ocr_path = os.path.join(workdir, f"ocr_{os.path.basename(input_path)}")
                ocr_info = ocr_pdf_to_searchable(
                    input_path, ocr_path,
                    dpi=user_config.get("pdf_dpi", 200),
                    progress_cb=lambda m: job.update({"progress": m}),
                )
                input_path = ocr_path
                logger.info(f"[{job_id}] OCR 完成: {ocr_info}")
        except ImportError:
            # ocr_engine 不可用时保持原有报错路径
            try:
                import pymupdf
                probe = pymupdf.open(input_path)
                text_chars = sum(len(probe[i].get_text().strip()) for i in range(min(3, probe.page_count)))
                probe.close()
                if text_chars < 50:
                    job["status"] = "failed"
                    job["error"] = "PDF 无文字层且 OCR 引擎不可用"
                    return
            except Exception:
                pass

        cmd = find_babeldoc_executable() + [
            "--files", input_path,
            "--openai",
            "--openai-model", "hunyuan-mt",
            "--openai-base-url", f"http://127.0.0.1:{PORT}/v1",
            "--openai-api-key", "local",
            "--lang-in", "en",
            "--lang-out", lang_out,
            "--qps", str(user_config.get("pdf_qps", 3)),
            "--custom-system-prompt", BABELDOC_SYSTEM_PROMPT,
            "--watermark-output-mode", "no_watermark",
            "--no-auto-extract-glossary",  # 本机小模型术语抽取意义不大，省一轮
        ]

        logger.info(f"[{job_id}] BabelDOC 启动: {' '.join(cmd[:6])} ...")

        # babeldoc 只需要访问本机翻译服务（127.0.0.1）。
        # 若运行环境带了系统代理（HTTP_PROXY 等），openai/httpx 会把
        # 127.0.0.1 的请求也塞进代理，导致 APIConnectionError，
        # 所以这里强制剥离代理变量并设置 NO_PROXY。
        env = {**os.environ, "HF_ENDPOINT": os.getenv("HF_ENDPOINT", "https://hf-mirror.com")}
        for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                   "ALL_PROXY", "all_proxy"):
            env.pop(_k, None)
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["no_proxy"] = "127.0.0.1,localhost"

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=workdir,
            env=env,
        )

        # BabelDOC 的 rich 进度条用 \r 刷新（不带换行），按行读永远看不到更新。
        # 这里按 \r 和 \n 一起切行，实时解析百分比，并过滤无害的弃用警告。
        import select as _select

        ansi_re = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
        last_log_key = ""

        def handle_output_line(line):
            nonlocal last_log_key
            line = ansi_re.sub("", line).strip()
            if not line:
                return
            # 全部进日志（内容变化才记一条，进度条刷新很频繁）
            key = line[:60]
            if key != last_log_key:
                last_log_key = key
                logger.info(f"[{job_id}] {line[:300]}")

            # ---- 进度显示只挑对用户有意义的行，原始日志不吓人 ----
            if "fitz" in line and "deprecated" in line:
                return
            # "completed. Total: 63, Successful: 49, Fallback: 14"
            m = re.search(r"Total:\s*(\d+)\D+Successful:\s*(\d+)\D+Fallback:\s*(\d+)", line)
            if m:
                total, ok, fb = m.groups()
                job["_total"] = int(total)
                job["progress"] = f"翻译中… 共 {total} 段（成功 {ok} / 兜底 {fb}）"
                return
            m = re.search(r"(\d{1,3})\s*%", line)
            if m:
                job["progress"] = f"翻译中… {m.group(1)}%"
                return
            # 逐段进度：babeldoc 每处理一个文本块会打一行 "paragraph id: xxx"
            if re.search(r"paragraph id:", line):
                job["_paras"] = job.get("_paras", 0) + 1
                job["progress"] = f"翻译中… 已处理 {job['_paras']} 个文本块"
                return
            if re.search(r"[Ll]oading.*[Mm]odel|ONNX model", line):
                job["progress"] = "加载版面分析模型…"
                return
            if re.search(r"start to translate", line):
                job["progress"] = "版面解析完成，开始翻译…"
                return
            if re.search(r"[Pp]arsing|[Cc]omposing|OCR", line):
                job["progress"] = "解析版面中…"
                return
            if re.search(r"pdf_creater|[Cc]reated|saving|Saving", line):
                job["progress"] = "生成 PDF 中…"
                return
            # 其余（try fallback、连接重试、堆栈碎片等）只进日志，不动进度显示

        fd = proc.stdout.fileno()
        buf = ""
        while True:
            ready, _, _ = _select.select([fd], [], [], 1.0)
            if ready:
                chunk = os.read(fd, 8192)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", "replace")
                parts = re.split(r"[\r\n]+", buf)
                buf = parts.pop()  # 最后一段可能是半行，留给下一轮
                for line in parts:
                    handle_output_line(line)
            elif proc.poll() is not None:
                break
        if buf.strip():
            handle_output_line(buf)
        proc.wait()

        if proc.returncode != 0:
            job["status"] = "failed"
            job["error"] = f"BabelDOC 退出码 {proc.returncode}"
            return

        stem = os.path.splitext(os.path.basename(input_path))[0]
        # BabelDOC 0.6.x 真实输出命名：
        #   {stem}.no_watermark.{lang}.mono.pdf    （纯译文版）
        #   {stem}.no_watermark.{lang}.dual.pdf    （双语对照版）
        #   {stem}.watermarked.{lang}.mono.pdf     （带水印版本）
        import glob as _glob

        # run_babeldoc 是后台线程，不能访问 Flask request 上下文
        wanted_mode = job.get("mode", "mono")
        suffix = ".dual.pdf" if wanted_mode == "dual" else ".mono.pdf"
        candidates = (
            _glob.glob(os.path.join(workdir, f"{stem}*.{lang_out}.mono.pdf"))
            + _glob.glob(os.path.join(workdir, f"{stem}*.{lang_out}.dual.pdf"))
        )
        # 优先用 watermark_output_mode=both 时生成的带水印版之外的版本
        candidates = [c for c in candidates if "watermarked" not in os.path.basename(c)]

        if candidates:
            # 优先匹配用户期望的 mode（mono/dual）
            preferred = [c for c in candidates if c.endswith(suffix)]
            chosen = sorted(preferred)[0] if preferred else sorted(candidates)[0]

            # 输入已经是裁剪过的，产物天然只有选中页，无需再裁。
            # 顺手把文章标题写回书签——原 PDF 没有书签，成品反而可导航了。
            for c in candidates:
                _apply_bookmarks(c, job.get("bookmarks") or [])

            n_sel = job.get("selected_pages") or 0
            job["progress"] = f"完成（{n_sel} 页）" if n_sel else "完成"

            # 所选页可译段落过少：多半是封面/目录/图片页，提前告知避免误解
            total_paras = job.get("_total") or 0
            if total_paras and total_paras < 5:
                job["progress"] += " · 所选页可译正文较少（多为图片/版式页）"

            job["status"] = "completed"
            job["result"] = chosen
            logger.info(f"[{job_id}] 输出: {chosen}")
            return

        # 兜底：列出工作目录里所有 PDF
        all_pdfs = _glob.glob(os.path.join(workdir, "*.pdf"))
        if all_pdfs:
            job["status"] = "completed"
            job["result"] = sorted(all_pdfs)[-1]
            job["progress"] = "完成（候选模糊匹配）"
            return

        job["status"] = "failed"
        job["error"] = "BabelDOC 未生成输出文件"

    except Exception as e:
        logger.error(f"[{job_id}] PDF 翻译异常: {e}")
        job["status"] = "failed"
        job["error"] = str(e)


def _run_pdf_job(job_id, input_path, lang_out):
    """包一层：PDF 任务跑着的时候，空闲看门狗不许退出服务。"""
    global _active_jobs
    with _activity_lock:
        _active_jobs += 1
    try:
        run_babeldoc(job_id, input_path, lang_out)
    finally:
        with _activity_lock:
            _active_jobs -= 1
        _pdf_jobs_save()  # 完成/失败后落盘一次


@app.route("/v1/pdf/toc", methods=["POST"])
def pdf_toc():
    """
    上传 PDF 并提取文章目录——只解析、不翻译，2-4 秒返回。

    杂志类 PDF 几乎没有内置书签，目录靠版面字号推断（见 pdf_toc.py）。
    返回 upload_id，用户勾选完再拿它调 /v1/pdf/translate。
    """
    if "file" not in request.files:
        return jsonify({"error": "缺少 file 字段"}), 400
    uploaded = request.files["file"]
    if not uploaded.filename:
        return jsonify({"error": "文件名为空"}), 400

    # 先剥离路径穿越风险，再校验扩展名
    safe_name = _safe_upload_name(uploaded.filename)
    if not safe_name.lower().endswith(".pdf"):
        return jsonify({"error": "仅支持 PDF 文件（.pdf）"}), 400

    try:
        from pdf_toc import extract_toc
    except ImportError as e:
        return jsonify({"error": f"目录提取模块不可用: {e}"}), 500

    upload_id = str(uuid.uuid4())
    udir = os.path.join(UPLOAD_DIR, upload_id)
    os.makedirs(udir, exist_ok=True)
    path = os.path.join(udir, safe_name)
    uploaded.save(path)

    # 扩展名之外再验 %PDF 魔数：传错文件时提前返回 400，而不是在 pymupdf.open 处爆 500
    if not _is_pdf_file(path):
        shutil.rmtree(udir, ignore_errors=True)
        return jsonify({"error": "文件不是有效的 PDF（缺少 %PDF 头）"}), 400

    try:
        digest = _sha256_of(path)
        if digest in TOC_CACHE:
            result = TOC_CACHE[digest]
        else:
            result = extract_toc(path)
            TOC_CACHE[digest] = result
    except Exception as e:
        shutil.rmtree(udir, ignore_errors=True)
        logger.error(f"目录提取失败: {e}")
        return jsonify({"error": f"目录提取失败: {e}"}), 500

    PDF_UPLOADS[upload_id] = {
        "path": path,
        "filename": safe_name,
        "sha256": digest,
        "page_count": result.get("page_count"),
        "created_at": time.time(),
    }
    _uploads_save()

    return jsonify({
        "upload_id": upload_id,
        "filename": safe_name,
        "page_count": result.get("page_count"),
        "source": result.get("source"),
        "confidence": result.get("confidence"),
        "warnings": result.get("warnings", []),
        "articles": result.get("articles", []),
    })


@app.route("/v1/pdf/thumb/<upload_id>/<int:page>", methods=["GET"])
def pdf_thumb(upload_id, page):
    """按需渲染某一页缩略图，落盘缓存，重复请求直接命中。

    为什么不随目录一起返回：76 页全渲染约 0.8 秒，虽然不慢，但用户多半
    只看前几屏。按需渲染配合浏览器 loading="lazy"，滚到哪渲染哪，
    首屏不受影响；命中缓存后就是静态文件，零成本。
    """
    up = PDF_UPLOADS.get(upload_id)
    if not up or not os.path.exists(up.get("path", "")):
        return jsonify({"error": "上传已过期"}), 410

    # 上限 1600 是给灯箱看大图用的（w=1200 的 JPEG 约 400KB，可接受）
    width = max(80, min(int(request.args.get("w", 180)), 1600))
    cache_dir = os.path.join(os.path.dirname(up["path"]), "thumbs")
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, f"{page}_{width}.jpg")
    if os.path.exists(cache_path):
        return send_file(cache_path, mimetype="image/jpeg")

    try:
        import pymupdf
        doc = pymupdf.open(up["path"])
        if not 1 <= page <= doc.page_count:
            doc.close()
            return jsonify({"error": "页码越界"}), 404
        pg = doc[page - 1]
        zoom = width / max(pg.rect.width, 1.0)
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        pix.save(cache_path, jpg_quality=75)
        doc.close()
    except Exception as e:
        logger.warning(f"缩略图渲染失败 {upload_id} p{page}: {e}")
        return jsonify({"error": f"缩略图渲染失败: {e}"}), 500

    return send_file(cache_path, mimetype="image/jpeg")


@app.route("/v1/pdf/translate", methods=["POST"])
def pdf_translate():
    """
    开始翻译。两个入口：

      1. upload_id（新流程）：已用 /v1/pdf/toc 解析过目录，按勾选的文章翻译
      2. file（旧流程）：直接上传，整本翻译或用 pages 手工指定页码

    form 字段：
      upload_id | file
      selection   JSON 数组 [{title, start, end}]，用户勾选的文章
      pages       手工页码范围，如 "1,3-5"（selection 为空时生效）
    """
    upload_id = request.form.get("upload_id") or None
    uploaded = request.files.get("file")

    if upload_id:
        up = PDF_UPLOADS.get(upload_id)
        if not up or not os.path.exists(up.get("path", "")):
            return jsonify({"error": "上传已过期，请重新选择文件"}), 410
        src_file, filename = up["path"], up["filename"]
    elif uploaded and uploaded.filename:
        filename = _safe_upload_name(uploaded.filename)
        if not filename.lower().endswith(".pdf"):
            return jsonify({"error": "仅支持 PDF 文件（.pdf）"}), 400
        src_file = None
    else:
        return jsonify({"error": "缺少 upload_id 或 file"}), 400

    pages_spec = (request.form.get("pages") or "").strip()
    lang_out = request.form.get("lang_out") or user_config.get("target_lang", "zh")
    mode = request.form.get("mode", "mono")
    if mode not in ("mono", "dual"):
        mode = "mono"

    # 勾选项：[{title, start, end}]，服务器据此算页码和书签，避免前端两边算不一致
    selection = []
    raw_sel = (request.form.get("selection") or "").strip()
    if raw_sel:
        try:
            selection = [a for a in json.loads(raw_sel)
                         if isinstance(a, dict) and a.get("start") and a.get("end")]
        except Exception as e:
            logger.warning(f"selection 解析失败，忽略: {e}")

    job_id = str(uuid.uuid4())
    workdir = os.path.join(BASE_DIR, "pdf_jobs", job_id)
    os.makedirs(workdir, exist_ok=True)

    input_path = os.path.join(workdir, filename)
    if src_file:
        shutil.copy2(src_file, input_path)
    else:
        uploaded.save(input_path)

    # ---- 只把选中的页喂给 BabelDOC ----
    wanted = []
    if selection:
        for a in selection:
            wanted.extend(range(int(a["start"]), int(a["end"]) + 1))
        wanted = sorted(set(wanted))
    elif pages_spec:
        import pymupdf as _pm
        probe = _pm.open(input_path)
        total = probe.page_count
        probe.close()
        wanted = _parse_pages(pages_spec, total)

    bookmarks = []
    selected_pages = 0
    if wanted:
        trimmed = os.path.join(workdir, "selected.pdf")
        selected_pages = _extract_pages(input_path, wanted, trimmed)
        if not selected_pages:
            return jsonify({"error": "所选页码在 PDF 中不存在"}), 400
        # 书签页码 = 该文章起始页在「选中页序列」里的位置
        pos = {p: i + 1 for i, p in enumerate(wanted)}
        for a in selection:
            p = pos.get(int(a["start"]))
            if p and a.get("title"):
                bookmarks.append({"title": a["title"], "page": p})
        input_path = trimmed

    try:
        from pdf_toc import selection_to_pages
        pages_label = selection_to_pages(selection, range(len(selection))) if selection else pages_spec
    except Exception:
        pages_label = pages_spec

    PDF_JOBS[job_id] = {
        "status": "pending",
        "progress": "排队中",
        "filename": filename,
        "mode": mode,
        "pages": pages_label or "",
        "workdir": workdir,
        "created_at": time.time(),
        "bookmarks": bookmarks,
        "selected_pages": selected_pages,
    }
    _pdf_jobs_save()

    t = threading.Thread(
        target=_run_pdf_job,
        args=(job_id, input_path, lang_out),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/v1/pdf/status/<job_id>", methods=["GET"])
def pdf_status(job_id):
    job = PDF_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({
        "status": job.get("status"),
        "progress": job.get("progress"),
        "filename": job.get("filename"),
        "error": job.get("error"),
    })


@app.route("/v1/pdf/download/<job_id>", methods=["GET"])
def pdf_download(job_id):
    job = PDF_JOBS.get(job_id)
    if not job or job.get("status") != "completed":
        return jsonify({"error": "任务未完成"}), 404

    # ?variant=mono|dual 精确下载对应版本；不带参数保持旧行为（默认产物）
    variant = request.args.get("variant", "")
    src = job.get("result")
    if variant in ("mono", "dual") and job.get("workdir"):
        import glob as _glob
        cands = [
            p for p in _glob.glob(os.path.join(job["workdir"], f"*.{variant}.pdf"))
            if "watermarked" not in os.path.basename(p)
        ]
        if cands:
            src = sorted(cands)[0]

    if not src or not os.path.exists(src):
        return jsonify({"error": "产物文件不存在"}), 404

    return send_file(
        src,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{os.path.splitext(job.get('filename', 'output'))[0]}.zh.{variant or 'mono'}.pdf",
    )


@app.route("/v1/pdf/jobs", methods=["GET"])
def pdf_jobs():
    """历史任务列表（新→旧），含每个任务的产物清单，供网页管理。"""
    items = []
    for jid, j in sorted(PDF_JOBS.items(), key=lambda kv: kv[1].get("created_at", 0), reverse=True):
        results = []
        if j.get("status") == "completed" and j.get("workdir"):
            import glob as _glob
            for p in sorted(_glob.glob(os.path.join(j["workdir"], "*.pdf"))):
                name = os.path.basename(p)
                if "watermarked" in name:
                    continue
                if name.endswith(".dual.pdf"):
                    results.append({"variant": "dual", "name": name})
                elif name.endswith(".mono.pdf"):
                    results.append({"variant": "mono", "name": name})
        items.append({
            "id": jid,
            "filename": j.get("filename"),
            "status": j.get("status"),
            "progress": j.get("progress"),
            "error": j.get("error"),
            "mode": j.get("mode"),
            "pages": j.get("pages") or "",
            "created_at": j.get("created_at"),
            "results": results,
        })
    return jsonify(items)


@app.route("/v1/pdf/jobs/<job_id>", methods=["DELETE"])
def pdf_jobs_delete(job_id):
    job = PDF_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    # 删任务记录 + 删工作目录（源 PDF 与产物一起清掉）
    workdir = job.get("workdir")
    PDF_JOBS.pop(job_id, None)
    _pdf_jobs_save()
    if workdir and os.path.isdir(workdir) and os.path.basename(workdir) == job_id:
        try:
            shutil.rmtree(workdir)
        except Exception as e:
            logger.warning(f"删除任务目录失败 {workdir}: {e}")
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# 轻量文档翻译（TXT / SRT / ASS / EPUB）
#
# 与 PDF 管线并列的第二种文件翻译路径：不依赖 BabelDOC，
# 纯标准库解析 + 复用 _infer_translate 逐段推理。
#   TXT  -> 双语 txt（原文行 + 译文行）
#   SRT  -> 双语 SRT（原文行 + 译文行，时间轴不变）
#   ASS  -> 双语 ASS（每条 Dialogue 拆成原文/译文两行）
#   EPUB -> 双语 EPUB（原文保留，译文以 <p class="hy-mt-tr"> 追加；mono 则替换文本）
# --------------------------------------------------------------------------

# 文档翻译中需要做逐段翻译的 HTML 块级标签（与整页翻译的段落选择一致）
_DOC_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"}


def _doc_split_paragraphs(text):
    """TXT：按空行分段。"""
    paras = []
    for block in re.split(r"\n\s*\n", text or ""):
        block = block.strip()
        if block:
            paras.append(block)
    return paras


def _doc_srt_ms(h, m, s, ms):
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def _doc_srt_fmt(ms):
    ms = max(0, int(round(ms)))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def _doc_parse_srt(text):
    """SRT -> [{start_ms, end_ms, text}]。"""
    cues = []
    for block in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        m = re.match(
            r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)",
            lines[1],
        )
        if not m:
            continue
        start = _doc_srt_ms(*map(int, m.groups()[:4]))
        end = _doc_srt_ms(*map(int, m.groups()[4:]))
        text = " ".join(lines[2:]).strip()
        if not text:
            continue
        cues.append({"start": start, "end": end, "text": text})
    return cues


def _doc_ass_ms(t):
    """ASS 时间 0:00:01.00 -> 毫秒（两位小数为百分秒）。"""
    try:
        h, m, rest = t.split(":")
        s, cs = rest.split(".")
        return (int(h) * 60 + int(m)) * 60000 + int(s) * 1000 + int(cs) * 10
    except Exception:
        return 0


def _doc_ass_fmt(ms):
    ms = max(0, int(round(ms)))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, milli = divmod(rem, 1000)
    return f"{h}:{m:02d}:{s:02d}.{milli // 10:02d}"


def _doc_parse_ass(text):
    """ASS -> [{start_ms, end_ms, text, prefix}]（prefix 为 Dialogue 前 9 字段，组装时复用）。"""
    cues = []
    in_events = False
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            in_events = s.lower() == "[events]"
            continue
        if not in_events or not s.startswith("Dialogue:"):
            continue
        # Dialogue: Marked=0,0:00:01.00,0:00:02.00,Default,,0,0,0,,正文
        parts = s.split(",", 9)
        if len(parts) < 10:
            continue
        body = parts[9].strip()
        if not body:
            continue
        cues.append({
            "start": _doc_ass_ms(parts[1].strip()),
            "end": _doc_ass_ms(parts[2].strip()),
            "text": body,
            "prefix": ",".join(parts[:9]) + ",",
        })
    return cues


def _doc_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


class _EpubBilingualizer(HTMLParser):
    """把 EPUB 的 xhtml 流式重写为双语版本。

    - dual：块内原文原样保留，块结束后追加 <p class="hy-mt-tr">译文</p>
    - mono：块内文本替换为译文（结构/标签保留）
    script/style 内容跳过，注释与自闭合标签原样透传。
    """

    def __init__(self, translate, mono):
        super().__init__(convert_charrefs=True)
        self.translate = translate
        self.mono = mono
        self.out = []
        self._depth = 0      # 当前块级嵌套深度（0=不在块内）
        self._buf = []
        self._skip = 0       # script/style 嵌套深度

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if self._skip:
            return
        self.out.append(self.get_starttag_text() or f"<{tag}>")
        if tag in _DOC_BLOCK_TAGS:
            self._depth += 1
            self._buf = []

    def handle_startendtag(self, tag, attrs):
        if not self._skip:
            self.out.append(self.get_starttag_text() or f"<{tag}/>")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        if self._skip:
            return
        if tag in _DOC_BLOCK_TAGS and self._depth:
            text = " ".join("".join(self._buf).split()).strip()
            self._buf = []
            self._depth -= 1
            if text:
                tr = (self.translate(text) or text).strip()
                if self.mono:
                    self.out.append(_doc_escape(tr or text))
                elif tr and tr != text:
                    self.out.append(f'<p class="hy-mt-tr">{_doc_escape(tr)}</p>')
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._skip:
            return
        if self._depth:
            if not self.mono:
                self.out.append(_doc_escape(data))
            self._buf.append(data)
        else:
            self.out.append(_doc_escape(data))

    def handle_comment(self, data):
        if not self._skip:
            self.out.append(f"<!--{data}-->")


def _doc_translate_paras(paras, lang_out, progress_cb=None):
    """逐段翻译；同语言段落原样返回；单段失败保留原文不中断任务。"""
    results = []
    total = len(paras)
    for i, p in enumerate(paras, 1):
        p = (p or "").strip()
        if not p:
            results.append(p)
            continue
        if is_same_language(p, lang_out):
            results.append(p)
        else:
            try:
                results.append(_infer_translate(p, lang_out))
            except Exception as e:
                logger.warning(f"文档翻译单段失败，保留原文: {e}")
                results.append(p)
        if progress_cb and (i % 5 == 0 or i == total):
            progress_cb(i, total)
    return results


def _run_doc_job(job_id, input_path, lang_out):
    """文档翻译任务线程：解析 -> 逐段翻译 -> 组装产物。跑着的时候看门狗不许退出。"""
    global _active_jobs
    with _activity_lock:
        _active_jobs += 1
    try:
        job = DOC_JOBS[job_id]
        kind = job["kind"]
        mode = job["mode"]
        workdir = job["workdir"]
        stem = os.path.splitext(os.path.basename(input_path))[0]
        job["status"] = "processing"
        job["progress"] = "解析文件中…"

        def cb(done, total):
            job["progress"] = f"翻译中 {done}/{total} 段…"

        out_path = None

        if kind == "txt":
            with open(input_path, encoding="utf-8", errors="replace") as f:
                raw = f.read()
            paras = _doc_split_paragraphs(raw)
            translated = _doc_translate_paras(paras, lang_out, cb)
            lines = []
            for orig, tr in zip(paras, translated):
                if mode == "mono":
                    lines.append(tr if tr.strip() else orig)
                else:
                    lines.append(orig)
                    lines.append(tr)
                lines.append("")
            out_path = os.path.join(workdir, f"{stem}.{lang_out}.{mode}.txt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

        elif kind == "srt":
            with open(input_path, encoding="utf-8", errors="replace") as f:
                raw = f.read()
            cues = _doc_parse_srt(raw)
            if not cues:
                raise ValueError("SRT 中未解析到任何字幕块")
            translated = _doc_translate_paras([c["text"] for c in cues], lang_out, cb)
            lines = []
            for i, (c, tr) in enumerate(zip(cues, translated), 1):
                lines.append(str(i))
                lines.append(f"{_doc_srt_fmt(c['start'])} --> {_doc_srt_fmt(c['end'])}")
                if mode == "mono":
                    lines.append(tr if tr.strip() else c["text"])
                else:
                    lines.append(c["text"])
                    if tr.strip():
                        lines.append(tr)
                lines.append("")
            out_path = os.path.join(workdir, f"{stem}.{lang_out}.{mode}.srt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

        elif kind == "ass":
            with open(input_path, encoding="utf-8", errors="replace") as f:
                raw = f.read()
            cues = _doc_parse_ass(raw)
            if not cues:
                raise ValueError("ASS 中未解析到任何 Dialogue 行")
            translated = _doc_translate_paras([c["text"] for c in cues], lang_out, cb)
            lines = []
            for c, tr in zip(cues, translated):
                body = tr if tr.strip() else c["text"]
                if mode == "mono":
                    lines.append(c["prefix"] + body)
                else:
                    lines.append(c["prefix"] + c["text"])
                    lines.append(c["prefix"] + body)
            out_path = os.path.join(workdir, f"{stem}.{lang_out}.{mode}.ass")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

        elif kind == "epub":
            out_path = _doc_epub_translate(input_path, job, lang_out, mode)

        job["status"] = "completed"
        job["progress"] = "完成"
        job["result"] = out_path
        logger.info(f"[{job_id}] 文档翻译输出: {out_path}")
    except Exception as e:
        logger.error(f"[{job_id}] 文档翻译异常: {e}")
        job["status"] = "failed"
        job["error"] = str(e)
    finally:
        with _activity_lock:
            _active_jobs -= 1


def _doc_epub_translate(src_path, job, lang_out, mode):
    """解包 EPUB -> 逐 xhtml 双语重写 -> 重打包（mimetype 保持第一项且不压缩）。"""
    import zipfile as _z

    job["progress"] = "解析 EPUB…"
    with _z.ZipFile(src_path) as z:
        html_names = [
            i.filename for i in z.infolist()
            if i.filename.lower().endswith((".xhtml", ".html", ".htm"))
        ]
    if not html_names:
        raise ValueError("EPUB 中未找到任何 xhtml/html 内容文件")

    # 粗略预扫段落数（用于进度显示），script/style 内不计
    paras_total = 0
    for name in html_names:
        with _z.ZipFile(src_path) as z:
            data = z.read(name)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        paras_total += len(re.findall(r"<(?:p|li|blockquote|h[1-6])[\s>]", text, re.I))

    done = [0]

    def translate(text):
        if is_same_language(text, lang_out):
            return text
        try:
            tr = _infer_translate(text, lang_out)
        except Exception as e:
            logger.warning(f"EPUB 单段失败，保留原文: {e}")
            return text
        done[0] += 1
        if done[0] % 5 == 0 or done[0] >= max(paras_total, 1):
            job["progress"] = f"翻译中 {done[0]}/{max(paras_total, 1)} 段…"
        return tr or text

    translated_files = {}
    for name in html_names:
        with _z.ZipFile(src_path) as z:
            data = z.read(name)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        parser = _EpubBilingualizer(translate, mode == "mono")
        try:
            parser.feed(text)
            parser.close()
        except Exception as e:
            logger.warning(f"EPUB 文件重写失败，保持原文 {name}: {e}")
            continue
        translated_files[name] = "".join(parser.out).encode("utf-8")

    stem = os.path.splitext(os.path.basename(src_path))[0]
    out_path = os.path.join(job["workdir"], f"{stem}.{lang_out}.{mode}.epub")

    # 重打包：mimetype 必须是第一项且不压缩（EPUB 规范）
    with _z.ZipFile(src_path) as z:
        mt = z.read("mimetype")
    with _z.ZipFile(out_path, "w") as zout:
        zout.writestr("mimetype", mt, compress_type=_z.ZIP_STORED)
        with _z.ZipFile(src_path) as z:
            for info in z.infolist():
                if info.filename == "mimetype":
                    continue
                data = translated_files.get(info.filename, z.read(info.filename))
                zout.writestr(info, data)
    return out_path


@app.route("/v1/doc/translate", methods=["POST"])
def doc_translate():
    """
    上传 TXT / SRT / ASS / EPUB 并翻译（任务式，同 PDF 流程）。

    form 字段：file（必填）、lang_out（可选，默认配置）、mode（mono|dual）
    """
    if llm is None:
        return jsonify({"error": "Model not loaded"}), 503
    if "file" not in request.files:
        return jsonify({"error": "缺少 file 字段"}), 400
    uploaded = request.files["file"]
    if not uploaded.filename:
        return jsonify({"error": "文件名为空"}), 400

    # 先剥离路径穿越风险，再从净化后的文件名取扩展名
    safe_name = _safe_upload_name(uploaded.filename)
    ext = os.path.splitext(safe_name)[1].lower().lstrip(".")
    if ext not in ("txt", "srt", "ass", "epub"):
        return jsonify({"error": f"暂不支持的文件类型 .{ext}（支持 txt / srt / ass / epub）"}), 400

    lang_out = request.form.get("lang_out") or user_config.get("target_lang", "zh")
    mode = request.form.get("mode", "dual")
    if mode not in ("mono", "dual"):
        mode = "dual"

    job_id = str(uuid.uuid4())
    workdir = os.path.join(DOC_JOBS_DIR, job_id)
    os.makedirs(workdir, exist_ok=True)
    input_path = os.path.join(workdir, safe_name)
    uploaded.save(input_path)

    DOC_JOBS[job_id] = {
        "status": "pending",
        "progress": "排队中",
        "filename": safe_name,
        "kind": ext,
        "mode": mode,
        "workdir": workdir,
        "created_at": time.time(),
    }

    t = threading.Thread(
        target=_run_doc_job,
        args=(job_id, input_path, lang_out),
        daemon=True,
    )
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/v1/doc/status/<job_id>", methods=["GET"])
def doc_status(job_id):
    job = DOC_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({
        "status": job.get("status"),
        "progress": job.get("progress"),
        "filename": job.get("filename"),
        "error": job.get("error"),
    })


@app.route("/v1/doc/download/<job_id>", methods=["GET"])
def doc_download(job_id):
    job = DOC_JOBS.get(job_id)
    if not job or job.get("status") != "completed" or not job.get("result"):
        return jsonify({"error": "任务未完成"}), 404
    src = job["result"]
    if not os.path.exists(src):
        return jsonify({"error": "产物文件不存在"}), 404
    return send_file(
        src,
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=os.path.basename(src),
    )


# --------------------------------------------------------------------------
# Web 页面
# --------------------------------------------------------------------------

PDF_PAGE = _load_pdf_page()


@app.route("/", methods=["GET"])
@app.route("/pdf", methods=["GET"])
def index():
    return Response(PDF_PAGE, mimetype="text/html")


# --------------------------------------------------------------------------

PORT = int(os.getenv("HYMT_PORT", "18770"))


def main():
    logger.info("=" * 60)
    logger.info("统一翻译服务 (Hy-MT2 + BabelDOC)")
    logger.info("=" * 60)

    _pdf_jobs_load()  # 恢复历史任务记录
    _uploads_load()
    _uploads_sweep()  # 清掉 24 小时前的上传件
    _jobs_sweep()     # 清掉过期历史翻译任务，避免工作目录堆积
    os.makedirs(DOC_JOBS_DIR, exist_ok=True)  # 轻量文档翻译工作目录

    if not load_gguf_model():
        logger.error("模型加载失败，服务终止")
        sys.exit(1)

    logger.info(f"监听地址: http://127.0.0.1:{PORT}")
    logger.info("  /v1/translate       网页 + 字幕翻译")
    logger.info("  /v1/pdf/toc         PDF 目录提取（勾选文章用）")
    logger.info("  /v1/pdf/translate   PDF 版面还原翻译")
    logger.info("  /v1/doc/translate   TXT/SRT/ASS/EPUB 轻量文档翻译")
    logger.info(f"  /pdf                文档翻译页  ->  http://localhost:{PORT}/pdf")

    if IDLE_EXIT_MIN > 0:
        logger.info(f"空闲 {IDLE_EXIT_MIN:.0f} 分钟自动退出（HYMT_IDLE_EXIT=0 可关闭）")
    else:
        logger.info("空闲自动退出已关闭（常驻模式）")
    # watchdog 常驻线程：内部每轮动态读取 IDLE_EXIT_MIN，
    # 支持面板在运行时于「省电 / 常驻」之间切换
    threading.Thread(target=_idle_watchdog, daemon=True).start()
    # 每小时清理一次过期历史翻译任务，防磁盘堆积
    threading.Thread(target=_jobs_sweep_loop, daemon=True).start()

    app.run(host="127.0.0.1", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
