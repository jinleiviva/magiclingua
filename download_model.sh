#!/bin/bash
set -e
# 下载 HY-MT GGUF 模型到 models/ 目录（首次运行需要，约 1–2GB）。
#
# 模型许可：Tencent HY Community License Agreement（详见仓库内 MODEL_LICENSE.txt）。
# 该许可不适用于欧盟 / 英国 / 韩国，请勿在这些地区分发或使用。
cd "$(dirname "$0")"
if [ -f venv/bin/activate ]; then source venv/bin/activate; fi

REPO="${MODEL_REPO:-tencent/Hy-MT2-1.8B-GGUF}"
FILE="${MODEL_FILE:-Hy-MT2-1.8B.Q4_K_M.gguf}"

mkdir -p models
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"

echo "从 Hugging Face 下载 $REPO/$FILE ..."
python3 -m huggingface_hub hf_hub_download \
    --repo-type model \
    --repo-id "$REPO" \
    --filename "$FILE" \
    --local-dir models

echo "完成：models/$FILE"
echo "提示：也可用环境变量自定义，例如 MODEL_REPO=tencent/Hy-MT2-7B-GGUF MODEL_FILE=Hy-MT2-7B.Q4_K_M.gguf ./download_model.sh"
