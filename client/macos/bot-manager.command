#!/bin/bash
# Bot Manager 客户端 · macOS 版
#
# 与 Windows 客户端等价：用 SSH 私钥建四条回环隧道，打开面板，并提供
# 测试连接 / 生成密钥 / 安装公钥 / 重置服务密码 等操作。
#
# 依赖：系统自带 bash 3.2、ssh、ssh-keygen、osascript、security、lsof、curl。
# 配置：~/.config/bot-manager-client/settings.json
# 密钥：面板密码与私钥口令存 macOS 钥匙串（service = bot-manager-client）

set -u

APP_NAME="Bot Manager 客户端"
APP_VERSION="2.2.5"
CFG_DIR="$HOME/.config/bot-manager-client"
CFG_FILE="$CFG_DIR/settings.json"
LOG_DIR="$HOME/Library/Logs/BotManagerClient"
LOG_FILE="$LOG_DIR/client.log"
SSH_LOG="$LOG_DIR/ssh.log"
PID_FILE="$CFG_DIR/tunnel.pid"
KNOWN_HOSTS="$CFG_DIR/known_hosts"

PANEL_PORT=17321
RESET_SCRIPT="/opt/bot-manager/current/reset-service-password.py"
KEYCHAIN_SERVICE="bot-manager-client"

# 面板里「新窗口打开」的三个入口：链接在本机浏览器打开，所以隧道要映射同名端口
SERVICE_PORTS="6185 5099 6081"

# 允许覆盖：Homebrew 的 ssh 通常比系统自带的新，SSH_ASKPASS_REQUIRE 支持更好
SSH_BIN="${BM_SSH_BIN:-/usr/bin/ssh}"
KEYGEN_BIN="${BM_SSH_KEYGEN:-/usr/bin/ssh-keygen}"
ASKPASS_FILE=""
TUNNEL_PID=0

# ---------------------------------------------------------------- 基础工具

ensure_dirs() {
    [ -d "$CFG_DIR" ] || mkdir -p "$CFG_DIR"
    [ -d "$LOG_DIR" ] || mkdir -p "$LOG_DIR"
    chmod 700 "$CFG_DIR" 2>/dev/null || true
}

log() {
    ensure_dirs
    local stamp
    stamp="$(date '+%Y-%m-%d %H:%M:%S')"
    printf '[%s] %s\n' "$stamp" "$*" >> "$LOG_FILE"
}

say() {
    # 终端里跑时打印，双击 .command 时也可见
    printf '%s\n' "$*"
    log "$*"
}

die() {
    say "错误：$*"
    alert "操作失败" "$*" "stop"
    exit 1
}

alert() {
    local title="$1" message="$2" icon="${3:-caution}"
    osascript >/dev/null 2>&1 <<APPLESCRIPT
tell application "System Events"
    display dialog "$(escape_applescript "$message")" with title "$(escape_applescript "$title")" buttons {"好"} default button 1 with icon $icon
end tell
APPLESCRIPT
}

escape_applescript() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

# 单行输入；取消返回非 0
prompt_text() {
    local title="$1" message="$2" default_value="${3:-}"
    local result
    result="$(osascript 2>/dev/null <<APPLESCRIPT
tell application "System Events"
    set r to display dialog "$(escape_applescript "$message")" with title "$(escape_applescript "$title")" default answer "$(escape_applescript "$default_value")" buttons {"取消", "确定"} default button 2
    return text returned of r
end tell
APPLESCRIPT
)" || return 1
    printf '%s' "$result"
}

# 隐藏输入（密码）；取消返回非 0
prompt_secret() {
    local title="$1" message="$2"
    local result
    result="$(osascript 2>/dev/null <<APPLESCRIPT
tell application "System Events"
    set r to display dialog "$(escape_applescript "$message")" with title "$(escape_applescript "$title")" default answer "" with hidden answer buttons {"取消", "确定"} default button 2
    return text returned of r
end tell
APPLESCRIPT
)" || return 1
    printf '%s' "$result"
}

