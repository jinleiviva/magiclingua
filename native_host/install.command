#!/bin/bash
# ---------------------------------------------------------------------------
# 安装「本地服务管理器」
#
# 做三件事：
#   1. 把 hymt_host.py 注册为 Chrome 的 Native Messaging 宿主
#      —— 这样插件才能合法地启动/停止本地翻译服务
#   2. 注册 launchd 任务 com.magiclingua.server
#      —— RunAtLoad=false，只在插件叫它的时候才起来
#      —— KeepAlive 只在异常退出时重启，服务自己闲置退出不会被拉起
#   3. 立即 bootstrap，验证可 kickstart
#
# 用法：  双击运行，或 ./install.command [扩展ID]
# ---------------------------------------------------------------------------
set -u

cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
HOST_DIR="$ROOT/native_host"
LABEL="com.magiclingua.server"
HOST_NAME="com.magiclingua.host"
# unpacked 扩展的 ID 由「扩展目录路径」决定（同一路径恒定，目录改名/移动才会变），
# 因此不写死任何默认 ID。白名单来源：① 浏览器记录自动探测（见下）② 命令行传入。
# 注意：不要给 manifest 加固定 key——它会让 Chrome 认为 ID 应为 key 派生值，
# 与路径哈希记录冲突，导致扩展被反复禁用（「重启后消失」的元凶）。
EXT_IDS=""
for _arg in "$@"; do
    [ -n "$_arg" ] && EXT_IDS="$EXT_IDS $_arg"
done
# 去重（保证每个 ID 在白名单里只出现一次）
_SEEN=""
for id in $EXT_IDS; do
    case " $_SEEN " in
        *" $id "*) ;;
        *) _SEEN="${_SEEN:+$_SEEN }$id" ;;
    esac
done
EXT_IDS="$_SEEN"

# -------------------------------------------------- 自动探测已安装的扩展 ID --
# 打包安装时 ID 由签名决定，未必在默认值里；未打包的 ID 由文件夹路径决定、
# 一般稳定，但若用户换过文件夹就会变。这里扫一遍各浏览器的 Extensions 目录，
# 把已安装的「MagicLingua」ID 自动并入白名单，省得每次重装都手动传 ID。
DETECTED=""
for edir in \
    "$HOME/Library/Application Support/Google/Chrome" \
    "$HOME/Library/Application Support/Chromium" \
    "$HOME/Library/Application Support/Microsoft Edge" \
    "$HOME/Library/Application Support/BraveSoftware/Brave-Browser" \
    "$HOME/Library/Application Support/Arc/User Data"
do
    [ -d "$edir" ] || continue
    while IFS= read -r mf; do
        if grep -q '"name":[[:space:]]*"MagicLingua"' "$mf" 2>/dev/null; then
            # 路径形如 .../Extensions/<ID>/<版本>/manifest.json
            id="$(echo "$mf" | sed -E 's#.*/Extensions/([a-p]{32})/.*#\1#')"
            [ -n "$id" ] && DETECTED="$DETECTED $id"
        fi
    done < <(find "$edir" -maxdepth 4 -name manifest.json 2>/dev/null)
done
# 去重合并
for id in $DETECTED; do
    case " $EXT_IDS " in
        *" $id "*) ;;
        *) EXT_IDS="$EXT_IDS $id" ;;
    esac
done
if [ -n "$DETECTED" ]; then
    echo "(自动探测到已安装扩展 ID:$DETECTED)"
fi

# 未打包（Load unpacked）的扩展不在 Extensions 目录里，其「路径 → ID」记录
# 存在各浏览器 profile 的 Secure Preferences 中。扫出来并入白名单——
# 这样扩展目录改名/移动后，只需重跑本脚本即可自动对上新 ID。
SP_IDS="$(python3 - "$ROOT/extension" <<'PY'
import json, os, sys
target = os.path.realpath(sys.argv[1])
home = os.path.expanduser("~")
bases = [
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Chromium",
    "Library/Application Support/Microsoft Edge",
    "Library/Application Support/BraveSoftware/Brave-Browser",
    "Library/Application Support/Arc/User Data",
]
ids = []
for b in bases:
    base = os.path.join(home, b)
    if not os.path.isdir(base):
        continue
    for prof in sorted(os.listdir(base)):
        sp = os.path.join(base, prof, "Secure Preferences")
        if not os.path.isfile(sp):
            continue
        try:
            d = json.load(open(sp, encoding="utf-8"))
        except Exception:
            continue
        for eid, info in d.get("extensions", {}).get("settings", {}).items():
            p = str(info.get("path", ""))
            if p and os.path.realpath(p) == target and eid not in ids:
                ids.append(eid)
print(" ".join(ids))
PY
)"
if [ -n "$SP_IDS" ]; then
    echo "(从浏览器扩展记录探测到未打包扩展 ID:$SP_IDS)"
    for id in $SP_IDS; do
        case " $EXT_IDS " in
            *" $id "*) ;;
            *) EXT_IDS="$EXT_IDS $id" ;;
        esac
    done
fi

PY_VENV="$ROOT/venv/bin/python3"

