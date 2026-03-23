# QVault — 部署指南

> **注意：後端伺服器無直接外網，需透過 HTTP proxy 安裝套件。**
> 前端 CSS/JS 使用 CDN，由使用者瀏覽器（有網路）直接載入。

## 架構

```
Browser → Nginx (:80) → App (:8000)
    ├── /auth/login     → 導向 OIDC Provider
    ├── /auth/callback  → 交換 code → 設定 session cookie
    ├── /auth/logout    → 清除 session cookie
    └── /*              → 驗證 session cookie 中的 JWT
```

App 以 Python venv 跑在主機上（systemd 管理），PG 用 Docker Compose。App 內建 OIDC 驗證，不需要 oauth2-proxy。

### 設定檔結構

設定分為 **兩個 `.env` 檔**，各司其職：

| 檔案 | 用途 | 使用者 |
|------|------|--------|
| `~/opt/qvault/.env` | App 設定（DB 連線、VLM、Auth） | FastAPI + systemd |
| `~/opt/qvault/deploy/.env` | Docker 服務設定（PG 初始化） | docker-compose |

共用變數（`DATA_DIR`、`PG_*`）需在兩個檔案中保持一致。`setup.sh` 會自動產生兩份。

```
DATA_DIR=/mnt/db/qvault          ← 設定一次，以下自動衍生
├── uploads/                     ← UPLOAD_DIR (自動)
├── logs/                        ← LOG_DIR (自動)
├── keys/public.pem              ← AUTH_PUBLIC_KEY_PATH (自動)
└── pgdata/                      ← PG volume mount (自動)

PG_USER / PG_PASSWORD / PG_PORT  ← DATABASE_URL 自動衍生
```

## 0. 設定 Proxy

所有需要外網的指令（apt, uv, curl, docker pull）都透過 proxy：

```bash
# 加入 ~/.bashrc 或 ~/.profile
export HTTP_PROXY=http://your-proxy:port
export HTTPS_PROXY=http://your-proxy:port
export NO_PROXY=localhost,127.0.0.1
source ~/.bashrc
```

apt proxy：

```bash
sudo tee /etc/apt/apt.conf.d/proxy.conf <<EOF
Acquire::http::Proxy "http://your-proxy:port";
Acquire::https::Proxy "http://your-proxy:port";
EOF
```

Docker daemon proxy：

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

---

## 系統需求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (Python 套件管理)
- Docker + Docker Compose
- LibreOffice (`sudo apt install libreoffice-impress`)
- poppler-utils (`sudo apt install poppler-utils`)
- Nginx (`sudo apt install nginx`)

## 一鍵部署

```bash
git clone <repo-url> qvault && cd qvault
bash deploy/setup.sh
```

腳本會互動式引導：前置檢查 → 同步程式碼 → 建立設定檔（App `.env` + Docker `deploy/.env`） → 資料目錄 → 公鑰 → Docker 服務 → Python 依賴 → DB 遷移 → systemd → Nginx

## 手動部署

<details>
<summary>展開手動步驟</summary>

### 設定檔

```bash
cd ~/opt/qvault

# App 設定（FastAPI + systemd）
cp .env.example .env
# 編輯 .env — 填入：DATA_DIR, PG_PASSWORD, VLM_BASE_URL, VLM_MODEL,
#   OIDC_ISSUER_URL, OAUTH2_CLIENT_SECRET, OAUTH2_REDIRECT_URL
chmod 600 .env

# Docker 服務設定（PostgreSQL + oauth2-proxy）
cp deploy/.env.example deploy/.env
# 編輯 deploy/.env — 填入：PG_PASSWORD
# 注意：PG_* 和 DATA_DIR 須與 .env 一致
chmod 600 deploy/.env
```

### 資料目錄

```bash
# DATA_DIR 下的子目錄（對應 .env 中的 DATA_DIR）
mkdir -p /mnt/db/qvault/{uploads/images,logs,keys,pgdata}
cp /path/to/public.pem /mnt/db/qvault/keys/public.pem
```

### Docker 服務（PostgreSQL）

```bash
cd deploy/
# docker-compose.yml 讀取 deploy/.env
docker compose up -d
```

### Python 依賴 + DB 遷移

```bash
cd ~/opt/qvault
uv sync --no-dev
uv run alembic upgrade head
```

### systemd 服務

```bash
mkdir -p ~/.config/systemd/user
cp deploy/qvault.service ~/.config/systemd/user/qvault.service

systemctl --user daemon-reload
systemctl --user enable --now qvault

# 登出後保持服務執行
sudo loginctl enable-linger $USER
```

### Nginx

```bash
sed 's|your-server-name|qvault.your-domain.com|g' \
    deploy/nginx.conf | sudo tee /etc/nginx/sites-available/qvault

sudo ln -sf /etc/nginx/sites-available/qvault /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

</details>

## 日常更新

```bash
cd ~/qvault && git pull
cd ~/opt/qvault
rsync -a --delete --exclude='.env' --exclude='.venv' \
    ~/qvault/ ~/opt/qvault/
uv sync --no-dev
uv run alembic upgrade head
systemctl --user restart qvault
```

或直接重新執行 `bash deploy/setup.sh`。

## 檔案結構

```
~/opt/qvault/                     # 程式碼（setup.sh rsync 過來）
├── app/                          # 應用程式碼
├── .venv/                        # uv 管理的虛擬環境
├── .env                          # App 設定檔（FastAPI + systemd）
├── deploy/
│   ├── .env                      # Docker 設定檔（PostgreSQL）
│   ├── docker-compose.yml        # PostgreSQL（讀取 deploy/.env）
│   ├── qvault.service            # systemd unit（讀取 ../.env）
│   └── nginx.conf                # Nginx 模板
└── alembic/                      # 資料庫遷移

/mnt/db/qvault/                   # 持久化資料（DATA_DIR）
├── pgdata/                       # PostgreSQL 資料
├── uploads/images/               # 上傳的 PPTX/PDF/PNG
├── logs/                         # Loguru 日誌
└── keys/public.pem               # Auth Center RS256 公鑰
```

---

## 驗證

```bash
systemctl --user status qvault
docker compose -f ~/opt/qvault/deploy/docker-compose.yml ps
curl http://localhost:8000/health   # 應回傳 {"status": "ok"}
```

## 服務管理速查

| 動作 | 指令 |
|---|---|
| 查看 app 狀態 | `systemctl --user status qvault` |
| 重啟 app | `systemctl --user restart qvault` |
| App 日誌 | `journalctl --user -u qvault -f` |
| PG 日誌 | `docker compose -f ~/opt/qvault/deploy/docker-compose.yml logs -f` |
| 停止全部 | `systemctl --user stop qvault && docker compose -f ~/opt/qvault/deploy/docker-compose.yml down` |
