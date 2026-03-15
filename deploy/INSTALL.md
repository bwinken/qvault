# QVault — 部署指南

> **注意：後端伺服器無直接外網，需透過 HTTP proxy 安裝套件。**
> 前端 CSS/JS 使用 CDN，由使用者瀏覽器（有網路）直接載入。

## 架構

```
Browser → Nginx (:80)
    ├── /oauth2/*    → oauth2-proxy (:4180)  [登入/登出]
    ├── /auth/login  → App (:8000)           [登入頁面，無需驗證]
    ├── /uploads/*   → 靜態檔案               [無需驗證，快取 7 天]
    └── /*           → auth_request 驗證
                     → App (:8000)           [JWT 注入 Authorization header]
```

三個組件：
- **App** — FastAPI，user-level systemd 服務（port 8000）
- **PostgreSQL + oauth2-proxy** — Docker Compose（PG :5432，oauth2-proxy :4180）
- **Nginx** — 系統層級，反向代理 + auth_request 認證

## 系統需求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (Python 套件管理)
- Docker + Docker Compose
- LibreOffice (用於 PPTX 轉 PDF)
- poppler-utils (用於 PDF 轉 PNG)
- Nginx

## 1. 設定 Proxy

所有需要外網的指令（apt, uv, curl, docker pull）都透過 proxy：

```bash
# 加入 ~/.bashrc 或 ~/.profile
export HTTP_PROXY=http://your-proxy:port
export HTTPS_PROXY=http://your-proxy:port
export NO_PROXY=localhost,127.0.0.1

# 立即生效
source ~/.bashrc
```

apt 也需要設定 proxy：

```bash
sudo tee /etc/apt/apt.conf.d/proxy.conf <<EOF
Acquire::http::Proxy "http://your-proxy:port";
Acquire::https::Proxy "http://your-proxy:port";
EOF
```

Docker pull 也需要 proxy（systemd 方式）：

```bash
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/proxy.conf <<EOF
[Service]
Environment="HTTP_PROXY=http://your-proxy:port"
Environment="HTTPS_PROXY=http://your-proxy:port"
Environment="NO_PROXY=localhost,127.0.0.1"
EOF
sudo systemctl daemon-reload && sudo systemctl restart docker
```

## 2. 安裝系統依賴

```bash
sudo apt update
sudo apt install -y libreoffice-impress poppler-utils nginx
```

## 3. 安裝 uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 4. 一鍵部署

```bash
cd /home/YOUR_USER
git clone <repo-url> qvault
cd qvault
bash deploy/setup.sh
```

腳本會互動式引導完成以下步驟：
1. 前置工具檢查
2. 程式碼同步到 `~/opt/qvault`
3. PostgreSQL + oauth2-proxy Docker 設定（互動輸入）
4. 啟動 Docker 服務
5. 安裝 Python 依賴
6. 建立 `.env` 並自動填入 DATABASE_URL
7. Auth Center 公鑰設定
8. 資料庫遷移
9. systemd 服務 + Nginx 設定

## 5. 手動部署（不使用 setup.sh）

### 5a. Docker 服務

```bash
cd deploy/
cp .env.example .env
# 編輯 .env，填入 PG 密碼、OIDC 設定、域名等
# 產生 cookie secret: openssl rand -base64 32

docker compose up -d
```

### 5b. 應用設定

```bash
cd ~/opt/qvault
uv sync --no-dev

cp .env.example .env
# 編輯 .env，填入 DATABASE_URL、VLM 位址等

mkdir -p keys uploads/images logs
# 放置 Auth Center 公鑰
cp /path/to/public.pem keys/public.pem

uv run alembic upgrade head
```

### 5c. systemd 服務

```bash
mkdir -p ~/.config/systemd/user
cp deploy/qvault.service ~/.config/systemd/user/qvault.service

systemctl --user daemon-reload
systemctl --user enable qvault
systemctl --user start qvault

# 登出後保持服務執行
sudo loginctl enable-linger $USER

# 查看狀態
systemctl --user status qvault
journalctl --user -u qvault -f
```

### 5d. Nginx

```bash
# 替換模板變數後複製
sed -e 's|__APP_DIR__|/home/YOUR_USER/opt/qvault|g' \
    -e 's|your-server-name|qvault.your-domain.com|g' \
    deploy/nginx.conf | sudo tee /etc/nginx/sites-available/qvault

sudo ln -sf /etc/nginx/sites-available/qvault /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

## 驗證

```bash
# 檢查所有服務
systemctl --user status qvault
docker compose -f ~/opt/qvault/deploy/docker-compose.yml ps

# 測試（跳過 auth）
curl http://localhost:8000/health
```

## 日常更新

```bash
cd /home/YOUR_USER/qvault
git pull
bash deploy/setup.sh    # 重新同步 + 遷移 + 重啟
```

或手動：

```bash
cd ~/opt/qvault
# 手動同步程式碼...
uv sync --no-dev
uv run alembic upgrade head
systemctl --user restart qvault
```

## 檔案結構（部署後）

```
~/opt/qvault/
├── app/                          # 應用程式碼
├── .venv/                        # uv 管理的虛擬環境
├── .env                          # 應用設定（DATABASE_URL, VLM 位址）
├── keys/public.pem               # Auth Center RS256 公鑰
├── uploads/images/               # 上傳的 PPTX/PDF/PNG
├── logs/                         # Loguru 日誌
├── deploy/
│   ├── docker-compose.yml        # PG + oauth2-proxy
│   ├── .env                      # Docker 環境變數（PG 密碼、OIDC 設定）
│   ├── pgdata/                   # PostgreSQL 資料持久化
│   ├── qvault.service            # systemd unit（已複製到 ~/.config/systemd/user/）
│   └── nginx.conf                # Nginx 模板
├── alembic/                      # 資料庫遷移
└── pyproject.toml
```
