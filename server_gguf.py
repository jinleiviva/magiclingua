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

from flask import Flask, Response, jsonify, request, send_file
from flask_cors import CORS
from llama_cpp import Llama

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
PDF_JOBS = {}
PDF_JOBS_FILE = os.path.join(BASE_DIR, "pdf_jobs", "jobs.json")

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
# --------------------------------------------------------------------------

IDLE_EXIT_MIN = float(os.getenv("HYMT_IDLE_EXIT", "20"))
START_TIME = time.time()

_last_activity = time.time()
_activity_lock = threading.Lock()
_active_jobs = 0

# 探活/查状态类请求不算「使用」，否则插件开着面板就永远退不出去
_NO_TOUCH_PATHS = ("/health", "/v1/status", "/v1/models", "/v1/config")


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


def _idle_watchdog():
    if IDLE_EXIT_MIN <= 0:
        logger.info("空闲自动退出已关闭（HYMT_IDLE_EXIT=0），服务常驻")
        return

    limit = IDLE_EXIT_MIN * 60
    while True:
        time.sleep(30)
        with _activity_lock:
            idle = time.time() - _last_activity
            busy = _active_jobs > 0
        if busy or idle < limit:
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
        llm = Llama(
            model_path=model_path,
            n_ctx=8192,
            n_threads=4,
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
    "source_lang": "auto",
    "target_lang": "zh",
    "font_size": 28,
    "theme_mode": "dark",
    "display_mode": "append",
    "bilingual_subtitle": False,
    "stream_output": True,
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


# --------------------------------------------------------------------------
# Prompt 构造（统一收在服务端，三个场景共用）
# --------------------------------------------------------------------------

LANG_NAMES = {
    "zh": "Chinese", "zh-cn": "Chinese", "zh-CN": "Chinese",
    "en": "English", "en-us": "English",
    "ja": "Japanese", "ko": "Korean", "fr": "French",
    "de": "German", "es": "Spanish", "ru": "Russian",
}

# HY-MT 官方模板用的目标语言中文名
LANG_NAMES_ZH = {
    "zh": "中文", "zh-cn": "中文", "zh-cn": "中文",
    "en": "英文", "en-us": "英文",
    "ja": "日文", "ko": "韩文", "fr": "法文",
    "de": "德文", "es": "西班牙文", "ru": "俄文",
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
        lines = [f"- {k} = {v}" for k, v in list(glossary.items())[:40]]
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

    prompt = build_translate_prompt(text, target_lang, context, glossary)
    started = time.time()

    if stream:
        return Response(
            stream_translation(prompt),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        with inference_lock:
            response = llm(
                prompt,
                max_tokens=data.get("max_tokens", 512),
                temperature=0.3,
                top_p=0.6,
                top_k=20,
                repeat_penalty=1.05,
                echo=False,
                stop=ECHO_STOP,
            )
        raw = response["choices"][0]["text"]
        translation = clean_translation(raw)
        elapsed = time.time() - started

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
                return jsonify({"error": "translation_garbled", "elapsed": round(time.time() - started, 3)}), 502

        logger.info(f"翻译完成 {elapsed:.2f}s ({len(text)} -> {len(translation)} 字符)")
        return jsonify({
            "translation": translation,
            "skipped": False,
            "elapsed": round(elapsed, 3),
        })
    except Exception as e:
        logger.error(f"翻译失败: {e}")
        return jsonify({"error": str(e)}), 500


def stream_translation(prompt):
    """SSE 流式输出，YouTube 字幕逐字显示。"""
    def generate():
        try:
            with inference_lock:
                for chunk in llm(
                    prompt,
                    max_tokens=512,
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
                max_tokens=data.get("max_tokens", 2048),
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
                    max_tokens=data.get("max_tokens", 2048),
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

    try:
        from pdf_toc import extract_toc
    except ImportError as e:
        return jsonify({"error": f"目录提取模块不可用: {e}"}), 500

    upload_id = str(uuid.uuid4())
    udir = os.path.join(UPLOAD_DIR, upload_id)
    os.makedirs(udir, exist_ok=True)
    path = os.path.join(udir, uploaded.filename)
    uploaded.save(path)

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
        "filename": uploaded.filename,
        "sha256": digest,
        "page_count": result.get("page_count"),
        "created_at": time.time(),
    }
    _uploads_save()

    return jsonify({
        "upload_id": upload_id,
        "filename": uploaded.filename,
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
        src_file, filename = None, uploaded.filename
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
# Web 页面
# --------------------------------------------------------------------------

PDF_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MagicLingua</title>
<style>
  * { box-sizing: border-box; }
  :root {
    --canvas:#ffffff; --surface:#f7f8fa; --ink:#1c1c1e; --ink-soft:#555a6a; --ink-muted:#8e91a0;
    --line:#e0e2e8; --line-soft:#eef0f3;
    --yellow:#ffd02f; --yellow-light:#fff4c4; --surface-yellow:#fff8e0; --yellow-dark:#746019; --yellow-line:#ffe9b0;
    --blue:#4262ff; --teal:#0fbcb0; --teal-light:#c3faf5; --teal-dark:#0a8a82; --coral:#c0392b;
  }
  body {
    margin: 0; padding: 24px 20px 88px;
    font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: var(--surface); color: var(--ink); line-height: 1.6;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1240px; margin: 0 auto; }

  /* 顶栏：品牌标识 + 标题，右侧进度区 */
  .topbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; flex-wrap: wrap; margin-bottom: 18px; }
  .brand { display: flex; align-items: center; gap: 12px; flex: 1; min-width: 260px; }
  .logo {
    width: 34px; height: 34px; border-radius: 9px; background: var(--yellow);
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  }
  .brand h1 { font-size: 22px; font-weight: 600; margin: 0 0 2px; letter-spacing: -.2px; }
  .sub { color: var(--ink-muted); font-size: 13px; margin: 0; }
  .topbar .status { flex: 1; min-width: 260px; max-width: 440px; margin-top: 0; }

  /* 翻译进度 */
  .status { display: none; margin-top: 14px; font-size: 13px; color: var(--ink-soft); }
  .status.show { display: block; }
  .bar { height: 6px; background: var(--line-soft); border-radius: 3px; overflow: hidden; margin-top: 8px; }
  .bar > div { height: 100%; background: #1c1c1e; width: 30%; transition: width .3s; border-radius: 3px; }
  .log {
    font-size: 12px; color: var(--ink-muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    margin-top: 8px; word-break: break-all; max-height: 90px; overflow-y: auto;
  }
  .ok { color: var(--teal-dark); } .err { color: var(--coral); }

  /* 卡片 */
  .card {
    background: var(--canvas); border: 1px solid var(--line-soft);
    border-radius: 16px; padding: 24px; margin-bottom: 16px;
  }
  h2 { font-size: 15px; font-weight: 600; margin: 0 0 14px;
       display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }

  .file-chip { display: flex; align-items: center; gap: 10px; font-size: 13px; color: var(--ink-soft); min-width: 0; }
  .file-chip b { font-weight: 600; color: var(--ink); word-break: break-all; }
  .card.compact { padding: 14px 20px; margin-bottom: 12px; }
  .card.compact h2 { margin-bottom: 10px; }
  .card.compact .drop { display: none; }
  .card.compact .row { margin-top: 10px; }

  /* 拖放区 */
  .drop {
    border: 2px dashed var(--line); border-radius: 16px; padding: 44px 20px; text-align: center;
    cursor: pointer; transition: all .18s; background: var(--surface);
  }
  .drop strong { font-size: 15px; color: var(--ink); }
  .drop:hover, .drop.over { border-color: var(--yellow); background: var(--surface-yellow); }
  .drop p { margin: 8px 0 0; color: var(--ink-muted); font-size: 13px; }

  .row { display: flex; gap: 12px; margin-top: 16px; align-items: center; flex-wrap: wrap; }
  input[type=text] {
    flex: 1; min-width: 140px; padding: 11px 14px; border: 1px solid var(--line);
    border-radius: 10px; font-size: 14px; font-family: inherit; background: var(--canvas); color: var(--ink);
  }
  input[type=text]:focus { outline: none; border-color: var(--blue); }

  /* 主行动：黑色药丸（Miro 标志性） */
  button {
    padding: 12px 22px; border: none; border-radius: 999px; background: #1c1c1e; color: #fff;
    font-size: 14px; font-weight: 500; cursor: pointer; font-family: inherit;
    transition: opacity .15s ease, transform .1s ease;
  }
  button:hover { opacity: .88; }
  button:active { transform: scale(.99); }
  button:disabled { background: var(--line); color: var(--ink-muted); cursor: not-allowed; }

  .inline-check { display: inline-flex; align-items: center; gap: 8px; font-size: 14px; color: var(--ink-soft); cursor: pointer; user-select: none; }
  .inline-check input[type=checkbox] { width: 18px; height: 18px; accent-color: #1c1c1e; cursor: pointer; }

  /* 双栏工作区 */
  .work { display: none; gap: 16px; align-items: flex-start; margin-bottom: 16px; }
  .work.show { display: flex; }
  .pane { flex: 1; min-width: 0; }
  .pane-preview {
    width: 380px; flex-shrink: 0; position: sticky; top: 20px; background: var(--canvas);
    border: 1px solid var(--line-soft); border-radius: 16px; padding: 16px;
  }
  .pv-head { font-size: 14px; font-weight: 600; margin-bottom: 10px; }
  .pv-box {
    height: min(620px, calc(100vh - 260px)); min-height: 300px;
    display: flex; align-items: center; justify-content: center;
    background: var(--surface); border: 1px solid var(--line-soft); border-radius: 12px; overflow: hidden;
  }
  .pv-box img { max-width: 100%; max-height: 100%; object-fit: contain; box-shadow: 0 4px 20px rgba(0,0,0,.12); }
  .pv-meta { font-size: 12.5px; color: var(--ink-muted); margin-top: 10px; line-height: 1.5; min-height: 34px; }

  /* 文章目录 */
  .toc-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
  .toc-head strong { font-size: 15px; font-weight: 600; }
  .toc-actions { display: flex; gap: 8px; }
  .toc-list {
    max-height: calc(100vh - 380px); min-height: 320px; overflow-y: auto;
    border: 1px solid var(--line-soft); border-radius: 12px; background: var(--canvas);
  }
  .toc-item {
    display: flex; align-items: flex-start; gap: 12px; padding: 10px 14px;
    border-bottom: 1px solid var(--line-soft); font-size: 14px; cursor: pointer; transition: background .12s;
  }
  .toc-item:last-child { border-bottom: none; }
  .toc-item:hover { background: var(--yellow-light); }
  .toc-item:has(input:checked) { background: var(--yellow-light); }
  .toc-item input[type=checkbox] { width: 16px; height: 16px; margin-top: 3px; accent-color: #1c1c1e; cursor: pointer; flex-shrink: 0; }
  .thumb {
    width: 148px; height: 104px; object-fit: cover; object-position: top center; flex-shrink: 0;
    border: 1px solid var(--line-soft); border-radius: 8px; background: var(--surface); cursor: zoom-in;
  }
  .thumb:hover { border-color: var(--yellow); }
  .toc-body { flex: 1; min-width: 0; display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
  .toc-main { flex: 1; min-width: 0; }
  .toc-page { font-size: 12.5px; color: var(--ink-muted); white-space: nowrap; flex-shrink: 0; padding-top: 1px; }
  .toc-title { display: block; word-break: break-word; line-height: 1.45; font-weight: 500; }
  .toc-title.low { color: var(--ink-muted); font-weight: 400; }
  .toc-meta { display: block; font-size: 12px; color: var(--ink-muted); margin-top: 2px; }
  .tag {
    display: inline-block; font-size: 11px; padding: 1px 8px; border-radius: 999px;
    background: var(--surface-yellow); color: var(--yellow-dark); margin-left: 6px; vertical-align: 1px;
  }
  .toc-foot { font-size: 12.5px; color: var(--ink-muted); margin-top: 10px; }
  .toc-warn {
    font-size: 13px; color: var(--yellow-dark); background: var(--surface-yellow);
    border: 1px solid var(--yellow-line); border-radius: 10px; padding: 9px 12px; margin-bottom: 12px;
  }

  /* 底部常驻操作条 */
  .actionbar {
    position: fixed; bottom: 0; left: 0; right: 0; z-index: 50;
    background: var(--canvas); border-top: 1px solid var(--line-soft);
    padding: 12px 20px; box-shadow: 0 -4px 20px rgba(0,0,0,.06);
  }
  .ab-inner { max-width: 1240px; margin: 0 auto; display: flex; align-items: center; gap: 14px; }
  .ab-info { flex: 1; font-size: 14px; color: var(--ink-soft); }

  .cols { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; align-items: start; }

  /* 历史任务 */
  .job { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding: 11px 0; border-bottom: 1px solid var(--line-soft); }
  .job:last-child { border-bottom: none; }
  .job-main { flex: 1; min-width: 200px; }
  .job-name { font-size: 14px; word-break: break-all; font-weight: 500; }
  .job-meta { font-size: 12px; color: var(--ink-muted); margin-top: 2px; }
  .badge { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 11.5px; font-weight: 500; white-space: nowrap; }
  .badge-done { background: var(--teal-light); color: var(--teal-dark); }
  .badge-run  { background: var(--surface-yellow); color: var(--yellow-dark); }
  .badge-err  { background: #ffe5e5; color: var(--coral); }

  .btn-ghost {
    background: var(--canvas); color: var(--ink-soft); border: 1px solid var(--line);
    padding: 7px 14px; font-size: 13px; border-radius: 999px;
  }
  .btn-ghost:hover { background: var(--surface); }
  .btn-danger { background: var(--canvas); color: var(--coral); border: 1px solid #f0c8c8; padding: 7px 14px; font-size: 13px; border-radius: 999px; }
  .btn-danger:hover { background: #fff5f5; }
  .empty { color: var(--ink-muted); font-size: 13px; padding: 14px; text-align: center; }

  ul { margin: 0; padding-left: 20px; color: var(--ink-soft); font-size: 13.5px; }
  li { margin-bottom: 6px; }
  code { background: var(--surface); padding: 2px 6px; border-radius: 6px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }

  @media (max-width: 1080px) {
    .work.show { display: block; }
    .pane-preview { display: none; }
    .cols { grid-template-columns: 1fr; }
  }
  @media (max-width: 640px) {
    body { padding: 16px 12px 84px; }
    .card { padding: 16px; }
    .drop { padding: 32px 16px; }
    .row { flex-direction: column; align-items: stretch; }
    .row button { width: 100%; }
    .toc-item { gap: 10px; }
    .thumb { width: 92px; height: 68px; }
    .ab-inner { flex-wrap: wrap; }
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">
      <span class="logo">
        <svg width="34" height="34" viewBox="0 0 34 34" fill="none" aria-hidden="true">
          <path d="M9 12H19M14 12V22M14 12L9 22M14 12L19 22" stroke="#1C1C1E" stroke-width="2"
            stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </span>
      <div>
        <h1>MagicLingua</h1>
        <div class="sub">本地 Hy-MT2 1.8B · 数据不出本机</div>
      </div>
    </div>
    <div class="status" id="status">
      <div id="statusText"></div>
      <div class="bar"><div id="bar"></div></div>
      <div class="log" id="log"></div>
    </div>
  </div>

  <div class="card" id="uploadCard">
    <h2>
      <span>PDF 翻译（保留原版面）</span>
      <span class="file-chip" id="fileChip" style="display:none">
        <b id="fileName"></b>
        <button class="btn-ghost" id="rechoose">换一个文件</button>
      </span>
    </h2>
    <div class="drop" id="drop">
      <strong>点击选择 PDF</strong>
      <p>或把文件拖到这里 · 引擎 BabelDOC</p>
    </div>
    <input type="file" id="file" accept=".pdf" style="display:none">
    <div class="row" id="pagesRow">
      <input type="text" id="pages" placeholder="页码范围，留空翻译全部（如 1,3-5）">
      <label class="inline-check"><input type="checkbox" id="bilingual"> 对照阅读</label>
      <button id="start" disabled>开始翻译</button>
    </div>
  </div>

  <div class="work" id="tocBox">
    <section class="card pane">
      <div class="toc-head">
        <strong id="tocTitle">文章目录</strong>
        <div class="toc-actions">
          <button class="btn-ghost" id="selAll">全选</button>
          <button class="btn-ghost" id="selNone">清空</button>
        </div>
      </div>
      <div class="toc-warn" id="tocWarn" style="display:none"></div>
      <div class="toc-list" id="tocList"></div>
      <div class="toc-foot" id="tocFoot"></div>
    </section>

    <aside class="pane-preview">
      <div class="pv-head">页面预览</div>
      <div class="pv-box"><img id="pvImg" alt="页面预览"></div>
      <div class="pv-meta" id="pvMeta">鼠标移到左侧条目即可在此预览；点缩略图看大图。</div>
    </aside>
  </div>

  <div class="cols">
    <div class="card" id="historyCard" style="display:none">
      <h2>历史任务</h2>
      <div id="jobs"></div>
    </div>

    <div class="card">
      <h2>网页与视频翻译</h2>
      <ul>
        <li>看新闻、看 YouTube：由 Chrome 扩展自动完成，无需在此操作</li>
        <li>扩展未安装时，到 <code>chrome://extensions</code> 加载 <code>extension/</code> 目录</li>
      </ul>
    </div>
  </div>
</div>

<div class="actionbar" id="actionBar" style="display:none">
  <div class="ab-inner">
    <div class="ab-info" id="abInfo">未选择任何文章</div>
    <button id="start2">开始翻译</button>
  </div>
</div>

<div id="lb" onclick="closeLb()" style="display:none;position:fixed;inset:0;z-index:99;background:rgba(0,0,0,.76);overflow:auto;cursor:zoom-out">
  <div style="position:fixed;top:14px;left:0;right:0;text-align:center;color:#fff;font-size:13px;z-index:100;pointer-events:none">
    <span id="lbInfo"></span>
    <span style="opacity:.65;margin-left:14px">点图片切换 适应窗口 / 原始尺寸 · ESC 关闭</span>
  </div>
  <div style="min-height:100%;display:flex;align-items:center;justify-content:center;padding:56px 16px">
    <img id="lbImg" alt="页面预览" onclick="toggleZoom(event)"
         style="max-width:100%;max-height:86vh;cursor:zoom-in;border-radius:6px;background:#fff;box-shadow:0 10px 40px rgba(0,0,0,.5)">
  </div>
</div>

<script>
const drop = document.getElementById('drop');
const fileInput = document.getElementById('file');
const uploadCard = document.getElementById('uploadCard');
const fileChip = document.getElementById('fileChip');
const fileName = document.getElementById('fileName');
const rechooseBtn = document.getElementById('rechoose');
const startBtn = document.getElementById('start');
const statusBox = document.getElementById('status');
const statusText = document.getElementById('statusText');
const bar = document.getElementById('bar');
const logEl = document.getElementById('log');
const pagesInput = document.getElementById('pages');
const bilingualCheck = document.getElementById('bilingual');
const tocBox = document.getElementById('tocBox');
const tocTitle = document.getElementById('tocTitle');
const tocList = document.getElementById('tocList');
const tocFoot = document.getElementById('tocFoot');
const tocWarn = document.getElementById('tocWarn');
const selAllBtn = document.getElementById('selAll');
const selNoneBtn = document.getElementById('selNone');
const pvImg = document.getElementById('pvImg');
const pvMeta = document.getElementById('pvMeta');
const actionBar = document.getElementById('actionBar');
const abInfo = document.getElementById('abInfo');
const startBtn2 = document.getElementById('start2');

// 扩展 LocalBridge 会把 chrome.storage.sync.pdfBilingual 写到 localStorage，
// 装上扩展的用户这里的开关会自动同步；没装扩展时永远是 false。
try {
  const raw = localStorage.getItem('hy_mt_pdf_bilingual');
  if (raw) bilingualCheck.checked = JSON.parse(raw);
} catch (e) { /* 隐私模式静默 */ }

let selected = null;
let tocData = null;      // /v1/pdf/toc 的返回，含 upload_id 与文章清单
let checked = new Set(); // 已勾选的文章下标

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function updateStartLabel() {
  let label = '开始翻译', info = '未选择任何文章';
  if (selected && tocData && checked.size > 0) {
    let n = 0;
    checked.forEach(i => { n += tocData.articles[i].pages; });
    label = `翻译已选 ${checked.size} 篇 / ${n} 页`;
    info = `已选 ${checked.size} / ${tocData.articles.length} 篇 · 共 ${n} 页`;
  } else if (selected && tocData && tocData.page_count) {
    label = `翻译全部（${tocData.page_count} 页）`;
    info = `未勾选，将翻译整本 ${tocData.page_count} 页`;
  }
  startBtn.textContent = label;
  startBtn2.textContent = label;
  abInfo.textContent = info;
}

// 悬停即在右侧大图预览，省去逐个点开
let pvTimer = null;
function previewPage(page, title) {
  clearTimeout(pvTimer);
  pvTimer = setTimeout(() => {
    pvImg.src = `/v1/pdf/thumb/${tocData.upload_id}/${page}?w=760`;
    pvMeta.textContent = `第 ${page} 页 · ${title}`;
  }, 110);
}

function renderToc() {
  const arts = tocData.articles || [];
  tocTitle.textContent = `文章目录 · ${arts.length} 篇 / 共 ${tocData.page_count} 页`;

  if (tocData.warnings && tocData.warnings.length) {
    tocWarn.style.display = 'block';
    tocWarn.textContent = tocData.warnings.join('；');
  } else {
    tocWarn.style.display = 'none';
  }

  tocList.innerHTML = arts.map((a, i) => {
    const span = a.end > a.start ? `${a.start}-${a.end}` : `${a.start}`;
    const low = a.confidence !== 'high';
    const tag = (a.flags && a.flags.length) ? `<span class="tag">${esc(a.flags.join(','))}</span>` : '';
    const meta = a.pages > 1 ? `共 ${a.pages} 页` : '单页';
    return `<div class="toc-item" data-t="${esc(a.title)}"
           onmouseenter="previewPage(${a.start}, this.dataset.t)">
      <input type="checkbox" data-i="${i}" id="cb${i}">
      <img class="thumb" loading="lazy" alt="第 ${a.start} 页" data-t="${esc(a.title)}"
           src="/v1/pdf/thumb/${tocData.upload_id}/${a.start}?w=300"
           onclick="openLb(event, ${a.start}, this.dataset.t)">
      <span class="toc-body" onclick="document.getElementById('cb${i}').click()">
        <span class="toc-main">
          <span class="toc-title${low ? ' low' : ''}">${esc(a.title)}${tag}</span>
          <span class="toc-meta">${meta}${low ? ' · 标题为推测' : ''}</span>
        </span>
        <span class="toc-page">P.${span}</span>
      </span>
    </div>`;
  }).join('');

  tocList.querySelectorAll('input[data-i]').forEach(cb => {
    cb.onchange = () => {
      const i = Number(cb.dataset.i);
      if (cb.checked) checked.add(i); else checked.delete(i);
      selAllBtn.textContent = (checked.size === arts.length) ? '取消全选' : '全选';
      updateStartLabel();
    };
  });

  pagesInput.placeholder = '手工页码（填了会覆盖上方勾选，如 1,3-5）';
  tocFoot.textContent = '默认不勾选 · 点缩略图看大图 · 一篇都不勾直接翻译＝翻译整本。';
  updateStartLabel();
}

let lbZoomed = false;

function openLb(ev, page, title) {
  ev.stopPropagation();
  lbZoomed = false;
  const img = document.getElementById('lbImg');
  img.src = `/v1/pdf/thumb/${tocData.upload_id}/${page}?w=1200`;
  img.style.maxWidth = '100%';
  img.style.maxHeight = '86vh';
  img.style.cursor = 'zoom-in';
  document.getElementById('lbInfo').textContent = `第 ${page} 页 · ${title}`;
  document.getElementById('lb').style.display = 'block';
}

// 再点图片：适应窗口 <-> 原始尺寸（原始尺寸下能看清正文）
function toggleZoom(ev) {
  ev.stopPropagation();
  lbZoomed = !lbZoomed;
  const img = document.getElementById('lbImg');
  img.style.maxWidth = lbZoomed ? 'none' : '100%';
  img.style.maxHeight = lbZoomed ? 'none' : '86vh';
  img.style.cursor = lbZoomed ? 'zoom-out' : 'zoom-in';
}

function closeLb() { document.getElementById('lb').style.display = 'none'; }

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeLb();
});

function checkAll(v) {
  if (!tocData) return;
  checked.clear();
  if (v) tocData.articles.forEach((a, i) => checked.add(i));
  tocList.querySelectorAll('input[data-i]').forEach(cb => { cb.checked = v; });
  selAllBtn.textContent = v ? '取消全选' : '全选';
  updateStartLabel();
}

selAllBtn.onclick = () => checkAll(checked.size !== (tocData ? tocData.articles.length : 0));
selNoneBtn.onclick = () => checkAll(false);

drop.onclick = () => fileInput.click();
rechooseBtn.onclick = () => fileInput.click();
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => {
  e.preventDefault(); drop.classList.remove('over');
  if (e.dataTransfer.files[0]) pick(e.dataTransfer.files[0]);
};
fileInput.onchange = e => { if (e.target.files[0]) pick(e.target.files[0]); };

async function pick(f) {
  selected = f;
  tocData = null;
  checked.clear();
  fileName.textContent = f.name;
  fileChip.style.display = 'flex';
  uploadCard.classList.add('compact');   // 收起拖拽区，把高度让给目录
  startBtn.disabled = false;
  statusBox.classList.remove('show');
  selAllBtn.textContent = '全选';

  // 上传并解析目录（2-4 秒）。失败就退回手工页码，绝不阻塞用户。
  const fd = new FormData();
  fd.append('file', f);
  tocBox.classList.add('show');
  actionBar.style.display = 'block';
  tocTitle.textContent = '解析目录中…';
  tocWarn.style.display = 'none';
  tocList.innerHTML = '<div class="empty">正在识别文章，约 2-4 秒…</div>';
  tocFoot.textContent = '';
  try {
    const r = await fetch('/v1/pdf/toc', { method: 'POST', body: fd });
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    tocData = d;
    renderToc();
  } catch (e) {
    tocBox.classList.remove('show');
    actionBar.style.display = 'none';
    tocData = null;
  }
  updateStartLabel();
}

async function startTranslate() {
  if (!selected) return;
  const fd = new FormData();
  const manual = pagesInput.value.trim();

  if (tocData) {
    fd.append('upload_id', tocData.upload_id);
    if (manual) {
      fd.append('pages', manual);            // 手工页码优先，覆盖勾选
    } else if (checked.size > 0) {
      const sel = [...checked].sort((a, b) => a - b).map(i => {
        const a = tocData.articles[i];
        return { title: a.title, start: a.start, end: a.end };
      });
      fd.append('selection', JSON.stringify(sel));
    }
    // 两者都空 = 翻译整本
  } else {
    fd.append('file', selected);             // 目录不可用，走旧路径
    if (manual) fd.append('pages', manual);
  }
  fd.append('mode', bilingualCheck.checked ? 'dual' : 'mono');

  startBtn.disabled = startBtn2.disabled = true;
  statusBox.classList.add('show');
  statusText.textContent = '上传中...';
  bar.style.width = '15%';

  const res = await fetch('/v1/pdf/translate', { method: 'POST', body: fd });
  const { job_id } = await res.json();

  const timer = setInterval(async () => {
    const s = await (await fetch('/v1/pdf/status/' + job_id)).json();
    statusText.textContent = s.progress || s.status;
    logEl.textContent = s.progress || '';
    if (s.status === 'processing') bar.style.width = '60%';
    if (s.status === 'completed') {
      clearInterval(timer);
      bar.style.width = '100%';
      statusText.innerHTML = '<span class="ok">翻译完成，开始下载</span>';
      window.location.href = '/v1/pdf/download/' + job_id;
      startBtn.disabled = startBtn2.disabled = false;
      loadJobs();
    }
    if (s.status === 'failed') {
      clearInterval(timer);
      statusText.innerHTML = '<span class="err">失败：' + (s.error || '未知错误') + '</span>';
      startBtn.disabled = startBtn2.disabled = false;
      loadJobs();
    }
  }, 1500);
}

startBtn.onclick = startTranslate;
startBtn2.onclick = startTranslate;

// ---- 历史任务管理（下载 / 删除） ----
const historyCard = document.getElementById('historyCard');
const jobsBox = document.getElementById('jobs');

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function loadJobs() {
  try {
    const jobs = await (await fetch('/v1/pdf/jobs')).json();
    if (!jobs || !jobs.length) {
      historyCard.style.display = 'none';
      return;
    }
    historyCard.style.display = 'block';
    jobsBox.innerHTML = jobs.map(j => {
      const badge = j.status === 'completed'
        ? '<span class="badge badge-done">完成</span>'
        : (j.status === 'failed' ? '<span class="badge badge-err">失败</span>'
          : '<span class="badge badge-run">处理中</span>');
      const dl = j.results.map(r =>
        `<a class="btn-ghost" style="text-decoration:none;display:inline-block" href="/v1/pdf/download/${j.id}?variant=${r.variant}">${r.variant === 'mono' ? '下载译文' : '下载双语'}</a>`
      ).join(' ');
      return `
        <div class="job">
          <div class="job-main">
            <div class="job-name">${j.filename} ${badge}</div>
            <div class="job-meta">${fmtTime(j.created_at)}${j.pages ? ' · 页码 ' + j.pages : ''} · ${j.progress || ''}</div>
          </div>
          ${dl}
          <button class="btn-danger" onclick="delJob('${j.id}')">删除</button>
        </div>`;
    }).join('');
  } catch (e) { /* 服务未起等 */ }
}

async function delJob(id) {
  if (!confirm('删除该任务及产物文件？此操作不可恢复。')) return;
  await fetch('/v1/pdf/jobs/' + id, { method: 'DELETE' });
  loadJobs();
}

loadJobs();
</script>
</body>
</html>
"""


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

    if not load_gguf_model():
        logger.error("模型加载失败，服务终止")
        sys.exit(1)

    logger.info(f"监听地址: http://127.0.0.1:{PORT}")
    logger.info("  /v1/translate       网页 + 字幕翻译")
    logger.info("  /v1/pdf/toc         PDF 目录提取（勾选文章用）")
    logger.info("  /v1/pdf/translate   PDF 版面还原翻译")
    logger.info(f"  /pdf                上传页面  ->  http://localhost:{PORT}/pdf")

    if IDLE_EXIT_MIN > 0:
        logger.info(f"空闲 {IDLE_EXIT_MIN:.0f} 分钟自动退出（HYMT_IDLE_EXIT=0 可关闭）")
        threading.Thread(target=_idle_watchdog, daemon=True).start()

    app.run(host="127.0.0.1", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
