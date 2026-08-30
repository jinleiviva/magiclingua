#!/bin/bash
set -e

echo "================================="
echo "启动HY-MT翻译服务 (GGUF版)"
echo "================================="

# 检查虚拟环境
if [ ! -d "venv" ]; then
    echo "错误: 虚拟环境不存在！"
    echo "请先运行: ./setup_env.sh"
    exit 1
fi

# 激活虚拟环境
source venv/bin/activate

# 模型由 server_gguf.py 在 models/ 目录自动扫描（或 HYMT_MODEL_PATH 指定）
if ! ls models/*.gguf >/dev/null 2>&1 && [ -z "${HYMT_MODEL_PATH:-}" ]; then
    echo "错误: 未找到 GGUF 模型！"
    echo "请先运行: ./download_model.sh   （约 1.1 GB，只下一次）"
    exit 1
fi

echo ""
echo "端口: 18770"
echo "推理引擎: llama.cpp (GGUF)"
echo ""
echo "服务启动后，可以："
echo "1. 在浏览器打开 http://localhost:18770/pdf 使用 PDF 翻译助手"
echo "2. 运行 python test_api.py 测试API"
echo ""
echo "按 Ctrl+C 停止服务"
echo ""
echo "提示: GGUF 格式对 CPU 优化良好，推理速度快！"
echo ""

# 启动服务器（显式使用 venv 内的 python，避免系统/python 路径歧义）
venv/bin/python3 server_gguf.py 2>&1 | tee log_server.txt
