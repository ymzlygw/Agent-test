# 🤖 GitHub Webhook + Cursor Agent 管理面板

基于 **FastAPI + Gradio** 的 GitHub Webhook 监听服务，根据 PR 被打上的不同 Label，动态调用 **Cursor Agent API** 执行 AI 代理任务，并通过 **ngrok 内网穿透**暴露到公网。

---

## 功能特性

- **Webhook 监听**：接收 GitHub `pull_request` + `labeled` 事件，立即返回 `200 OK`，后台异步处理
- **签名校验**：验证 `X-Hub-Signature-256`，防止伪造请求
- **动态角色配置**：通过 Gradio 界面新增/修改/删除 Label → Prompt 映射，无需重启
- **Cursor Agent 调用**：严格按照 API 规范组装请求体，完整记录请求与响应
- **自动代码审查**（`c-review`）：获取 PR diff，调用 Agent，结果自动回复为 PR 评论
- **自动代码修改**（`c-edit`）：调用 Agent 拉取代码、修改、测试、提交并合并到 `dev` 分支
- **实时日志面板**：Gradio 界面内嵌日志查看器，支持自动刷新
- **ngrok 集成**：服务启动时自动建立隧道，日志中打印 Webhook URL
- **Docker 支持**：一键 `docker compose up -d` 启动，日志和配置文件持久化

---

## 快速开始

### 方式一：Docker Compose（推荐）

**1. 克隆并配置**

```bash
git clone <this-repo>
cd <this-repo>

# 复制并填写环境变量
cp .env.example .env
nano .env
```

**2. 创建持久化数据目录**

```bash
mkdir -p data
# 复制默认角色配置到持久化目录
cp roles_config.json data/roles_config.json
touch data/app.log
```

**3. 启动服务**

```bash
docker compose up -d
```

**4. 查看日志**

```bash
docker compose logs -f
```

**5. 访问 Gradio 管理面板**

浏览器打开 `http://localhost:7860`

---

### 方式二：本地直接运行

**1. 安装依赖**

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**2. 配置环境变量**

```bash
cp .env.example .env
# 编辑 .env 填入各项密钥
```

**3. 启动服务**

```bash
python app.py
```

---

## 环境变量说明

| 变量名 | 说明 | 是否必需 |
|--------|------|:--------:|
| `CURSOR_API_KEY` | Cursor API 密钥 | ✅ |
| `GITHUB_TOKEN` | GitHub PAT（需 `repo` 权限） | ✅ |
| `GITHUB_WEBHOOK_SECRET` | Webhook 签名密钥（建议设置） | ⚠️ |
| `NGROK_TOKEN` | ngrok 认证 Token | ✅ |
| `NGROK_DOMAIN` | ngrok 自定义域名（付费账户） | ❌ |
| `PORT` | 服务端口（默认 `7860`） | ❌ |

---

## GitHub Webhook 配置指南

1. 进入目标仓库 → **Settings** → **Webhooks** → **Add webhook**

2. 填写以下配置：

   | 字段 | 值 |
   |------|-----|
   | **Payload URL** | `https://<your-ngrok-domain>/webhook` |
   | **Content type** | `application/json` |
   | **Secret** | 与 `GITHUB_WEBHOOK_SECRET` 相同的值 |
   | **Which events** | 选择 `Let me select individual events` → 勾选 **Pull requests** |

3. 点击 **Add webhook**

> **提示**：服务启动后，ngrok 公网 URL 会打印在日志中，也可在管理面板的「服务状态」Tab 中查看。

---

## 默认 Label 定义

| Label | 触发动作 | 说明 |
|-------|----------|------|
| `c-review` | 代码审查 | 分析 PR diff，找出潜在 Bug 和改进点，以评论形式回复 PR |
| `c-edit` | 自动修改 | 根据 PR 描述自动修改代码、编写测试、提交并合并到 `dev` 分支 |

> **自定义**：在管理面板的「角色管理」Tab 中新增/修改 Label 配置，修改立即生效。

---

## Cursor Agent API 规范

服务调用 Cursor Agent API 时严格使用以下结构：

```
POST https://api.cursor.com/v1/agents
Authorization: Bearer <CURSOR_API_KEY>
Content-Type: application/json

{
  "prompt": {
    "text": "<组装好的 PROMPT_TEXT>"
  },
  "repos": [
    {
      "url": "<CLEAN_REPO_URL>",
      "startingRef": "<TARGET_BRANCH>"
    }
  ]
}
```

---

## 项目结构

```
.
├── app.py                # 核心服务（FastAPI + Gradio + Webhook 处理）
├── roles_config.json     # 角色配置（由界面动态管理，不纳入版本控制）
├── requirements.txt      # Python 依赖
├── Dockerfile            # Docker 镜像构建文件
├── docker-compose.yml    # Docker Compose 编排
├── .env.example          # 环境变量示例（复制为 .env 使用）
├── .gitignore            # Git 忽略规则
├── README.md             # 本文档
└── data/                 # 持久化数据目录（不纳入版本控制）
    ├── roles_config.json # 持久化角色配置
    └── app.log           # 服务运行日志
```

---

## 注意事项

- `.env` 文件包含敏感密钥，已加入 `.gitignore`，**请勿提交到版本控制**
- `roles_config.json` 在 `.gitignore` 中（运行时动态修改），`data/` 目录为持久化目录
- Webhook 响应立即返回 `200 OK`，Cursor API 调用在后台异步执行，不会触发 GitHub 超时重试
- `c-edit` action 会确保目标仓库存在 `dev` 分支（不存在时自动从默认分支创建）
