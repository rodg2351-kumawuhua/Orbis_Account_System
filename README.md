# Orbis AI 财务系统

这是一个适合香港小型新公司（尤其是资讯 / 服务类公司，员工少于 5 人）的轻量财务系统。

当前版本已适配：

- `Render`：免费公网 Web 服务
- `Neon Postgres`：免费线上数据库
- `Cloudinary`：免费单据文件存储

## 当前能力

- 员工用户名 / 密码登录
- 管理员后台统一管理账号
- 结构化财务录入
- 上传收据 / 发票 / PDF
- 单据存到 Cloudinary
- 财务数据存到 Neon Postgres
- CSV 导出
- 可选 AI 单据识别

## 技术栈

- Web 应用：Python 内置 HTTP 服务
- 认证：签名 Cookie + 密码哈希
- 数据库：`SQLite`（本地开发） / `Postgres`（线上）
- 文件存储：本地目录（本地开发） / `Cloudinary`（线上）

## 环境变量

复制 `.env.example` 为 `.env`：

```bash
cp .env.example .env
```

关键变量：

- `APP_SECRET_KEY`
- `DATABASE_URL`
- `DATABASE_PATH`
- `BOOTSTRAP_ADMIN_USERNAME`
- `BOOTSTRAP_ADMIN_PASSWORD`
- `CLOUDINARY_CLOUD_NAME`
- `CLOUDINARY_API_KEY`
- `CLOUDINARY_API_SECRET`
- `OPENAI_API_KEY`（可选）

说明：

- 本地开发时，如果 `DATABASE_URL` 留空，会回退到 `SQLite`
- 线上部署时，应填写 `Neon` 提供的 `DATABASE_URL`
- 线上部署时，应填写 `Cloudinary` 账号参数

## 本地启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m src.app serve-form --host 127.0.0.1 --port 8787
```

打开：

- `http://127.0.0.1:8787/login`

## 管理员与员工

### 初始管理员

首次启动会自动创建管理员账号：

- 用户名：`BOOTSTRAP_ADMIN_USERNAME`
- 密码：`BOOTSTRAP_ADMIN_PASSWORD`

### 命令行创建员工

```bash
python -m src.app create-user --username alice --password 'StrongPass123!' --role staff
```

### 后台管理

管理员登录后进入：

- `/admin/users`

可执行：

- 创建员工
- 设置角色
- 重置密码
- 启用 / 停用账号

## 数据存储

### 本地开发

- 数据库：`data/orbis_finance.db`
- 单据：`output/receipts/`

### 线上部署

- 数据库：`Neon Postgres`
- 单据：`Cloudinary`

## CSV 导出

登录后可导出：

- `/export/ledger.csv`
- `/export/monthly_summary.csv`

## Render 部署

仓库已经包含：

- `Dockerfile`
- `render.yaml`

### 部署步骤

1. 把仓库推到 GitHub
2. 注册并登录 Render
3. 在 Render 新建 Blueprint 或 Web Service
4. 连接这个仓库
5. 使用仓库内的 `render.yaml`

### Render 需要的环境变量

- `APP_SECRET_KEY`
- `BOOTSTRAP_ADMIN_PASSWORD`
- `DATABASE_URL`
- `CLOUDINARY_CLOUD_NAME`
- `CLOUDINARY_API_KEY`
- `CLOUDINARY_API_SECRET`

### Health Check

系统默认健康检查地址：

- `/login`

## Neon 配置

1. 注册并登录 Neon
2. 新建一个免费 Postgres 项目
3. 复制连接串
4. 写入 Render 环境变量：

```text
DATABASE_URL=postgresql://...
```

## Cloudinary 配置

1. 注册并登录 Cloudinary
2. 获取：
   - `cloud_name`
   - `api_key`
   - `api_secret`
3. 写入 Render 环境变量

这样上传的单据就不会依赖 Render 本地磁盘。

## 本地开发与线上行为差异

### 本地

- 没有 `DATABASE_URL` 时，用 SQLite
- 没有 Cloudinary 配置时，单据保存到本地目录

### 线上

- 有 `DATABASE_URL` 时，用 Postgres
- 有 Cloudinary 配置时，单据直接上传云端

## 重要文件

- 应用主入口：`src/app.py`
- 配置：`src/config.py`
- 数据模型：`src/models.py`
- 依赖：`requirements.txt`
- Render 配置：`render.yaml`
- Docker 镜像：`Dockerfile`

## 下一步建议

这版已经可以作为免费公网 MVP 上线。

如果你下一步继续增强，最值得加的是：

1. PostgreSQL schema migration 工具
2. Cloudinary 上传失败重试
3. 管理员修改用户资料
4. Telegram Bot 登录后录入