confirm() {
    local title="$1" message="$2"
    osascript >/dev/null 2>&1 <<APPLESCRIPT
tell application "System Events"
    display dialog "$(escape_applescript "$message")" with title "$(escape_applescript "$title")" buttons {"取消", "继续"} default button 2
end tell
APPLESCRIPT
}

# ---------------------------------------------------------------- 配置读写

json_get() {
    # 极简 JSON 读取：只处理本脚本自己写出的扁平对象
    local key="$1"
    [ -f "$CFG_FILE" ] || return 1
    sed -n 's/.*"'"$key"'":[[:space:]]*"\([^"]*\)".*/\1/p' "$CFG_FILE" | head -1
}

json_get_bool() {
    local key="$1" default="$2" value
    [ -f "$CFG_FILE" ] || { printf '%s' "$default"; return 0; }
    value="$(sed -n 's/.*"'"$key"'":[[:space:]]*\(true\|false\).*/\1/p' "$CFG_FILE" | head -1)"
    [ -z "$value" ] && value="$default"
    printf '%s' "$value"
}

json_escape() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

write_settings() {
    local host="$1" port="$2" user="$3" key="$4" local_port="$5" autobrowser="$6" panel_user="$7"
    ensure_dirs
    cat > "$CFG_FILE" <<JSON
{
  "serverHost": "$(json_escape "$host")",
  "serverPort": "$(json_escape "$port")",
  "serverUser": "$(json_escape "$user")",
  "keyPath": "$(json_escape "$key")",
  "localPort": "$(json_escape "$local_port")",
  "openBrowser": $autobrowser,
  "panelUser": "$(json_escape "$panel_user")"
}
JSON
    chmod 600 "$CFG_FILE"
    log "设置已保存"
}

load_settings() {
    SERVER_HOST="$(json_get serverHost)"
    SERVER_PORT="$(json_get serverPort)"; [ -n "$SERVER_PORT" ] || SERVER_PORT=22
    SERVER_USER="$(json_get serverUser)"; [ -n "$SERVER_USER" ] || SERVER_USER=root
    KEY_PATH="$(json_get keyPath)"
    LOCAL_PORT="$(json_get localPort)"; [ -n "$LOCAL_PORT" ] || LOCAL_PORT=$PANEL_PORT
    OPEN_BROWSER="$(json_get_bool openBrowser true)"
    PANEL_USER="$(json_get panelUser)"; [ -n "$PANEL_USER" ] || PANEL_USER=admin
}

settings_complete() {
    [ -n "${SERVER_HOST:-}" ] && [ -n "${KEY_PATH:-}" ] && [ -f "${KEY_PATH:-/nonexistent}" ]
}

# ---------------------------------------------------------------- 钥匙串

keychain_set() {
    local account="$1" value="$2"
    security delete-generic-password -a "$account" -s "$KEYCHAIN_SERVICE" >/dev/null 2>&1 || true
    printf '%s' "$value" | security add-generic-password -a "$account" -s "$KEYCHAIN_SERVICE" -w "$(cat)" -U >/dev/null 2>&1
}

keychain_get() {
    local account="$1"
    security find-generic-password -a "$account" -s "$KEYCHAIN_SERVICE" -w 2>/dev/null || true
}

keychain_has() {
    local account="$1"
    security find-generic-password -a "$account" -s "$KEYCHAIN_SERVICE" >/dev/null 2>&1
}

# ---------------------------------------------------------------- askpass

install_askpass() {
    # 供 ssh 在无 tty 时取私钥口令；密码来自钥匙串，不落盘
    ensure_dirs
    ASKPASS_FILE="$CFG_DIR/askpass-$$.sh"
    cat > "$ASKPASS_FILE" <<'ASKPASS'
#!/bin/sh
security find-generic-password -a "$BM_ASKPASS_ACCOUNT" -s "bot-manager-client" -w 2>/dev/null
ASKPASS
    chmod 700 "$ASKPASS_FILE"
}

