#!/bin/bash
# ---------------------------------------------------------------------------
# 诊断：为什么插件连不上本地服务管理器
#
# 逐项检查 Native Messaging 通路的每一环，把结论直接打出来。
# 用法：双击运行
# ---------------------------------------------------------------------------
set -u

cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
HOST_DIR="$ROOT/native_host"
HOST_NAME="com.magiclingua.host"
LABEL="com.magiclingua.server"
PORT="18770"

ok()   { echo "  ✅ $1"; }
bad()  { echo "  ❌ $1"; }
warn() { echo "  ⚠️  $1"; }

echo "=================================================="
echo " MagicLingua · 本地服务管理器诊断"
echo "=================================================="
echo ""

# ---------------------------------------------------------- 1. 宿主脚本 ----
echo "[1] 宿主脚本"
if [ -f "$HOST_DIR/hymt_host.py" ]; then
    ok "存在: $HOST_DIR/hymt_host.py"
else
    bad "缺失: $HOST_DIR/hymt_host.py —— 项目文件被移动过？"
fi

if [ -x "$HOST_DIR/hymt_host.py" ]; then
    ok "有执行权限"
else
    bad "没有执行权限，正在补上"
    chmod +x "$HOST_DIR/hymt_host.py"
fi

# Chrome 会用自己的环境直接执行它，模拟一次
if printf '' | "$HOST_DIR/hymt_host.py" >/dev/null 2>&1; then
    ok "直接执行不报错（shebang /usr/bin/env python3 可解析）"
else
    bad "直接执行失败，检查 shebang 与 python3 是否在 PATH 里"
fi

# ----------------------------------------------- 2. Native Messaging 清单 --
echo ""
echo "[2] Native Messaging 宿主清单"

FOUND_ANY=0
for dir in \
    "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts" \
    "$HOME/Library/Application Support/Chromium/NativeMessagingHosts" \
    "$HOME/Library/Application Support/Microsoft Edge/NativeMessagingHosts" \
    "$HOME/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts"
do
    [ -d "$dir" ] || continue
    f="$dir/$HOST_NAME.json"
    browser="$(basename "$(dirname "$(dirname "$dir")")")"
    if [ -f "$f" ]; then
        FOUND_ANY=1
        ok "$browser: $f"
        "$HOST_DIR/../venv/bin/python3" - "$f" <<'PY'
import json, os, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as e:
    print("     ❌ JSON 解析失败:", e); raise SystemExit
print("     path   =", d.get("path"))
print("     type   =", d.get("type"))
if os.access(d.get("path", ""), os.X_OK):
    print("     ✅ path 指向的文件可执行")
else:
    print("     ❌ path 指向的文件不存在或不可执行")
for o in d.get("allowed_origins", []):
    print("     允许:", o)
PY
    fi
done
[ "$FOUND_ANY" -eq 0 ] && bad "任何浏览器目录下都没有 $HOST_NAME.json —— 先跑 install.command"

# ------------------------------------------------------------- 3. 服务 ----
echo ""
echo "[3] 翻译服务状态"
if curl -s --max-time 2 "http://127.0.0.1:$PORT/v1/status" >/tmp/mf_status.json 2>/dev/null; then
    ok "服务在线"
    cat /tmp/mf_status.json; echo
else
    warn "服务未运行（这是正常的，省内存模式）"
fi

# --------------------------------------------------------- 4. 浏览器进程 --
echo ""
echo "[4] 浏览器"
RUNNING=0
for b in "Google Chrome" "Chromium" "Microsoft Edge" "Brave Browser" "Arc"; do
    p="$(pgrep -x "$b" | head -1)"
    if [ -n "$p" ]; then
        RUNNING=1
        START="$(ps -o lstart= -p "$p" | xargs)"
        echo "  • $b 运行中 (pid $p)"
        echo "    启动时间: $START"
    fi
done
[ "$RUNNING" -eq 0 ] && echo "  • 没有检测到运行中的浏览器"

echo ""
echo "=================================================="
echo " 结论与下一步"
echo "=================================================="
echo ""
if [ "$RUNNING" -eq 1 ]; then
    echo "浏览器正在运行。如果它是在你跑 install.command 之前启动的，"
    echo "那它根本没读过新写入的宿主清单 —— 这就是按钮灰掉的原因。"
    echo ""
    echo "  👉 完全退出浏览器：Cmd+Q（不是点红色关闭按钮）"
    echo "  👉 重新打开，再试「启动」按钮"
    echo ""
fi
echo "重启后仍不行，请对比两处的 ID 是否一致："
echo "  A. chrome://extensions 上「MagicLingua」卡片显示的 ID"
echo "  B. 上面 [2] 里列出的 allowed_origins"
echo ""
echo "不一致就重跑（把 A 的 ID 填进去）："
echo "  $HOST_DIR/install.command <A 的 ID>"
echo ""
