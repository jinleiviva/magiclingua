#!/bin/bash
set -e

echo "================================="
echo "HY-MT 翻译服务 —— 环境配置"
echo "================================="

# 检查 Python 版本
echo "检查 Python 版本..."
python3 --version
PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
MIN_VERSION="3.9"
if [ "$(printf '%s\n' "$MIN_VERSION" "$PYTHON_VERSION" | sort -V | head -n1)" != "$MIN_VERSION" ]; then
    echo "错误: 需要 Python 3.9 或更高版本，当前版本: $PYTHON_VERSION"
    exit 1
fi
echo "Python 版本检查通过: $PYTHON_VERSION"

# 创建虚拟环境
echo ""
echo "创建虚拟环境..."
if [ -d "venv" ]; then
    echo "虚拟环境已存在，跳过创建"
else
    python3 -m venv venv
    echo "虚拟环境创建成功"
fi

# 激活虚拟环境
source venv/bin/activate

# 升级 pip
pip install --upgrade pip

# 安装运行依赖（真实最小依赖，见 requirements.txt）
echo ""
echo "安装运行依赖（Flask / llama-cpp-python / PyMuPDF / babeldoc 等）..."
echo "这一步可能耗时几分钟（llama-cpp-python 需要从源码编译）。"
pip install -r requirements.txt

echo ""
echo "================================="
echo "环境配置完成！"
echo "================================="
echo ""
echo "下一步："
echo "  1. 下载翻译模型： ./download_model.sh   （约 1-2GB，首次必须）"
echo "  2. 启动服务：       ./start_server_gguf.sh"
echo "  3. 浏览器打开：     http://localhost:18770/pdf"
echo "  4. 安装 Chrome 扩展：开发者模式加载 extension/ 目录"
