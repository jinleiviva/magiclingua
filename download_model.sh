#!/bin/bash
set -euo pipefail
# 下载 HY-MT GGUF 模型到 models/ 目录（首次运行需要，约 1.1GB）。
#
# 特性：
# - 已存在则跳过（幂等，重复执行不会重下）
# - curl 直连 + 断点续传 + 字节数校验（x-linked-size）
# - 默认走国内镜像 hf-mirror.com（该仓库自动回源官方，等价官方直连），
#   可用 HF_ENDPOINT 覆盖（如 https://huggingface.co）
#
# 为什么不用 huggingface_hub CLI：
#   huggingface_hub ≥1.x 对 hf-mirror 的 308 回源重定向有跨 host 安全检查，
#   会报 "Distant resource does not seem to be on huggingface.co" 而失败；
#   curl -L 无此限制，且自带进度条/断点续传，跨版本稳定。
#
# 模型许可：Tencent HY Community License Agreement（详见仓库内 MODEL_LICENSE.txt）。
# 该许可不适用于欧盟 / 英国 / 韩国，请勿在这些地区分发或使用。
cd "$(dirname "$0")"
ROOT="$(pwd)"

REPO="${MODEL_REPO:-tencent/Hy-MT2-1.8B-GGUF}"
FILE="${MODEL_FILE:-Hy-MT2-1.8B-Q4_K_M.gguf}"
ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
URL="$ENDPOINT/$REPO/resolve/main/$FILE"
DEST="models/$FILE"
PART="$DEST.part"

mkdir -p models

# ---------------------------------------------------------------- 幂等 -------
if [ -f "$DEST" ] && [ -s "$DEST" ]; then
    SIZE="$(du -h "$DEST" | cut -f1)"
    echo "已存在：models/${FILE}（${SIZE}），跳过下载"
    echo "如需强制重下，先删除该文件再运行本脚本。"
    exit 0
fi

# ------------------------------------------------------ 获取期望字节数 ------
# x-linked-size 是 CDN 给出的精确大小；HEAD 跟随重定向即可读到
EXPECTED="$(curl -sIL --max-time 20 "$URL" | grep -i "^x-linked-size:" | head -1 | sed 's/[^0-9]//g')"
if [ -z "$EXPECTED" ]; then
    echo "⚠ 无法获取文件大小（网络受限？），跳过大小校验，仅依赖 curl 完整性。"
    EXPECTED=""
fi
if [ -n "$EXPECTED" ]; then
    SIZE_GB="$(echo "scale=2; $EXPECTED/1073741824" | bc 2>/dev/null || echo "1.1")"
    echo "目标: $REPO/${FILE}（约 ${SIZE_GB}GB）"
fi

# ------------------------------------------------------------ 下载与校验 ------
check_size() {
    [ -z "$EXPECTED" ] && return 0
    local actual
    actual="$(stat -f%z "$1" 2>/dev/null || stat -c%s "$1" 2>/dev/null || echo 0)"
    [ "$actual" = "$EXPECTED" ]
}

dl() {
    # $1 = 是否允许断点续传
    if [ "$1" = "1" ] && [ -f "$PART" ] && [ -s "$PART" ]; then
        echo "检测到未完成下载，断点续传..."
        curl -fL --retry 3 --retry-all-errors --progress-bar -C - -o "$PART" "$URL"
    else
        rm -f "$PART"
        curl -fL --retry 3 --retry-all-errors --progress-bar -o "$PART" "$URL"
    fi
}

echo "下载 $REPO/$FILE ..."
echo "（约 1.1GB，显示进度条；断网自动重试，可随时中断后重跑续传）"

# 第一轮：允许续传
if ! dl 1; then
    echo "下载中断，请重试（会从断点继续）"; exit 1
fi
if ! check_size "$PART"; then
    echo "⚠ 大小校验失败，整包重下（不使用断点，避免续传坏数据）..."
    if ! dl 0; then
        echo "下载中断，请重试"; exit 1
    fi
    check_size "$PART" || { echo "✗ 大小仍不符，请检查网络后重试"; rm -f "$PART"; exit 1; }
fi

mv "$PART" "$DEST"
echo ""
echo "✅ 完成：models/${FILE}（$(du -h "$DEST" | cut -f1)）"
echo "提示：也可用环境变量自定义，例如 MODEL_REPO=tencent/Hy-MT2-7B-GGUF MODEL_FILE=Hy-MT2-7B.Q4_K_M.gguf ./download_model.sh"