cleanup_askpass() {
    [ -n "$ASKPASS_FILE" ] && rm -f "$ASKPASS_FILE"
    ASKPASS_FILE=""
}

ssh_common_args() {
    printf '%s\n' \
        "-p" "$SERVER_PORT" \
        "-o" "ConnectTimeout=10" \
        "-o" "StrictHostKeyChecking=accept-new" \
        "-o" "UserKnownHostsFile=$KNOWN_HOSTS" \
        "-o" "LogLevel=ERROR" \
        "-o" "ServerAliveInterval=30" \
        "-o" "ServerAliveCountMax=3"
}

run_ssh() {
    # 处理口令私钥：有口令就挂 askpass，没有就 BatchMode
    local passphrase
    passphrase="$(keychain_get "key-passphrase")"
    if [ -n "$passphrase" ]; then
        install_askpass
        SSH_ASKPASS="$ASKPASS_FILE" SSH_ASKPASS_REQUIRE=force BM_ASKPASS_ACCOUNT="key-passphrase" \
            "$SSH_BIN" "$@" </dev/null
        local code=$?
        cleanup_askpass
        return $code
    fi
    "$SSH_BIN" -o BatchMode=yes "$@" </dev/null
}

# ---------------------------------------------------------------- 端口与隧道

port_listening() {
    local port="$1"
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
}

port_owner_pid() {
    local port="$1"
    lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -1
}

port_owner_name() {
    local pid="$1"
    [ -z "$pid" ] && return 1
    ps -p "$pid" -o comm= 2>/dev/null | head -1
}

wait_port() {
    local port="$1" seconds="${2:-20}" i=0
    while [ "$i" -lt "$seconds" ]; do
        port_listening "$port" && return 0
        sleep 1
        i=$((i + 1))
    done
    return 1
}

panel_healthy() {
    local port="$1"
    curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/api/health" 2>/dev/null | grep -q '^200$'
}

tunnel_pid() {
    local pid=""
    if [ -f "$PID_FILE" ]; then
        pid="$(tr -dc '0-9' < "$PID_FILE")"
    fi
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        printf '%s' "$pid"
        return 0
    fi
    return 1
}

start_tunnel() {
    ensure_dirs
    local port skip=""
    local -a forwards=()
    local -a args=()
    forwards+=("127.0.0.1:$LOCAL_PORT:127.0.0.1:$PANEL_PORT")
    for port in $SERVICE_PORTS; do
        [ "$port" = "$LOCAL_PORT" ] && continue
        if port_listening "$port"; then
            skip="$skip $port"
            continue
        fi
        forwards+=("127.0.0.1:$port:127.0.0.1:$port")
    done
    [ -n "$skip" ] && log "本机端口被占用，已跳过转发：$skip"

    # ssh_common_args 输出的是逐行参数，按行读进数组，避免被拆成单个字符串
    while IFS= read -r line; do
        args+=("$line")
    done <<EOF
$(ssh_common_args)
EOF
    args+=(-i "$KEY_PATH" -N)
    local forward
    for forward in "${forwards[@]}"; do
        args+=(-L "$forward")
    done
    args+=(-o ExitOnForwardFailure=yes -o BatchMode=yes -E "$SSH_LOG")
    args+=("$SERVER_USER@$SERVER_HOST")

    local passphrase
    passphrase="$(keychain_get "key-passphrase")"
    local -a askenv=()
    if [ -n "$passphrase" ]; then
        install_askpass
        askenv=(env "SSH_ASKPASS=$ASKPASS_FILE" "SSH_ASKPASS_REQUIRE=force" "BM_ASKPASS_ACCOUNT=key-passphrase")
    fi

    "${askenv[@]}" "$SSH_BIN" "${args[@]}" </dev/null >/dev/null 2>&1 &
    local pid=$!
    cleanup_askpass
    printf '%s' "$pid" > "$PID_FILE"
    TUNNEL_PID=$pid
    log "隧道已启动（ssh PID $pid，转发 ${#forwards[@]} 个端口）"

    if ! wait_port "$LOCAL_PORT" 20; then
        return 1
    fi
    return 0
}

