# FA Insight Harvester — 部署指南

> **注意：後端伺服器無直接外網，需透過 HTTP proxy 安裝套件。**
> 前端 CSS/JS 使用 CDN，由使用者瀏覽器（有網路）直接載入。

## 系統需求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (Python 套件管理)
- PostgreSQL 16+ (含 pgvector 擴展)
- LibreOffice (用於 PPTX 轉 PDF)
- poppler-utils (用於 PDF 轉 PNG)
- Nginx

## 1. 設定 Proxy

所有需要外網的指令（apt, uv, curl）都透過 proxy：

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
# /etc/apt/apt.conf.d/proxy.conf
sudo tee /etc/apt/apt.conf.d/proxy.conf <<EOF
Acquire::http::Proxy "http://your-proxy:port";
Acquire::https::Proxy "http://your-proxy:port";
EOF
```

## 2. 安裝系統依賴

```bash
sudo apt update
sudo apt install -y postgresql postgresql-contrib \
    libreoffice-impress poppler-utils nginx postgresql-16-pgvector
```

## 3. 安裝 uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 4. 設定 PostgreSQL

```bash
sudo -u postgres psql <<EOF
CREATE DATABASE fa_insight;
CREATE USER fa_user WITH PASSWORD 'your-secure-password';
GRANT ALL PRIVILEGES ON DATABASE fa_insight TO fa_user;
\c fa_insight
CREATE EXTENSION vector;
EOF
```

## 5. 部署應用

```bash
cd /home/YOUR_USER
git clone <repo-url> fa-insight-harvester
cd fa-insight-harvester

# 安裝依賴（uv 會自動使用 HTTP_PROXY 環境變數）
uv sync

# 複製並編輯環境設定
cp .env.example .env
# 編輯 .env，填入 DB 連線資訊、VLM 位址、OAuth 設定等

# 放置 Auth Center 公鑰
cp /path/to/auth_public_key.pem ./auth_public_key.pem

# 建立上傳目錄
mkdir -p uploads/images

# 執行資料庫遷移
uv run alembic upgrade head
```

## 6. 設定 systemd 服務

```bash
mkdir -p ~/.config/systemd/user
cp deploy/fa-insight-harvester.service ~/.config/systemd/user/

# 啟用並啟動
systemctl --user daemon-reload
systemctl --user enable fa-insight-harvester
systemctl --user start fa-insight-harvester

# 讓 user service 在登出後繼續運行
loginctl enable-linger $USER

# 查看狀態
systemctl --user status fa-insight-harvester
journalctl --user -u fa-insight-harvester -f
```

## 7. 設定 Nginx

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/fa-insight-harvester

# 編輯設定，修改 server_name 和路徑
sudo vim /etc/nginx/sites-available/fa-insight-harvester

# 啟用
sudo ln -sf /etc/nginx/sites-available/fa-insight-harvester /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## 8. 驗證

```bash
curl http://localhost:8000/health
```

## 日常更新

```bash
cd /home/YOUR_USER/fa-insight-harvester
git pull
uv sync
uv run alembic upgrade head
systemctl --user restart fa-insight-harvester
```
