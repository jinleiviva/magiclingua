#!/bin/bash
# ============================================================================
# MagicLingua 一键安装脚本（macOS / Linux）
#
# 用法：
#   ./setup_env.sh                一键安装（venv + 引擎 + 依赖 + 模型 + 自启注册）
#   ./setup_env.sh --with-pdf     额外安装 PDF 翻译依赖（babeldoc 约 470MB）
#   ./setup_env.sh --skip-model   跳过模型下载（已手动放置模型时）
#   ./setup_env.sh --skip-host    跳过 launchd / 浏览器宿主注册（仅本地调试）
#
# Apple Silicon 会自动使用 llama-cpp-python 0.3.35 官方预编译 wheel：
#   官方 wheel 的 zip CRC 字段全错（打包 bug），直接 pip install 会报 BadZipFile。
#   本脚本自动完成「下载 → ditto 解压 → Python zipfile 重打包（修 CRC）→ 安装」，
#   全程免源码编译（源码编译需 7 分钟+，且易失败）。
# 其他平台回退到源码编译。
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"

# ---------------------------------------------------------------- 常量 -------
LLAMA_VER="0.3.35"
WHEEL_NAME="llama_cpp_python-${LLAMA_VER}-py3-none-macosx_11_0_arm64.whl"
WHEEL_URL="https://github.com/abetlen/llama-cpp-python/releases/download/v${LLAMA_VER}/${WHEEL_NAME}"
WHEEL_BYTES=18164782          # 官方 wheel 原始字节数（下载后校验用）
CACHE_DIR="$ROOT/.cache/wheels"

# ---------------------------------------------------------------- 参数 ------
WITH_PDF=0
SKIP_MODEL=0
SKIP_HOST=0
for arg in "$@"; do
    case "$arg" in
        --with-pdf)   WITH_PDF=1 ;;
        --skip-model) SKIP_MODEL=1 ;;
        --skip-host)  SKIP_HOST=1 ;;
        -h|--help)
            sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "未知参数: ${arg}（--help 查看用法）"; exit 1 ;;
    esac
done

# ---------------------------------------------------------------- 输出 ------
info() { echo "==> $*"; }
ok()   { echo "    ✓ $*"; }
warn() { echo "    ! $*"; }
die()  { echo "    ✗ $*" >&2; exit 1; }

echo "==============================================="
echo " MagicLingua · 一键安装"
echo "==============================================="

# ---------------------------------------------------------------- 平台 ------
OS="$(uname -s)"
ARCH="$(uname -m)"
if [ "$OS" = "Darwin" ] && [ "$ARCH" = "arm64" ]; then
    USE_WHEEL=1
else
    USE_WHEEL=0
fi
if [ "$USE_WHEEL" = "1" ]; then
    info "平台: $OS ${ARCH}（Apple Silicon → 官方预编译 wheel，免编译）"
else
    info "平台: $OS ${ARCH}（无官方 wheel → llama-cpp-python 源码编译，耗时较长）"
fi

# ---------------------------------------------------------------- Python ----
PY_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PY_BIN" >/dev/null 2>&1; then
    die "未找到 python3，请先安装 Python 3.9+（https://www.python.org/downloads/）"
fi
PY_VER="$("$PY_BIN" -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
if [ "$(printf '%s\n' '3.9' "$PY_VER" | sort -V | head -1)" != "3.9" ]; then
    die "需要 Python ≥ 3.9，当前版本: $PY_VER"
fi
info "Python: $PY_VER"

# ---------------------------------------------------------------- venv ------
if [ -d venv ]; then
    ok "venv 已存在，跳过创建"
else
    info "创建虚拟环境 venv/ ..."
    "$PY_BIN" -m venv venv || die "venv 创建失败"
fi
VPY="venv/bin/python"
[ -x "$VPY" ] || die "venv 内 python 不可用（${VPY}）"
"$VPY" -m pip install --quiet --upgrade pip || die "pip 升级失败"

# ---------------------------------------------------------------- 引擎 ------
stat_size() {
    if [ "$OS" = "Darwin" ]; then stat -f%z "$1"; else stat -c%s "$1"; fi
}

install_engine() {
    if "$VPY" -c "import llama_cpp, sys; sys.exit(0 if llama_cpp.__version__=='$LLAMA_VER' else 1)" 2>/dev/null; then
        ok "llama-cpp-python $LLAMA_VER 已安装，跳过"
        return 0
    fi
    if [ "$USE_WHEEL" = "1" ]; then
        install_engine_wheel
    else
        warn "源码编译 llama-cpp-python ${LLAMA_VER}（约 7 分钟，需要 Xcode CLT / 编译工具链）..."
        "$VPY" -m pip install "llama-cpp-python==$LLAMA_VER" || die "源码编译安装失败"
    fi
    "$VPY" -c "import llama_cpp; print('    ✓ 引擎验证: llama_cpp', llama_cpp.__version__)"
}