stop_tunnel() {
    local pid
    pid="$(tunnel_pid)" || {
        rm -f "$PID_FILE"
        return 1
    }
    kill "$pid" 2>/dev/null || true
    sleep 1
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    TUNNEL_PID=0
    log "隧道已停止（PID $pid）"
    return 0
}

# ---------------------------------------------------------------- 连接

test_connection() {
    local result
    result="$(run_ssh -i "$KEY_PATH" -o BatchMode=no "$SERVER_USER@$SERVER_HOST" 'echo BM_OK' 2>&1)"
    if printf '%s' "$result" | grep -q 'BM_OK'; then
        say "[连接测试] 成功：这把私钥可以登录 $SERVER_USER@$SERVER_HOST"
        return 0
    fi
    say "[连接测试] 失败：$(printf '%s' "$result" | tail -3 | tr '\n' ' ')"
    return 1
}

open_panel() {
    if [ "$OPEN_BROWSER" = "true" ]; then
        open "http://127.0.0.1:$LOCAL_PORT/"
        log "已在浏览器打开面板"
    fi
}

connect_panel() {
    load_settings
    if ! settings_complete; then
        alert "还没有配置" "请先在「设置」里填服务器地址、用户和私钥路径。"
        run_settings
        return
    fi

    if port_listening "$LOCAL_PORT"; then
        local pid name
        pid="$(port_owner_pid "$LOCAL_PORT")"
        name="$(port_owner_name "$pid")"
        local healthy="不可用"
        panel_healthy "$LOCAL_PORT" && healthy="可用"
        if confirm "已有隧道" "本机端口 $LOCAL_PORT 已被 ${name:-未知进程}(PID ${pid:-?}) 占用。隧道状态：$healthy

点「继续」会结束它并重新建立本客户端的隧道；点「取消」则直接沿用已有隧道。"; then
            if [ -n "$pid" ] && [ "${name:-}" = "ssh" ]; then
                kill "$pid" 2>/dev/null || true
                sleep 1
                log "已结束遗留隧道 PID $pid"
            else
                alert "端口被占用" "占用端口的是 ${name:-未知进程}，不是 ssh，未做处理。请手动关闭或换一个本地端口。"
                return
            fi
        else
            if [ "$healthy" = "可用" ]; then
                say "沿用已有隧道（断开时不会关闭它）"
                open_panel
            else
                alert "端口被占用" "端口被非 ssh 进程或坏掉的隧道占用，无法复用。"
            fi
            return
        fi
    fi

    say "正在建立隧道…"
    if ! start_tunnel; then
        local tail_text
        tail_text="$(tail -8 "$SSH_LOG" 2>/dev/null)"
        alert "隧道没建起来" "20 秒内本地端口没有开始监听。

$tail_text"
        return
    fi
    if ! panel_healthy "$LOCAL_PORT"; then
        alert "面板没有响应" "隧道已建立，但面板无响应。请确认服务器上 bot-manager 服务在运行：

systemctl status bot-manager"
        return
    fi
    say "已连接：http://127.0.0.1:$LOCAL_PORT/"
    open_panel
}

disconnect_panel() {
    if stop_tunnel; then
        say "已断开"
    else
        say "本客户端没有正在运行的隧道"
    fi
    if port_listening "$LOCAL_PORT"; then
        local pid name
        pid="$(port_owner_pid "$LOCAL_PORT")"
        name="$(port_owner_name "$pid")"
        say "注意：端口 $LOCAL_PORT 仍由 ${name:-未知}(PID ${pid:-?}) 监听，这不是本客户端启动的隧道"
        alert "仍有隧道在跑" "端口 $LOCAL_PORT 仍被 ${name:-未知}(PID ${pid:-?}) 占用，那不是本客户端启动的。"
    fi
}

