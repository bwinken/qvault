#!/bin/bash
# QVault 部署腳本（PostgreSQL + User-Level systemd + Nginx）
# 用法: bash deploy/setup.sh
# 部署到 ~/opt/qvault，以當前使用者身份執行
set -e

APP_NAME="qvault"
APP_DIR="$HOME/opt/$APP_NAME"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_DIR="$APP_DIR/deploy"
ENV_FILE="$APP_DIR/.env"
DOCKER_ENV_FILE="$DEPLOY_DIR/.env"

# Proxy 設定（只需設定 http_proxy 即可，不需要則留空）
PROXY_URL="${http_proxy:-}"
if [ -n "$PROXY_URL" ]; then
    export http_proxy="$PROXY_URL"
    export HTTP_PROXY="$PROXY_URL"
    export https_proxy="$PROXY_URL"
    export HTTPS_PROXY="$PROXY_URL"
    export no_proxy="localhost,127.0.0.1,*.company.local"
    export NO_PROXY="$no_proxy"
    echo "使用 Proxy: $PROXY_URL"
fi

# ╔═══════════════════════════════════════╗
# ║  0. 前置檢查                          ║
# ╚═══════════════════════════════════════╝
echo ""
echo "=== 0. 前置檢查 ==="
MISSING=""
command -v uv &>/dev/null || MISSING="$MISSING uv"
command -v docker &>/dev/null || MISSING="$MISSING docker"
command -v rsync &>/dev/null || MISSING="$MISSING rsync"
command -v openssl &>/dev/null || MISSING="$MISSING openssl"
command -v libreoffice &>/dev/null || MISSING="$MISSING libreoffice"
command -v pdftoppm &>/dev/null || MISSING="$MISSING poppler-utils(pdftoppm)"

if [ -n "$MISSING" ]; then
    echo "缺少以下工具：$MISSING"
    echo ""
    echo "安裝方式："
    echo "  uv:            curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "  docker:        https://docs.docker.com/engine/install/"
    echo "  rsync:         sudo apt install rsync"
    echo "  openssl:       sudo apt install openssl"
    echo "  libreoffice:   sudo apt install libreoffice-impress"
    echo "  poppler-utils: sudo apt install poppler-utils"
    exit 1
fi

# Check docker daemon
if ! docker info &>/dev/null; then
    echo "錯誤：Docker daemon 未啟動，請先執行 sudo systemctl start docker"
    exit 1
fi

echo "所有前置工具已就緒 ✓"

# ╔═══════════════════════════════════════╗
# ║  1. 同步程式碼                         ║
# ╚═══════════════════════════════════════╝
echo ""
echo "=== 1. 同步程式碼到 $APP_DIR ==="
mkdir -p "$APP_DIR"
rsync -a --delete \
    --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='.claude/' --exclude='.env' \
    --exclude='deploy/.env' \
    --exclude='uploads/' --exclude='logs/' --exclude='keys/' \
    "$SCRIPT_DIR/" "$APP_DIR/"
echo "程式碼同步完成 ✓"

# ╔═══════════════════════════════════════╗
# ║  2. 建立設定檔                        ║
# ╚═══════════════════════════════════════╝
echo ""
echo "=== 2. 建立設定檔 ==="
echo "  App 設定檔：$ENV_FILE"
echo "  Docker 設定檔：$DOCKER_ENV_FILE"

if [ -f "$ENV_FILE" ] && [ -f "$DOCKER_ENV_FILE" ]; then
    echo "設定檔已存在，跳過互動設定"
    echo "  如需重新設定，請刪除 $ENV_FILE 和 $DOCKER_ENV_FILE 後重新執行"
else
    echo ""
    echo "── 資料目錄 ──"
    echo "  所有運行時資料（上傳檔案、日誌、公鑰、PG 資料）集中存放"
    echo "  建議放在獨立 mount point（如 /mnt/db/qvault）"
    read -rp "  資料根目錄 [/mnt/db/qvault]: " DATA_DIR
    DATA_DIR="${DATA_DIR:-/mnt/db/qvault}"

    echo ""
    echo "── PostgreSQL ──"
    read -rp "  資料庫使用者 [qvault]: " PG_USER
    PG_USER="${PG_USER:-qvault}"
    read -rsp "  資料庫密碼: " PG_PASSWORD
    echo ""
    if [ -z "$PG_PASSWORD" ]; then
        PG_PASSWORD=$(openssl rand -hex 16)
        echo "  （自動產生密碼: $PG_PASSWORD）"
    fi
    read -rp "  資料庫名稱 [qvault]: " PG_DB
    PG_DB="${PG_DB:-qvault}"
    read -rp "  PostgreSQL Port [5432]: " PG_PORT
    PG_PORT="${PG_PORT:-5432}"

    echo ""
    echo "── VLM Server ──"
    read -rp "  VLM API Base URL [http://vlm-server:8000/v1]: " VLM_BASE_URL
    VLM_BASE_URL="${VLM_BASE_URL:-http://vlm-server:8000/v1}"
    read -rp "  VLM Model Name: " VLM_MODEL
    VLM_MODEL="${VLM_MODEL:-your-vlm-model-name}"
    read -rp "  VLM Embedding Model Name: " VLM_EMBEDDING_MODEL
    VLM_EMBEDDING_MODEL="${VLM_EMBEDDING_MODEL:-your-embedding-model-name}"

    echo ""
    echo "── OAuth2 / OIDC ──"
    read -rp "  OIDC Issuer URL (Auth Center 位址): " OIDC_ISSUER_URL
    read -rp "  OAuth2 Client ID [qvault]: " OAUTH2_CLIENT_ID
    OAUTH2_CLIENT_ID="${OAUTH2_CLIENT_ID:-qvault}"
    read -rsp "  OAuth2 Client Secret: " OAUTH2_CLIENT_SECRET
    echo ""
    read -rp "  外部域名 (如 qvault.company.com): " DOMAIN
    OAUTH2_REDIRECT_URL="http://${DOMAIN}/auth/callback"

    # ── App .env（FastAPI + systemd 使用）──
    cat > "$ENV_FILE" <<ENVEOF