install_engine_wheel() {
    mkdir -p "$CACHE_DIR"
    RAW="$CACHE_DIR/$WHEEL_NAME.raw"
    FIXED="$CACHE_DIR/$WHEEL_NAME"

    if [ ! -f "$FIXED" ]; then
        # ---- 下载（校验字节数，不符即删）----
        if [ -f "$RAW" ] && [ "$(stat_size "$RAW")" = "$WHEEL_BYTES" ]; then
            ok "wheel 已下载（$WHEEL_BYTES 字节），跳过下载"
        else
            info "下载官方 wheel（${WHEEL_BYTES} 字节）..."
            rm -f "$RAW"
            curl -fL --retry 3 --progress-bar -o "$RAW" "$WHEEL_URL" \
                || { rm -f "$RAW"; die "wheel 下载失败，请检查网络后重试"; }
            actual="$(stat_size "$RAW")"
            if [ "$actual" != "$WHEEL_BYTES" ]; then
                rm -f "$RAW"
                die "wheel 大小不符（期望 ${WHEEL_BYTES}，实际 ${actual}），已删除，请重试"
            fi
            ok "下载完成"
        fi

        # ---- 修复 CRC：ditto 解压（容忍 CRC 错误）→ zipfile 重打包（写正确 CRC）
        info "修复官方 wheel 的 CRC 字段（打包 bug，内容完好）..."
        TMP="$(mktemp -d)"
        ditto -x -k "$RAW" "$TMP/unpacked" || die "ditto 解压失败"
        "$VPY" - "$TMP/unpacked" "$FIXED" <<'PY'
import os, sys, zipfile
src_dir, dst = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(src_dir):
        for f in files:
            p = os.path.join(root, f)
            z.write(p, os.path.relpath(p, src_dir))
PY
        rm -rf "$TMP"
        ok "CRC 修复完成"
    else
        ok "修复版 wheel 已缓存，跳过下载与修复"
    fi

    info "安装 llama-cpp-python $LLAMA_VER ..."
    "$VPY" -m pip install --quiet "$FIXED" || die "wheel 安装失败"
    ok "引擎安装完成"
}

# ---------------------------------------------------------------- 依赖 ------
install_deps() {
    info "安装核心依赖（Flask / numpy / huggingface_hub 等）..."
    "$VPY" -m pip install --quiet -r requirements-core.txt || die "核心依赖安装失败"
    ok "核心依赖安装完成"

    if [ "$WITH_PDF" = "1" ]; then
        info "安装 PDF 翻译依赖（babeldoc 约 470MB，需几分钟）..."
        "$VPY" -m pip install --quiet -r requirements-pdf.txt || die "PDF 依赖安装失败"
        ok "PDF 依赖安装完成"
    else
        warn "未安装 PDF 翻译依赖（需要时: ./setup_env.sh --with-pdf）"
    fi
}

# ---------------------------------------------------------------- 模型 ------
download_model() {
    if ls models/*.gguf >/dev/null 2>&1; then
        ok "模型已存在: $(ls models/*.gguf | head -1)"
        return 0
    fi
    info "下载翻译模型（约 1.1GB，来自魔搭社区 modelscope.cn，带进度条）..."
    ./download_model.sh || die "模型下载失败，请重试 ./download_model.sh"
    ok "模型下载完成"
}

# ---------------------------------------------------------------- 宿主 ------
setup_host() {
    if [ ! -x native_host/install.command ]; then
        warn "native_host/install.command 不存在，跳过宿主注册"
        return 0
    fi
    info "注册 launchd 自启 + Chrome 原生宿主（含扩展 ID 自动探测）..."
    bash native_host/install.command
}

# ---------------------------------------------------------------- 主流程 ----
install_engine
install_deps
if [ "$SKIP_MODEL" = "1" ]; then
    warn "已跳过模型下载（--skip-model），请自行放置模型到 models/ 或设置 HYMT_MODEL_PATH"
else
    download_model
fi
if [ "$SKIP_HOST" = "1" ]; then
    warn "已跳过 launchd / 浏览器宿主注册（--skip-host）"
else
    setup_host
fi

# ---------------------------------------------------------------- 完成 ------
echo ""
echo "==============================================="
echo " MagicLingua 安装完成！"
echo "==============================================="
echo ""
echo "接下来（三步）："
echo "  1. 完全退出 Chrome（Cmd+Q）再重新打开"
echo "     —— 宿主清单只在 Chrome 启动时加载，不重启会一直报「找不到本地服务」"
echo "  2. 打开 chrome://extensions"
echo "     —— 右上角开启「开发者模式」→「加载已解压的扩展程序」→ 选择本目录的 extension/ 文件夹"
echo "  3. 点插件图标 → 服务卡片 → 点「启动」"
echo ""
echo "常见操作："
echo "  ./start_server_gguf.sh          前台启动服务（调试用）"
echo "  ./download_model.sh             重新下载模型（支持 MODEL_REPO/MODEL_FILE 自定义）"
echo "  ./setup_env.sh --with-pdf       补装 PDF 翻译依赖"
echo "  native_host/diagnose.command    服务连不上时跑诊断"
echo ""