# ---------------------------------------------------------------- 密钥

generate_key() {
    local target
    target="$(osascript 2>/dev/null <<'APPLESCRIPT'
tell application "System Events"
    set r to choose file name with prompt "新私钥保存为（默认在 ~/.ssh）" default name "id_ed25519" default location (path to home folder)
    return POSIX path of r
end tell
APPLESCRIPT
)" || return 1
    [ -z "$target" ] && return 1
    if [ -f "$target" ]; then
        alert "文件已存在" "$target 已经存在，换一个名字。"
        return 1
    fi
    mkdir -p "$(dirname "$target")"
    chmod 700 "$(dirname "$target")" 2>/dev/null || true

    local passphrase=""
    if prompt_secret "私钥口令（可留空）" "给私钥设个口令可以防止文件被拷走后直接使用。留空表示不设口令。" >/dev/null 2>&1; then
        passphrase="$(prompt_secret "私钥口令（可留空）" "输入私钥口令，留空表示不设。" || true)"
    fi
    "$KEYGEN_BIN" -t ed25519 -f "$target" -N "$passphrase" -C 'bot-manager-client' >/dev/null 2>&1 || {
        alert "生成失败" "ssh-keygen 执行失败，请检查目录是否可写。"
        return 1
    }
    [ -n "$passphrase" ] && keychain_set "key-passphrase" "$passphrase"
    local pub
    pub="$(cat "$target.pub")"
    printf '%s' "$pub" | pbcopy 2>/dev/null || true
    alert "已生成密钥" "私钥：$target

公钥（已复制到剪贴板）：
$pub"
    KEY_PATH="$target"
    save_from_prompts

    if confirm "安装公钥" "现在就把它装到服务器吗？需要输入一次服务器登录密码（不是私钥口令）。"; then
        install_public_key
    fi
}

install_public_key() {
    local pubfile="$KEY_PATH.pub"
    if [ ! -f "$pubfile" ]; then
        alert "找不到公钥" "同目录下没有 $KEY_PATH.pub。"
        return 1
    fi
    local server_password
    server_password="$(prompt_secret "服务器登录密码" "输入 $SERVER_USER@$SERVER_HOST 的登录密码，用于把公钥追加到 authorized_keys。")" || return 1
    [ -z "$server_password" ] && return 1

    ensure_dirs
    local helper="$CFG_DIR/askpass-server-$$.sh"
    printf '#!/bin/sh\nprintf "%%s\\n" "%s"\n' "$server_password" > "$helper"
    chmod 700 "$helper"

    local output
    output="$(SSH_ASKPASS="$helper" SSH_ASKPASS_REQUIRE=force \
        "$SSH_BIN" -p "$SERVER_PORT" -o StrictHostKeyChecking=accept-new \
        -o UserKnownHostsFile="$KNOWN_HOSTS" -o PreferredAuthentications=password \
        -o PubkeyAuthentication=no -o NumberOfPasswordPrompts=1 \
        "$SERVER_USER@$SERVER_HOST" \
        'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && echo BM_KEY_OK' \
        < "$pubfile" 2>&1)"
    rm -f "$helper"

    if printf '%s' "$output" | grep -q 'BM_KEY_OK'; then
        say "[安装公钥] 成功"
        alert "已安装" "公钥已追加到服务器 authorized_keys，现在可以点「测试连接」验证。"
    else
        say "[安装公钥] 失败：$(printf '%s' "$output" | tail -3 | tr '\n' ' ')"
        alert "安装失败" "$(printf '%s' "$output" | tail -5)"
    fi
}

# ---------------------------------------------------------------- 密码重置