if [ -x "$PY_VENV" ]; then
    SERVER_PY="$PY_VENV"
elif [ -x "/usr/bin/python3" ]; then
    SERVER_PY="/usr/bin/python3"
else
    SERVER_PY="$(command -v python3)"
fi

echo "=================================================="
echo " MagicLingua · 本地服务管理器安装"
echo "=================================================="
echo "项目目录 : $ROOT"
echo "服务解释器: $SERVER_PY"
echo "扩展 ID  :"
for id in $EXT_IDS; do
    echo "          $id"
done
echo ""

# --------------------------------------------------------------- launchd --
LA_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$LA_DIR"
PLIST="$LA_DIR/$LABEL.plist"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$SERVER_PY</string>
        <string>$ROOT/server_gguf.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$ROOT</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HYMT_PORT</key>
        <string>18770</string>
        <key>HYMT_IDLE_EXIT</key>
        <string>20</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>$ROOT/log_server.txt</string>
    <key>StandardErrorPath</key>
    <string>$ROOT/log_server.txt</string>
</dict>
</plist>
PLISTEOF

echo "[1/3] launchd 任务已写入: $PLIST"

UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || true
BOOT_OUT="$(launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>&1)"
if [ -z "$BOOT_OUT" ]; then
    echo "      已注册（RunAtLoad=false，等插件叫它才起来）"
elif launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
    echo "      已注册（本次 bootstrap 提示：${BOOT_OUT%%$'\n'*}）"
else
    echo "      ⚠ launchd 注册失败：${BOOT_OUT%%$'\n'*}"
    echo "        不影响使用——插件会退回到直接派生进程模式。"
    echo "        若想用 launchd 托管，请从访达双击本文件（终端/沙箱里 launchctl 常被拒）。"
fi

# ------------------------------------------------------- Native Messaging --
chmod +x "$HOST_DIR/hymt_host.py"

NM_TARGETS="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
for extra in \
    "$HOME/Library/Application Support/Chromium/NativeMessagingHosts" \
    "$HOME/Library/Application Support/Microsoft Edge/NativeMessagingHosts" \
    "$HOME/Library/Application Support/BraveSoftware/Brave-Browser/NativeMessagingHosts" \
    "$HOME/Library/Application Support/Arc/User Data/NativeMessagingHosts"
do
    parent="$(dirname "$extra")"
    [ -d "$parent" ] && NM_TARGETS="$NM_TARGETS:$extra"
done

echo "[2/3] 注册 Native Messaging 宿主"
IFS=':'
for dir in $NM_TARGETS; do
    unset IFS
    [ -z "$dir" ] && continue
    mkdir -p "$dir"
    ALLOWED=""
    for id in $EXT_IDS; do
        if [ -n "$ALLOWED" ]; then
            ALLOWED="$ALLOWED,
    \"chrome-extension://$id/\""
        else
            ALLOWED="\"chrome-extension://$id/\""
        fi
    done
    cat > "$dir/$HOST_NAME.json" <<NMEOF
{
  "name": "$HOST_NAME",
  "description": "MagicLingua local translation service controller",
  "path": "$HOST_DIR/hymt_host.py",
  "type": "stdio",
  "allowed_origins": [
    $ALLOWED
  ]
}
NMEOF
    echo "      -> $dir/$HOST_NAME.json"
done
unset IFS

# -------------------------------------------------------------- 自检 ------
echo "[3/3] 自检"
# 协议是 4 字节小端长度前缀 + JSON，不能直接用 echo 裸喂
python3 - "$HOST_DIR/hymt_host.py" <<'PY' >/dev/null 2>&1 \
    && echo "      宿主脚本可正常收发消息" \
    || echo "      ⚠ 宿主脚本自检未通过，检查文件权限与 shebang"
import json, struct, subprocess, sys
msg = json.dumps({"action": "ping"}).encode()
p = subprocess.run([sys.argv[1]], input=struct.pack("<I", len(msg)) + msg,
                   capture_output=True, timeout=15)
sys.exit(0 if b'"pong": true' in p.stdout else 1)
PY

echo ""
echo "=================================================="
echo " ⚠️  必须完全退出 Chrome 再打开（Cmd+Q，不是关窗口）"
echo "=================================================="
echo ""
echo "Chrome 只在【启动时】扫描 NativeMessagingHosts 目录。"
echo "只刷新扩展（chrome://extensions 的 ↻）不会重新读取宿主清单，"
echo "宿主清单是这次才写入的，不重启就会一直报「找不到」。"
echo ""
echo "重启后："
echo "  1. chrome://extensions 确认 MagicLingua 已启用（必要时按 ↻ 刷新）"
echo "  2. 点插件图标 -> 第二张卡 -> 点「启动」"
echo "  3. 停止使用 20 分钟后服务自己退出，内存归零"
echo ""
echo "重启后按钮仍是灰的，就跑诊断脚本看具体原因："
echo "  $HOST_DIR/diagnose.command"
echo ""
echo "若诊断说扩展 ID 不匹配，把 chrome://extensions 上显示的 ID 传进来："
echo "  $HOST_DIR/install.command <你的扩展ID>"
echo ""
