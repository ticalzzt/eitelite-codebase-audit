#!/usr/bin/env bash
# EITElite 零配置部署脚本
# 用法: curl -fsSL https://raw.githubusercontent.com/ticalzzt/eitelite/main/install.sh | bash

set -e

REPO="https://github.com/ticalzzt/eitelite.git"
INSTALL_DIR="${HOME}/eitelite"
BIN_DIR="${HOME}/.local/bin"

echo "EITElite One-Click Install"
echo "=========================="
echo ""

# 1. 检测 Python
PYTHON=""
for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v $cmd &>/dev/null; then
        VER=$($cmd --version 2>&1 | grep -oP '\d+\.\d+')
        MAJOR=${VER%.*}; MINOR=${VER#*.}
        if [ "$MAJOR" = "3" ] && [ "$MINOR" -ge "10" ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "❌ Python 3.10+ required. Install it then retry."
    exit 1
fi
echo "✅ Python: $($PYTHON --version)"

# 2. 克隆仓库
if [ -d "$INSTALL_DIR" ]; then
    echo "📁 $INSTALL_DIR already exists, pulling updates..."
    cd "$INSTALL_DIR"
    git pull --ff-only 2>/dev/null || true
else
    echo "📥 Cloning EITElite..."
    git clone --depth 1 "$REPO" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# 3. 安装依赖
echo "📦 Installing dependencies..."
$PYTHON -m pip install --quiet --upgrade pip 2>/dev/null || true
$PYTHON -m pip install --quiet requests urllib3 2>/dev/null || true

# 4. 创建 config.json（交互式）
CONFIG_FILE="${INSTALL_DIR}/config.json"
if [ ! -f "$CONFIG_FILE" ]; then
    echo ""
    echo "🔧 Configuration"
    echo "================="
    
    read -p "Worker name [worker]: " WORKER_NAME
    WORKER_NAME=${WORKER_NAME:-worker}
    
    read -p "DeepSeek API Key: " API_KEY
    
    read -p "DeepSeek Model [deepseek-chat]: " MODEL
    MODEL=${MODEL:-deepseek-chat}
    
    read -p "TG Bot Token (optional, press Enter to skip): " TG_TOKEN
    
    read -p "tical-chat URL (optional, e.g. http://server:8080): " CHAT_URL
    
    cat > "$CONFIG_FILE" <<CONFEOF
{
    "name": "$WORKER_NAME",
    "workspace": "$HOME",
    "ai_model": "$MODEL",
    "ai_key": "$API_KEY",
    "ai_endpoint": "https://api.deepseek.com/v1",
    "tg_token": "$TG_TOKEN",
    "chat_url": "$CHAT_URL",
    "chat_key": "${TICAL_CHAT_KEY:-}"
}
CONFEOF
    echo "✅ Config saved to $CONFIG_FILE"
else
    echo "📝 Using existing config: $CONFIG_FILE"
fi

# 5. 创建 systemd service（可选）
read -p "Install as systemd service? (y/N): " INSTALL_SVC
if [ "$INSTALL_SVC" = "y" ] || [ "$INSTALL_SVC" = "Y" ]; then
    SERVICE_NAME="eitelite-${WORKER_NAME}"
    SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
    
    sudo tee "$SERVICE_FILE" > /dev/null <<SERVICEEOF
[Unit]
Description=EITElite Worker - ${WORKER_NAME}
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${INSTALL_DIR}
ExecStart=${PYTHON} ${INSTALL_DIR}/eitelite_cli.py start
Restart=always
RestartSec=5
Environment=HOME=${HOME}

[Install]
WantedBy=multi-user.target
SERVICEEOF

    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    echo "✅ Service created: $SERVICE_NAME"
    echo "   Start: sudo systemctl start $SERVICE_NAME"
    echo "   Logs:  journalctl -u $SERVICE_NAME -f"
fi

# 6. 一键启动
echo ""
echo "🎉 EITElite installed!"
echo "======================="
echo "  Dir:     $INSTALL_DIR"
echo "  Config:  $CONFIG_FILE"
echo "  Start:   cd $INSTALL_DIR && $PYTHON eitelite_cli.py start"
echo "  Status:  cd $INSTALL_DIR && $PYTHON eitelite_cli.py status"
echo ""
echo "Quick test:"
echo '  echo '"'"'{"sender":"seoul","target":"'${WORKER_NAME}'","content":"ping"}'"'"' | curl -s -X POST http://localhost:8080/v1/messages -H "Content-Type: application/json" -d @-'