reset_password() {
    local choice
    choice="$(osascript 2>/dev/null <<'APPLESCRIPT'
tell application "System Events"
    set r to choose from list {"AstrBot（6185，账号 astrbot）", "SnowLuma（5099，账号 admin）", "noVNC / VNC（6081，密码最长 8 位）"} with title "重置哪个服务的密码？" with prompt "通过 SSH 在服务器上执行，改完会立刻用新密码试登录。" default items {"AstrBot（6185，账号 astrbot）"}
    return item 1 of r
end tell
APPLESCRIPT
)" || return 1
    [ -z "$choice" ] && return 1

    local target max_len
    case "$choice" in
        AstrBot*)  target="astrbot";  max_len=128 ;;
        SnowLuma*) target="snowluma"; max_len=128 ;;
        *)         target="vnc";      max_len=8 ;;
    esac

    local password
    password="$(prompt_secret "重置 $target 密码" "输入新密码（至少 8 位，不能含空格，最多 $max_len 位）。")" || return 1
    [ ${#password} -lt 8 ] && { alert "密码太短" "至少 8 位。"; return 1; }
    [ ${#password} -gt "$max_len" ] && { alert "密码太长" "最多 $max_len 位。"; return 1; }
    case "$password" in *[[:space:]]*) alert "格式不支持" "不能包含空格等空白字符。"; return 1 ;; esac

    local again
    again="$(prompt_secret "确认密码" "再输入一次。")" || return 1
    [ "$password" != "$again" ] && { alert "两次不一致" "请重新执行重置。"; return 1; }

    say "正在重置 $target 密码…"
    local output
    output="$(printf '%s\n' "$password" | run_ssh \
        -o BatchMode=no "$SERVER_USER@$SERVER_HOST" \
        "python3 $RESET_SCRIPT --target $target" 2>&1)"

    local json
    json="$(printf '%s' "$output" | grep -E '^\{.*\}$' | tail -1)"
    if [ -z "$json" ]; then
        say "[$target] 没有拿到服务端结果：$(printf '%s' "$output" | tail -3 | tr '\n' ' ')"
        alert "重置失败" "没有拿到服务端结果：

$(printf '%s' "$output" | tail -6)"
        return 1
    fi

    local ok message detail
    ok="$(printf '%s' "$json" | sed -n 's/.*"ok"[[:space:]]*:[[:space:]]*\(true\|false\).*/\1/p')"
    message="$(printf '%s' "$json" | sed -n 's/.*"message"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
    detail="$(printf '%s' "$json" | sed -n 's/.*"detail"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"

    say "[$target] $message"
    [ -n "$detail" ] && say "  $detail"
    if [ "$ok" = "true" ]; then
        if [ "$target" = "astrbot" ]; then
            keychain_set "panel-password" "$password"
        fi
        alert "密码已重置" "$message

$detail"
    else
        alert "重置失败" "服务端返回：$message

$detail"
    fi
}

# ---------------------------------------------------------------- 设置

save_from_prompts() {
    write_settings "$SERVER_HOST" "$SERVER_PORT" "$SERVER_USER" "$KEY_PATH" "$LOCAL_PORT" "$OPEN_BROWSER" "$PANEL_USER"
}

run_settings() {
    local host port user key local_port
    host="$(prompt_text "服务器地址" "服务器 IP 或域名" "${SERVER_HOST:-}")" || return
    port="$(prompt_text "SSH 端口" "默认 22" "${SERVER_PORT:-22}")" || return
    user="$(prompt_text "登录用户" "通常是 root" "${SERVER_USER:-root}")" || return
    key="$(prompt_text "私钥文件" "私钥的完整路径，例如 ~/.ssh/id_ed25519" "${KEY_PATH:-$HOME/.ssh/id_ed25519}")" || return
    case "$key" in "~/"*) key="$HOME/${key#\~/}" ;; esac
    local_port="$(prompt_text "本地端口" "隧道在本机监听的端口" "${LOCAL_PORT:-$PANEL_PORT}")" || return

    SERVER_HOST="$host"; SERVER_PORT="$port"; SERVER_USER="$user"; KEY_PATH="$key"; LOCAL_PORT="$local_port"
    write_settings "$SERVER_HOST" "$SERVER_PORT" "$SERVER_USER" "$KEY_PATH" "$LOCAL_PORT" "$OPEN_BROWSER" "$PANEL_USER"

    if ! [ -f "$KEY_PATH" ]; then
        alert "私钥不存在" "$KEY_PATH 不存在，请重新选择。"
        return
    fi
    if prompt_secret "私钥口令" "如果这把私钥有口令，填一次会存进钥匙串；没有口令就点取消。" >/dev/null 2>&1; then
        local pass
        pass="$(prompt_secret "私钥口令" "输入私钥口令（存钥匙串，下次自动使用）。" || true)"
        [ -n "$pass" ] && keychain_set "key-passphrase" "$pass"
    fi
    if prompt_secret "面板密码" "面板 admin 的密码，用于「复制密码」（可留空）。" >/dev/null 2>&1; then
        local panel_pw
        panel_pw="$(prompt_secret "面板密码" "输入面板登录密码。" || true)"
        [ -n "$panel_pw" ] && keychain_set "panel-password" "$panel_pw"
    fi
    say "设置已保存：$SERVER_USER@$SERVER_HOST:$SERVER_PORT → $LOCAL_PORT"
}

copy_panel_password() {
    local password
    password="$(keychain_get "panel-password")"
    if [ -z "$password" ]; then
        alert "没有面板密码" "在「设置」里填一次面板密码，或到服务器上重置。"
        return 1
    fi
    printf '%s' "$password" | pbcopy
    say "面板密码已复制到剪贴板"
}

show_log() {
    ensure_dirs
    if [ -f "$LOG_FILE" ]; then
        tail -20 "$LOG_FILE"
    else
        say "还没有日志"
    fi
}

# ---------------------------------------------------------------- 菜单

main_menu() {
    load_settings
    local status="未配置"
    if settings_complete; then
        status="$SERVER_USER@$SERVER_HOST · 本地 $LOCAL_PORT"
        if tunnel_pid >/dev/null; then
            status="$status · 已连接"
        fi
    fi

    local choice
    choice="$(osascript 2>/dev/null <<APPLESCRIPT
tell application "System Events"
    set r to choose from list {"连接并打开面板", "测试连接", "断开连接", "重置服务密码", "生成密钥…", "安装公钥到服务器…", "复制面板密码", "设置…", "查看日志", "退出"} with title "$APP_NAME $APP_VERSION" with prompt "$(escape_applescript "$status")" default items {"连接并打开面板"}
    return item 1 of r
end tell
APPLESCRIPT
)" || exit 0
    [ -z "$choice" ] && exit 0

    case "$choice" in
        "连接并打开面板") connect_panel ;;
        "测试连接")
            if ! settings_complete; then alert "还没有配置" "先到「设置」里填服务器和私钥。"; run_settings; main_menu; return; fi
            test_connection && alert "连接成功" "这把私钥可以登录 $SERVER_USER@$SERVER_HOST。" || alert "连接失败" "看日志：$LOG_FILE"
            main_menu ;;
        "断开连接") disconnect_panel; main_menu ;;
        "重置服务密码") reset_password; main_menu ;;
        "生成密钥…") generate_key; main_menu ;;
        "安装公钥到服务器…") install_public_key; main_menu ;;
        "复制面板密码") copy_panel_password; main_menu ;;
        "设置…") run_settings; main_menu ;;
        "查看日志") show_log; main_menu ;;
        "退出") exit 0 ;;
    esac
}

