#!/bin/bash
set -euo pipefail
# 下载 HY-MT GGUF 模型到 models/ 目录（首次运行需要，约 1.1GB）。
#
# 特性：
# - 已存在则跳过（幂等，重复执行不会重下）
# - curl 直连 + 断点续传 + 字节数校验（content-range / x-linked-size）+ SHA256 完整性校验
# - 默认走魔搭社区 modelscope.cn（国内 CDN，实测约 10MB/s，比 hf-mirror 快 5 倍），
#   腾讯官方命名空间 Tencent-Hunyuan；可用环境变量切换回 HuggingFace。
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

# 默认走魔搭社区（国内直连 CDN）；设置 HF_ENDPOINT 则切回 HuggingFace / 镜像。
# 例：HF_ENDPOINT=https://huggingface.co MODEL_NAMESPACE=tencent ./download_model.sh
NAMESPACE="${MODEL_NAMESPACE:-Tencent-Hunyuan}"
REPO="${MODEL_REPO:-Hy-MT2-1.8B-GGUF}"
FILE="${MODEL_FILE:-Hy-MT2-1.8B-Q4_K_M.gguf}"
if [ -n "${HF_ENDPOINT:-}" ]; then
    # HuggingFace 格式：无 /models/ 前缀，分支为 main，大小在 x-linked-size 头
    BASE="$HF_ENDPOINT/$NAMESPACE/$REPO"
    URL="$BASE/resolve/main/$FILE"
    SIZE_SRC="x-linked-size"
else
    # 魔搭格式：/models/ 前缀，分支为 master，大小在 content-range 头
    BASE="https://modelscope.cn/models/$NAMESPACE/$REPO"
    URL="$BASE/resolve/master/$FILE"
    SIZE_SRC="content-range"
fi
DEST="models/$FILE"
PART="$DEST.part"

mkdir -p models

# ------------------------------------------------- SHA256 基线 ----------
# 模型完整性 / 防投毒校验。默认值取自官方源当前发布版本的本地基准快照哈希；
# 若切换模型变体（如 7B / 其他量化档），必须通过环境变量 EXPECTED_SHA256
# 覆盖为对应变体的官方哈希，否则下载后校验会失败并删除文件。
# ⚠️ 权威哈希应以模型发布页（modelscope / HuggingFace）公布的为准。
EXPECTED_SHA256="${EXPECTED_SHA256:-dc5f44fcf1fa496ee7ad725982c0c8c553a4de00259b53af84c4b89fb0c06699}"

# ---------------------------------------------------------------- 幂等 -------
if [ -f "$DEST" ] && [ -s "$DEST" ]; then
    SIZE="$(du -h "$DEST" | cut -f1)"
    echo "已存在：models/${FILE}（${SIZE}），跳过下载"
    echo "如需强制重下，先删除该文件再运行本脚本。"
    # 已存在也复核哈希（若 EXPECTED_SHA256 已设置），不匹配仅告警不阻断。
    if [ -n "$EXPECTED_SHA256" ]; then
        actual_sha="$(sha256_of "$DEST")"
        [ "$actual_sha" = "$EXPECTED_SHA256" ] \
            && echo "🔒 SHA256 校验通过" \
            || echo "⚠ SHA256 与基线不符（可能变体不同或文件损坏），建议删除后重下"
    fi
    exit 0
fi

# ------------------------------------------------------ 获取期望字节数 ------
# 魔搭：GET + Range 0-0 返回 206 + content-range: bytes 0-0/1133080448 → 取斜杠后数字
# HF：  HEAD 返回 x-linked-size: 1133080448 → 取数字
if [ "$SIZE_SRC" = "content-range" ]; then
    EXPECTED="$(curl -sL -D - -o /dev/null --max-time 20 -r 0-0 "$URL" | grep -i "^content-range:" | head -1 | sed 's/.*\///' | tr -dc '0-9')"
else
    EXPECTED="$(curl -sIL --max-time 20 "$URL" | grep -i "^x-linked-size:" | head -1 | sed 's/[^0-9]//g')"
fi
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

# SHA256 校验：macOS 用 shasum，Linux 用 sha256sum
sha256_of() {
    shasum -a 256 "$1" 2>/dev/null | awk '{print $1}' \
        || sha256sum "$1" 2>/dev/null | awk '{print $1}'
}
verify_sha256() {
    [ -z "$EXPECTED_SHA256" ] && return 0
    local actual
    actual="$(sha256_of "$1")"
    if [ "$actual" = "$EXPECTED_SHA256" ]; then
        echo "🔒 SHA256 校验通过：$actual"
        return 0
    fi
    echo "✗ SHA256 校验失败！"
    echo "   期望：$EXPECTED_SHA256"
    echo "   实际：$actual"
    echo "   文件可能损坏或被篡改，请勿使用该模型。"
    return 1
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
echo "来源: $URL"

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
if ! verify_sha256 "$DEST"; then
    echo "下载完成但校验失败，正在删除损坏文件..."; rm -f "$DEST"; exit 1
fi
echo ""
echo "✅ 完成：models/${FILE}（$(du -h "$DEST" | cut -f1)）"
echo "提示：也可用环境变量自定义，例如"
echo "  魔搭（默认）：MODEL_NAMESPACE=Tencent-Hunyuan MODEL_REPO=Hy-MT2-7B-GGUF MODEL_FILE=Hy-MT2-7B-Q4_K_M.gguf"
echo "  切回 HF：    HF_ENDPOINT=https://huggingface.co MODEL_NAMESPACE=tencent MODEL_REPO=Hy-MT2-7B-GGUF MODEL_FILE=Hy-MT2-7B-Q4_K_M.gguf"