# ══════════════════════════════════════════════
# QVault — App 設定檔（FastAPI + systemd 使用）
# ══════════════════════════════════════════════
# Docker 服務設定請見 deploy/.env

# ── Data root ──
DATA_DIR=${DATA_DIR}

# ── PostgreSQL (DATABASE_URL auto-derived) ──
PG_USER=${PG_USER}
PG_PASSWORD=${PG_PASSWORD}
PG_DB=${PG_DB}
PG_PORT=${PG_PORT}

# ── VLM Server ──
VLM_BASE_URL=${VLM_BASE_URL}
VLM_API_KEY=dummy
VLM_MODEL=${VLM_MODEL}
VLM_EMBEDDING_MODEL=${VLM_EMBEDDING_MODEL}
VLM_MAX_CONCURRENCY=5

# ── OIDC Auth ──
OIDC_ISSUER_URL=${OIDC_ISSUER_URL}
OAUTH2_CLIENT_ID=${OAUTH2_CLIENT_ID}
OAUTH2_CLIENT_SECRET=${OAUTH2_CLIENT_SECRET}
OAUTH2_REDIRECT_URL=${OAUTH2_REDIRECT_URL}

DEV_SKIP_AUTH=false
ENVEOF
    chmod 600 "$ENV_FILE"
    echo "App .env 已建立 ✓"

    # ── Docker .env（docker-compose 使用）──
    cat > "$DOCKER_ENV_FILE" <<ENVEOF
# ══════════════════════════════════════════════
# QVault — Docker 設定檔（PostgreSQL）
# ══════════════════════════════════════════════
# App 設定請見 ../.env

# ── Data root ──
DATA_DIR=${DATA_DIR}

# ── PostgreSQL ──
PG_USER=${PG_USER}
PG_PASSWORD=${PG_PASSWORD}
PG_DB=${PG_DB}
PG_PORT=${PG_PORT}
ENVEOF
    chmod 600 "$DOCKER_ENV_FILE"
    echo "Docker .env 已建立 ✓"
fi

# Load config for subsequent steps
source "$ENV_FILE"
DATA_DIR="${DATA_DIR:-/mnt/db/qvault}"

# ╔═══════════════════════════════════════╗
# ║  3. 建立資料目錄                       ║
# ╚═══════════════════════════════════════╝
echo ""
echo "=== 3. 建立資料目錄 ==="
mkdir -p "$DATA_DIR/uploads/images"
mkdir -p "$DATA_DIR/logs"
mkdir -p "$DATA_DIR/keys"
mkdir -p "$DATA_DIR/pgdata"
echo "  $DATA_DIR/uploads/   — 上傳檔案"
echo "  $DATA_DIR/logs/      — 應用日誌"
echo "  $DATA_DIR/keys/      — Auth 公鑰"
echo "  $DATA_DIR/pgdata/    — PostgreSQL 資料"
echo "資料目錄已建立 ✓"

# ╔═══════════════════════════════════════╗
# ║  4. Auth Center 公鑰                   ║
# ╚═══════════════════════════════════════╝
echo ""
echo "=== 4. Auth Center 公鑰 ==="
KEY_DIR="$DATA_DIR/keys"
if [ -f "$KEY_DIR/public.pem" ]; then
    echo "public.pem 已存在 ✓"
else
    echo "尚未放置公鑰。請選擇方式："
    echo "  1) 直接貼上公鑰內容"
    echo "  2) 指定檔案路徑"
    echo "  3) 稍後手動放置"
    read -rp "  選擇 [3]: " KEY_CHOICE
    KEY_CHOICE="${KEY_CHOICE:-3}"

    case "$KEY_CHOICE" in
        1)
            echo "  請貼上 PEM 公鑰內容（貼完後按 Ctrl+D）："
            cat > "$KEY_DIR/public.pem"
            echo ""
            echo "  公鑰已儲存 ✓"
            ;;
        2)
            read -rp "  公鑰檔案路徑: " KEY_PATH
            cp "$KEY_PATH" "$KEY_DIR/public.pem"
            echo "  公鑰已複製 ✓"
            ;;
        *)
            echo "  提醒：請手動放置公鑰到 $KEY_DIR/public.pem"
            ;;
    esac