# ---------------------------------------------------------------- 自检与诊断

self_test() {
    local failures=0
    check() {
        local label="$1" result="$2"
        if [ "$result" = "0" ]; then
            printf '[OK  ] %s\n' "$label"
        else
            printf '[FAIL] %s\n' "$label"
            failures=$((failures + 1))
        fi
    }

    [ -x "$SSH_BIN" ]; check "ssh 可用" $?
    [ -x "$KEYGEN_BIN" ]; check "ssh-keygen 可用" $?
    command -v osascript >/dev/null 2>&1; check "osascript 可用" $?
    command -v security >/dev/null 2>&1; check "security 可用" $?
    command -v lsof >/dev/null 2>&1; check "lsof 可用" $?
    command -v curl >/dev/null 2>&1; check "curl 可用" $?

    local tmp
    tmp="$(mktemp -d)"
    write_settings "example.com" "22" "root" "/tmp/key" "17321" "true" "admin" >/dev/null 2>&1
    cp "$CFG_FILE" "$tmp/s.json" 2>/dev/null
    [ -s "$tmp/s.json" ]; check "设置文件可写入" $?
    cp "$tmp/s.json" "$CFG_FILE" 2>/dev/null
    load_settings
    [ "$SERVER_HOST" = "example.com" ]; check "设置可回读" $?
    rm -rf "$tmp"

    keychain_set "selftest" "value-123" >/dev/null 2>&1
    [ "$(keychain_get "selftest")" = "value-123" ]; check "钥匙串读写" $?
    security delete-generic-password -a "selftest" -s "$KEYCHAIN_SERVICE" >/dev/null 2>&1 || true

    port_listening 1; [ $? -ne 0 ]; check "端口探测（1 未监听）" $?

    printf '\n%s 项失败\n' "$failures"
    [ "$failures" -eq 0 ] && return 0 || return 1
}

