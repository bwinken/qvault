#!/bin/bash
# FA Insight Harvester 部署腳本（PostgreSQL + User-Level systemd + Nginx）
# 用法: bash deploy/setup.sh
# 部署到 ~/opt/fa-insight-harvester，以當前使用者身份執行
set -e

APP_NAME="fa-insight-harvester"
APP_DIR="$HOME/opt/$APP_NAME"
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# PostgreSQL 設定（可透過環境變數覆蓋）
PG_USER="${PG_USER:-fa_insight}"
PG_PASSWORD="${PG_PASSWORD:-fa_insight_password}"
PG_DB="${PG_DB:-fa_insight}"
PG_PORT="${PG_PORT:-5432}"

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

# PostgreSQL Docker 設定
PG_CONTAINER="${PG_CONTAINER:-fa-insight-pg}"

# === 前置檢查 ===
echo "=== 0. 前置檢查 ==="
MISSING=""
command -v uv &>/dev/null || MISSING="$MISSING uv"
command -v docker &>/dev/null || MISSING="$MISSING docker"
command -v libreoffice &>/dev/null || MISSING="$MISSING libreoffice"
command -v pdftoppm &>/dev/null || MISSING="$MISSING poppler-utils(pdftoppm)"

if [ -n "$MISSING" ]; then
    echo "缺少以下工具：$MISSING"
    echo ""
    echo "安裝方式："
    echo "  uv:            curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "  docker:        https://docs.docker.com/engine/install/"
    echo "  libreoffice:   sudo apt install libreoffice-impress"
    echo "  poppler-utils: sudo apt install poppler-utils"
    exit 1
fi

echo "=== 1. 啟動 PostgreSQL Docker 容器 ==="
if docker ps --format '{{.Names}}' | grep -q "^${PG_CONTAINER}$"; then
    echo "PostgreSQL 容器已在運行中"
elif docker ps -a --format '{{.Names}}' | grep -q "^${PG_CONTAINER}$"; then
    echo "啟動已存在的 PostgreSQL 容器..."
    docker start "$PG_CONTAINER"
else
    echo "建立並啟動 PostgreSQL 容器..."
    mkdir -p "$HOME/opt/pgdata-fa-insight"
    docker run -d \
        --name "$PG_CONTAINER" \
        --restart unless-stopped \
        -e POSTGRES_USER="$PG_USER" \
        -e POSTGRES_PASSWORD="$PG_PASSWORD" \
        -e POSTGRES_DB="$PG_DB" \
        -p "127.0.0.1:${PG_PORT}:5432" \
        -v "$HOME/opt/pgdata-fa-insight:/var/lib/postgresql/data" \
        pgvector/pgvector:pg16
    echo "等待 PostgreSQL 啟動..."
    for i in $(seq 1 15); do
        if docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" &>/dev/null; then
            echo "PostgreSQL 已就緒"
            break
        fi
        sleep 1
    done
fi

echo "=== 2. 部署程式碼 ==="
mkdir -p "$APP_DIR"
rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='.claude/' --exclude='.env' \
    --exclude='uploads/' --exclude='logs/' \
    "$SCRIPT_DIR/" "$APP_DIR/"

echo "=== 3. 安裝依賴（uv sync）==="
cd "$APP_DIR" && uv sync

echo "=== 4. 建立必要目錄 ==="
mkdir -p "$APP_DIR/uploads/images"
mkdir -p "$APP_DIR/logs"

echo "=== 5. 檢查 .env ==="
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    # 自動填入 PostgreSQL 連線資訊
    sed -i "s|DATABASE_URL=.*|DATABASE_URL=postgresql+asyncpg://${PG_USER}:${PG_PASSWORD}@localhost:${PG_PORT}/${PG_DB}|" "$APP_DIR/.env"
    echo "已建立 .env（已自動填入 DATABASE_URL，請編輯其餘設定）"
fi
chmod 600 "$APP_DIR/.env"

echo "=== 6. 檢查 OAuth 公鑰 ==="
if [ ! -f "$APP_DIR/auth_public_key.pem" ]; then
    echo "提醒：auth_public_key.pem 尚未放置"
    echo "  請從 Auth Center 取得公鑰並放到 $APP_DIR/auth_public_key.pem"
else
    chmod 644 "$APP_DIR/auth_public_key.pem"
fi

echo "=== 7. 資料庫遷移 ==="
cd "$APP_DIR" && uv run alembic upgrade head 2>/dev/null || \
    echo "提醒：資料庫遷移失敗，請確認 .env 中的 DATABASE_URL 設定正確且 PostgreSQL 已啟動"

echo "=== 8. 安裝 user-level systemd service ==="
mkdir -p "$HOME/.config/systemd/user"
sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/fa-insight-harvester.service" \
    > "$HOME/.config/systemd/user/$APP_NAME.service"
systemctl --user daemon-reload
systemctl --user enable "$APP_NAME"
systemctl --user restart "$APP_NAME"

# 確保使用者登出後服務仍繼續執行
echo "=== 9. 啟用 lingering（登出後保持服務執行）==="
sudo loginctl enable-linger "$(whoami)" 2>/dev/null || \
    echo "提醒：需要 sudo 執行 loginctl enable-linger $(whoami) 以確保登出後服務持續運行"

echo "=== 10. 安裝 nginx 設定（需要 sudo）==="
if command -v nginx &>/dev/null; then
    sed "s|__APP_DIR__|$APP_DIR|g" "$APP_DIR/deploy/nginx.conf" \
        | sudo tee /etc/nginx/sites-available/$APP_NAME > /dev/null
    sudo ln -sf /etc/nginx/sites-available/$APP_NAME /etc/nginx/sites-enabled/$APP_NAME
    sudo nginx -t && sudo systemctl reload nginx
else
    echo "提醒：nginx 未安裝，請手動設定反向代理"
fi

echo ""
echo "=== 部署完成 ==="
echo "應用目錄：$APP_DIR"
echo "PostgreSQL：docker container '$PG_CONTAINER' (127.0.0.1:$PG_PORT)"
echo "資料目錄：$HOME/opt/pgdata-fa-insight"
echo ""
echo "服務管理："
echo "  systemctl --user status $APP_NAME    # 查看狀態"
echo "  systemctl --user restart $APP_NAME   # 重啟"
echo "  journalctl --user -u $APP_NAME -f    # 查看日誌"
echo "  docker logs -f $PG_CONTAINER         # PostgreSQL 日誌"
echo ""
echo "請確認："
echo "  1. 已編輯 $APP_DIR/.env（VLM 位址、OAuth 設定等）"
echo "  2. 已放置 $APP_DIR/auth_public_key.pem（Auth Center RS256 公鑰）"
echo "  3. 已修改 nginx 設定中的 server_name 為實際域名"
echo "     sudo nano /etc/nginx/sites-available/$APP_NAME"
echo "     sudo nginx -t && sudo systemctl reload nginx"