fi
[ -f "$KEY_DIR/public.pem" ] && chmod 644 "$KEY_DIR/public.pem"

echo ""
echo "──────────────────────────────────────"
echo " 檢查點：請確認以上設定正確後按 Enter 繼續"
echo "──────────────────────────────────────"
read -rp ""

# ╔═══════════════════════════════════════╗
# ║  5. 啟動 Docker 服務                   ║
# ╚═══════════════════════════════════════╝
echo ""
echo "=== 5. 啟動 Docker 服務（PostgreSQL）==="
cd "$DEPLOY_DIR"
docker compose up -d
echo "Docker 服務已啟動 ✓"

# ╔═══════════════════════════════════════╗
# ║  6. 安裝 Python 依賴                   ║
# ╚═══════════════════════════════════════╝
echo ""
echo "=== 6. 安裝 Python 依賴 ==="
cd "$APP_DIR"
uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev
echo "依賴安裝完成 ✓"

# ╔═══════════════════════════════════════╗
# ║  7. 資料庫遷移                          ║
# ╚═══════════════════════════════════════╝
echo ""
echo "=== 7. 資料庫遷移 ==="
cd "$APP_DIR"
uv run alembic upgrade head 2>&1 || {
    echo "資料庫遷移失敗，請確認："
    echo "  1. PostgreSQL 容器已正常啟動 (docker ps)"
    echo "  2. .env 中的 PG_* 設定正確"
    exit 1
}
echo "資料庫遷移完成 ✓"

# ╔═══════════════════════════════════════╗
# ║  8. systemd 服務                       ║
# ╚═══════════════════════════════════════╝
echo ""
echo "=== 8. 安裝 user-level systemd 服務 ==="
mkdir -p "$HOME/.config/systemd/user"
cp "$DEPLOY_DIR/qvault.service" "$HOME/.config/systemd/user/$APP_NAME.service"
systemctl --user daemon-reload
systemctl --user enable "$APP_NAME"
systemctl --user restart "$APP_NAME"
echo "systemd 服務已啟動 ✓"

# 確保使用者登出後服務仍繼續執行
sudo loginctl enable-linger "$(whoami)" 2>/dev/null || \
    echo "提醒：需要 sudo 執行 loginctl enable-linger $(whoami)"

# ╔═══════════════════════════════════════╗
# ║  9. Nginx 設定                         ║
# ╚═══════════════════════════════════════╝
echo ""
echo "=== 9. 安裝 Nginx 設定 ==="
if command -v nginx &>/dev/null; then
    # 替換模板變數
    DOMAIN="${DOMAIN:-your-server-name}"
    sed -e "s|your-server-name|$DOMAIN|g" \
        "$DEPLOY_DIR/nginx.conf" \
        | sudo tee /etc/nginx/sites-available/$APP_NAME > /dev/null
    sudo ln -sf /etc/nginx/sites-available/$APP_NAME /etc/nginx/sites-enabled/$APP_NAME
    sudo nginx -t && sudo systemctl reload nginx
    echo "Nginx 設定完成 ✓"
else
    echo "提醒：Nginx 未安裝，請手動設定反向代理"
fi

# ╔═══════════════════════════════════════╗
# ║  完成                                  ║
# ╚═══════════════════════════════════════╝
echo ""
echo "╔══════════════════════════════════════╗"
echo "║       QVault 部署完成！              ║"
echo "╚══════════════════════════════════════╝"
echo ""
echo "  App 設定檔：$ENV_FILE"
echo "  Docker 設定檔：$DOCKER_ENV_FILE"
echo "  資料目錄：$DATA_DIR"
echo "    uploads/  — 上傳的 PPTX/PDF/PNG"
echo "    logs/     — 應用日誌"
echo "    keys/     — Auth Center 公鑰"
echo "    pgdata/   — PostgreSQL 資料"
echo ""
echo "  Docker：PostgreSQL (:${PG_PORT})"
echo "  App：http://127.0.0.1:8000"
echo ""
echo "服務管理："
echo "  systemctl --user status $APP_NAME             # 查看狀態"
echo "  systemctl --user restart $APP_NAME            # 重啟應用"
echo "  journalctl --user -u $APP_NAME -f             # 應用日誌"
echo "  docker compose -f $DEPLOY_DIR/docker-compose.yml logs -f  # Docker 日誌"
echo ""
echo "請確認："
echo "  1. 已放置 $DATA_DIR/keys/public.pem（Auth Center RS256 公鑰）"
if [ "$DOMAIN" = "your-server-name" ]; then
    echo "  2. 已修改 Nginx 設定中的 server_name 為實際域名"
fi