diagnose() {
    load_settings
    if ! settings_complete; then
        say "配置不完整，先运行一次图形界面填好设置。"
        return 2
    fi
    say "服务器：$SERVER_USER@$SERVER_HOST:$SERVER_PORT"
    say "私钥：$KEY_PATH"
    say "本地端口：$LOCAL_PORT → 面板 $PANEL_PORT"
    if ! test_connection; then
        return 1
    fi
    if port_listening "$LOCAL_PORT"; then
        say "[隧道] 端口 $LOCAL_PORT 已在监听"
    else
        if ! start_tunnel; then
            say "[隧道] 启动失败"
            tail -8 "$SSH_LOG" 2>/dev/null
            return 1
        fi
        say "[隧道] 已启动（PID $TUNNEL_PID）"
    fi
    local port state
    for port in $SERVICE_PORTS; do
        state="未监听"
        port_listening "$port" && state="已转发"
        say "[转发] $port -> $state"
    done
    if panel_healthy "$LOCAL_PORT"; then
        say "[面板] /api/health 200，链路可用"
        local code=0
    else
        say "[面板] 无响应"
        local code=1
    fi
    stop_tunnel >/dev/null 2>&1
    say "[隧道] 已停止"
    return $code
}

# ---------------------------------------------------------------- 入口

trap 'cleanup_askpass' EXIT INT TERM

case "${1:-}" in
    --selftest)
        ensure_dirs
        self_test
        exit $?
        ;;
    --diagnose)
        ensure_dirs
        diagnose
        exit $?
        ;;
    --version)
        printf '%s %s\n' "$APP_NAME" "$APP_VERSION"
        exit 0
        ;;
    --help|-h)
        cat <<HELP
$APP_NAME $APP_VERSION（macOS）

  （无参数）      打开图形菜单
  --selftest     环境自检：ssh / 钥匙串 / 配置读写 / 端口探测
  --diagnose     链路诊断：连接测试 → 建隧道 → 四个端口 → 面板健康检查
  --version      版本号
HELP
        exit 0
        ;;
esac

ensure_dirs
load_settings

# 供自动化测试 source 使用：只定义函数与常量，不进菜单
if [ "${BM_SOURCE_ONLY:-0}" = "1" ]; then
    return 0 2>/dev/null || exit 0
fi

main_menu
