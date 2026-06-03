"""
GitHub Webhook 监听服务 + Cursor Agent 管理面板
FastAPI (Webhook) + Gradio (UI) 集成服务
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import gradio as gr
import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from github import Github, GithubException

# ─── 环境变量 ─────────────────────────────────────────────────────────────────
load_dotenv()

GITHUB_WEBHOOK_SECRET: str = os.getenv("GITHUB_WEBHOOK_SECRET", "")
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
CURSOR_API_KEY: str = os.getenv("CURSOR_API_KEY", "")
NGROK_TOKEN: str = os.getenv("NGROK_TOKEN", "")
NGROK_DOMAIN: str = os.getenv("NGROK_DOMAIN", "")
PORT: int = int(os.getenv("PORT", "7860"))

ROLES_CONFIG_PATH = Path("roles_config.json")
LOG_FILE = Path("app.log")

# ─── 日志配置 ──────────────────────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=DATE_FORMAT,
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("webhook_service")


class FileHandler(logging.FileHandler):
    """同时写入文件的日志处理器"""
    pass


file_handler = FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
logger.addHandler(file_handler)

# 同时捕获 uvicorn 日志
for uvicorn_logger_name in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
    uvicorn_log = logging.getLogger(uvicorn_logger_name)
    uvicorn_log.addHandler(file_handler)


# ─── 角色配置管理 ──────────────────────────────────────────────────────────────

def load_roles_config() -> dict:
    """从文件加载角色配置"""
    if not ROLES_CONFIG_PATH.exists():
        default = {"roles": {}, "updated_at": datetime.now(timezone.utc).isoformat()}
        save_roles_config(default)
        return default
    with open(ROLES_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_roles_config(config: dict) -> None:
    """保存角色配置到文件"""
    config["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(ROLES_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


# ─── Cursor Agent API ──────────────────────────────────────────────────────────

CURSOR_API_URL = "https://api.cursor.com/v1/agents"


async def call_cursor_agent(
    prompt_text: str,
    repo_url: str,
    target_branch: str,
) -> dict[str, Any]:
    """调用 Cursor Agent API（异步）"""
    if not CURSOR_API_KEY:
        logger.error("CURSOR_API_KEY 未配置，跳过 Cursor API 调用")
        return {"error": "CURSOR_API_KEY not configured"}

    # 清理 repo URL（移除 .git 后缀和 auth token）
    clean_repo_url = re.sub(r"https://[^@]+@", "https://", repo_url)
    clean_repo_url = clean_repo_url.rstrip("/")
    if clean_repo_url.endswith(".git"):
        clean_repo_url = clean_repo_url[:-4]

    payload = {
        "prompt": {
            "text": prompt_text
        },
        "repos": [
            {
                "url": clean_repo_url,
                "startingRef": target_branch,
            }
        ],
    }

    headers = {
        "Authorization": f"Bearer {CURSOR_API_KEY}",
        "Content-Type": "application/json",
    }

    logger.info(
        "Cursor API 请求 | URL: %s | Repo: %s | Branch: %s | Prompt 前100字: %s",
        CURSOR_API_URL,
        clean_repo_url,
        target_branch,
        prompt_text[:100].replace("\n", " "),
    )
    logger.debug("Cursor API Payload: %s", json.dumps(payload, ensure_ascii=False))

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            response = await client.post(
                CURSOR_API_URL,
                headers=headers,
                json=payload,
            )
            response_data = response.json() if response.content else {}
            logger.info(
                "Cursor API 响应 | 状态码: %d | 响应摘要: %s",
                response.status_code,
                str(response_data)[:200],
            )
            return {
                "status_code": response.status_code,
                "response": response_data,
            }
        except httpx.TimeoutException:
            logger.error("Cursor API 调用超时")
            return {"error": "Request timeout"}
        except Exception as e:
            logger.error("Cursor API 调用异常: %s", str(e))
            return {"error": str(e)}


# ─── GitHub 工具函数 ────────────────────────────────────────────────────────────

def get_pr_diff(repo_full_name: str, pr_number: int) -> str:
    """通过 PyGithub 获取 PR 的 diff 内容"""
    if not GITHUB_TOKEN:
        logger.warning("GITHUB_TOKEN 未配置，无法获取 PR diff")
        return "(GITHUB_TOKEN not configured)"
    try:
        gh = Github(GITHUB_TOKEN)
        repo = gh.get_repo(repo_full_name)
        pr = repo.get_pull(pr_number)
        files = pr.get_files()
        diff_parts = []
        for f in files:
            diff_parts.append(f"### {f.filename} (+{f.additions} -{f.deletions})")
            if f.patch:
                diff_parts.append(f"```diff\n{f.patch}\n```")
        return "\n".join(diff_parts) if diff_parts else "(no diff available)"
    except GithubException as e:
        logger.error("获取 PR diff 失败: %s", str(e))
        return f"(error fetching diff: {e})"


def post_pr_comment(repo_full_name: str, pr_number: int, comment_body: str) -> bool:
    """向 PR 发布评论"""
    if not GITHUB_TOKEN:
        logger.warning("GITHUB_TOKEN 未配置，无法发布 PR 评论")
        return False
    try:
        gh = Github(GITHUB_TOKEN)
        repo = gh.get_repo(repo_full_name)
        pr = repo.get_pull(pr_number)
        pr.create_issue_comment(comment_body)
        logger.info("已向 PR #%d 发布评论", pr_number)
        return True
    except GithubException as e:
        logger.error("发布 PR 评论失败: %s", str(e))
        return False


def ensure_dev_branch(repo_full_name: str) -> str:
    """确保 dev 分支存在，不存在则从主分支创建，返回 dev 分支名"""
    if not GITHUB_TOKEN:
        return "main"
    try:
        gh = Github(GITHUB_TOKEN)
        repo = gh.get_repo(repo_full_name)
        branches = [b.name for b in repo.get_branches()]
        if "dev" in branches:
            return "dev"
        # 找到默认分支
        default_branch = repo.default_branch
        source_sha = repo.get_branch(default_branch).commit.sha
        repo.create_git_ref(ref="refs/heads/dev", sha=source_sha)
        logger.info("已基于 %s 创建 dev 分支 (repo: %s)", default_branch, repo_full_name)
        return "dev"
    except GithubException as e:
        logger.error("确保 dev 分支失败: %s", str(e))
        return "dev"


# ─── Webhook 核心处理逻辑 ──────────────────────────────────────────────────────

def build_prompt(role_config: dict, pr_info: dict) -> str:
    """将 PR 信息填充到角色 Prompt 模板中"""
    template: str = role_config.get("prompt_template", "{pr_title}\n{pr_body}")
    return template.format(
        repo_url=pr_info.get("repo_url", ""),
        pr_title=pr_info.get("pr_title", ""),
        pr_body=pr_info.get("pr_body", "") or "",
        base_branch=pr_info.get("base_branch", "main"),
        pr_author=pr_info.get("pr_author", ""),
        pr_diff=pr_info.get("pr_diff", ""),
        pr_number=pr_info.get("pr_number", 0),
    )


async def handle_labeled_pr(pr_info: dict, role_config: dict) -> None:
    """后台任务：处理被打了 label 的 PR"""
    label = role_config["label"]
    action = role_config.get("action", "review")
    repo_full = pr_info["repo_full_name"]
    pr_number = pr_info["pr_number"]

    logger.info(
        "开始处理 PR #%d [%s] (repo: %s, action: %s)",
        pr_number, label, repo_full, action
    )

    # 获取 diff
    pr_diff = get_pr_diff(repo_full, pr_number)
    pr_info["pr_diff"] = pr_diff

    # 构建 prompt
    prompt_text = build_prompt(role_config, pr_info)

    # 确定目标分支
    if action == "edit":
        target_branch = ensure_dev_branch(repo_full)
    else:
        target_branch = pr_info.get("base_branch", "main")

    # 调用 Cursor Agent
    result = await call_cursor_agent(
        prompt_text=prompt_text,
        repo_url=pr_info["repo_url"],
        target_branch=target_branch,
    )

    # c-review：将结果作为 PR 评论回写
    if action == "review" and GITHUB_TOKEN:
        agent_response_text = result.get("response", {})
        if isinstance(agent_response_text, dict):
            agent_response_text = json.dumps(agent_response_text, ensure_ascii=False, indent=2)

        comment = (
            f"## 🤖 Cursor Agent Code Review\n\n"
            f"**Label触发**：`{label}`\n\n"
            f"**Agent 响应**：\n\n{agent_response_text}\n\n"
            f"---\n*由 Cursor Agent 自动生成 @ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*"
        )
        post_pr_comment(repo_full, pr_number, comment)

    logger.info(
        "PR #%d [%s] 处理完成，Cursor API 状态: %s",
        pr_number, label, result.get("status_code", result.get("error", "unknown"))
    )


# ─── ngrok 生命周期 ────────────────────────────────────────────────────────────

ngrok_tunnel = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期：启动时开启 ngrok，关闭时停止"""
    global ngrok_tunnel
    if NGROK_TOKEN:
        try:
            from pyngrok import conf, ngrok

            conf.get_default().auth_token = NGROK_TOKEN
            options: dict[str, Any] = {"addr": PORT}
            if NGROK_DOMAIN:
                options["hostname"] = NGROK_DOMAIN
            ngrok_tunnel = ngrok.connect(**options)
            public_url = ngrok_tunnel.public_url
            webhook_url = f"{public_url}/webhook"
            logger.info("ngrok 隧道已建立: %s", public_url)
            logger.info("GitHub Webhook Payload URL: %s", webhook_url)
        except Exception as e:
            logger.error("ngrok 启动失败: %s", str(e))
    else:
        logger.warning("NGROK_TOKEN 未配置，跳过 ngrok 启动")

    yield

    if ngrok_tunnel:
        try:
            from pyngrok import ngrok
            ngrok.disconnect(ngrok_tunnel.public_url)
            logger.info("ngrok 隧道已关闭")
        except Exception as e:
            logger.error("ngrok 关闭失败: %s", str(e))


# ─── FastAPI 应用 ──────────────────────────────────────────────────────────────

fastapi_app = FastAPI(
    title="GitHub Webhook + Cursor Agent Service",
    description="监听 GitHub PR Label 事件，动态调用 Cursor Agent 执行 AI 任务",
    version="1.0.0",
    lifespan=lifespan,
)


def verify_github_signature(payload_bytes: bytes, signature_header: Optional[str]) -> bool:
    """校验 GitHub Webhook 签名 (X-Hub-Signature-256)"""
    if not GITHUB_WEBHOOK_SECRET:
        logger.warning("GITHUB_WEBHOOK_SECRET 未配置，跳过签名校验")
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        GITHUB_WEBHOOK_SECRET.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    actual = signature_header[len("sha256="):]
    return hmac.compare_digest(expected, actual)


@fastapi_app.post("/webhook")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    """GitHub Webhook 接收端点"""
    payload_bytes = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    event_type = request.headers.get("X-GitHub-Event", "")

    # 签名校验
    if not verify_github_signature(payload_bytes, signature):
        logger.warning("Webhook 签名校验失败，拒绝请求")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 仅处理 pull_request 事件
    if event_type != "pull_request":
        return JSONResponse({"status": "ignored", "reason": f"event type '{event_type}' not handled"})

    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    action = payload.get("action", "")
    if action != "labeled":
        return JSONResponse({"status": "ignored", "reason": f"action '{action}' not handled"})

    label_name: str = payload.get("label", {}).get("name", "")
    pr_data = payload.get("pull_request", {})
    repo_data = payload.get("repository", {})

    pr_info = {
        "pr_number": pr_data.get("number"),
        "pr_title": pr_data.get("title", ""),
        "pr_body": pr_data.get("body", ""),
        "pr_author": pr_data.get("user", {}).get("login", ""),
        "base_branch": pr_data.get("base", {}).get("ref", "main"),
        "head_branch": pr_data.get("head", {}).get("ref", ""),
        "repo_full_name": repo_data.get("full_name", ""),
        "repo_url": repo_data.get("html_url", repo_data.get("clone_url", "")),
    }

    logger.info(
        "收到 Webhook | 事件: pull_request | action: labeled | label: %s | PR #%d | repo: %s",
        label_name,
        pr_info["pr_number"],
        pr_info["repo_full_name"],
    )

    # 读取最新角色配置
    config = load_roles_config()
    roles = config.get("roles", {})

    if label_name not in roles:
        logger.info("Label '%s' 未在角色配置中定义，忽略", label_name)
        return JSONResponse({"status": "ignored", "reason": f"label '{label_name}' not configured"})

    role_config = roles[label_name]
    if not role_config.get("enabled", True):
        logger.info("Label '%s' 对应角色已禁用，忽略", label_name)
        return JSONResponse({"status": "ignored", "reason": f"label '{label_name}' is disabled"})

    # 立即返回 200，将耗时操作放入后台
    background_tasks.add_task(handle_labeled_pr, pr_info, role_config)
    return JSONResponse({"status": "accepted", "label": label_name, "pr": pr_info["pr_number"]})


@fastapi_app.get("/health")
async def health_check():
    """健康检查端点"""
    tunnel_url = ngrok_tunnel.public_url if ngrok_tunnel else None
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ngrok_url": tunnel_url,
        "webhook_url": f"{tunnel_url}/webhook" if tunnel_url else None,
    }


# ─── Gradio UI ─────────────────────────────────────────────────────────────────

CSS = """
.header-md { text-align: center; padding: 10px 0; }
.label-badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: bold;
    background: #1f6feb;
    color: white;
    margin: 2px;
}
.status-ok { color: #3fb950; font-weight: bold; }
.status-warn { color: #d29922; font-weight: bold; }
"""

HEADER_MD = """
# 🤖 Cursor Agent Webhook 管理面板

**GitHub Webhook 监听服务** — 根据 PR Label 动态调用 Cursor Agent 执行 AI 任务

---
"""


def get_roles_table_data() -> list[list]:
    """获取角色列表用于 Dataframe 展示"""
    config = load_roles_config()
    rows = []
    for label, role in config.get("roles", {}).items():
        rows.append([
            label,
            role.get("description", ""),
            role.get("action", "review"),
            "✅ 启用" if role.get("enabled", True) else "❌ 禁用",
            role.get("created_at", ""),
        ])
    return rows if rows else [["(暂无配置)", "", "", "", ""]]


def refresh_roles():
    """刷新角色列表"""
    data = get_roles_table_data()
    return data


def save_role(label: str, description: str, prompt_template: str, action: str, enabled: bool) -> str:
    """新增或更新角色配置"""
    label = label.strip()
    if not label:
        return "❌ Label 不能为空"
    if not prompt_template.strip():
        return "❌ Prompt 模板不能为空"

    config = load_roles_config()
    roles = config.get("roles", {})
    is_new = label not in roles

    roles[label] = {
        "label": label,
        "description": description.strip(),
        "prompt_template": prompt_template.strip(),
        "action": action,
        "enabled": enabled,
        "created_at": roles.get(label, {}).get("created_at", datetime.now(timezone.utc).isoformat()),
    }
    config["roles"] = roles
    save_roles_config(config)

    action_word = "新增" if is_new else "更新"
    logger.info("角色配置已%s: label=%s, action=%s", action_word, label, action)
    return f"✅ 角色 `{label}` 已{action_word}保存"


def delete_role(label: str) -> tuple[str, list]:
    """删除角色配置"""
    label = label.strip()
    if not label:
        return "❌ 请输入要删除的 Label", get_roles_table_data()

    config = load_roles_config()
    roles = config.get("roles", {})
    if label not in roles:
        return f"❌ Label `{label}` 不存在", get_roles_table_data()

    del roles[label]
    config["roles"] = roles
    save_roles_config(config)
    logger.info("角色配置已删除: label=%s", label)
    return f"✅ Label `{label}` 已删除", get_roles_table_data()


def load_role_for_edit(label: str) -> tuple:
    """加载指定 label 的配置到编辑表单"""
    label = label.strip()
    config = load_roles_config()
    role = config.get("roles", {}).get(label)
    if not role:
        return "", "", "", "review", True
    return (
        role.get("label", ""),
        role.get("description", ""),
        role.get("prompt_template", ""),
        role.get("action", "review"),
        role.get("enabled", True),
    )


def read_log_tail(n_lines: int = 100) -> str:
    """读取日志文件最后 n 行"""
    if not LOG_FILE.exists():
        return "(日志文件不存在)"
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        tail = lines[-n_lines:] if len(lines) > n_lines else lines
        return "".join(tail)
    except Exception as e:
        return f"(读取日志失败: {e})"


def get_service_status() -> str:
    """获取服务状态摘要"""
    tunnel_url = ngrok_tunnel.public_url if ngrok_tunnel else None
    lines = [
        f"**服务状态**: {'🟢 运行中' if True else '🔴 停止'}",
        f"**ngrok 隧道**: {tunnel_url or '⚠️ 未启动 (NGROK_TOKEN 未配置)'}",
        f"**Webhook URL**: {f'{tunnel_url}/webhook' if tunnel_url else '—'}",
        f"**Cursor API**: {'✅ 已配置' if CURSOR_API_KEY else '❌ 未配置 (CURSOR_API_KEY)'}",
        f"**GitHub Token**: {'✅ 已配置' if GITHUB_TOKEN else '❌ 未配置 (GITHUB_TOKEN)'}",
        f"**Webhook Secret**: {'✅ 已配置' if GITHUB_WEBHOOK_SECRET else '⚠️ 未配置 (签名校验已跳过)'}",
        f"**角色配置数量**: {len(load_roles_config().get('roles', {}))} 个",
        f"**当前时间**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    return "\n\n".join(lines)


def build_gradio_ui() -> gr.Blocks:
    with gr.Blocks(title="Cursor Agent 管理面板", css=CSS) as demo:
        gr.Markdown(HEADER_MD, elem_classes="header-md")

        with gr.Tabs():
            # ── Tab 1: 服务状态 ──────────────────────────────────────────────
            with gr.TabItem("📊 服务状态"):
                status_md = gr.Markdown(get_service_status())
                refresh_status_btn = gr.Button("🔄 刷新状态", variant="secondary", size="sm")
                refresh_status_btn.click(fn=get_service_status, outputs=status_md)

            # ── Tab 2: 角色管理 ──────────────────────────────────────────────
            with gr.TabItem("⚙️ 角色管理"):
                gr.Markdown("### 当前已配置的 Label → Role 映射")

                roles_table = gr.Dataframe(
                    headers=["Label", "描述", "动作类型", "状态", "创建时间"],
                    value=get_roles_table_data(),
                    interactive=False,
                    wrap=True,
                )
                refresh_table_btn = gr.Button("🔄 刷新列表", variant="secondary", size="sm")
                refresh_table_btn.click(fn=refresh_roles, outputs=roles_table)

                gr.Markdown("---")
                gr.Markdown("### 新增 / 修改角色配置")

                with gr.Row():
                    with gr.Column(scale=1):
                        edit_label = gr.Textbox(label="Label 名称", placeholder="例如: c-review")
                        load_btn = gr.Button("📂 加载已有配置", variant="secondary", size="sm")
                    with gr.Column(scale=2):
                        edit_desc = gr.Textbox(
                            label="描述",
                            placeholder="简述该 Label 触发后执行的操作",
                        )

                edit_action = gr.Radio(
                    choices=["review", "edit"],
                    value="review",
                    label="动作类型",
                    info="review: 代码审查并回复评论 | edit: 自动修改代码并提交",
                )
                edit_enabled = gr.Checkbox(label="启用此角色", value=True)
                edit_prompt = gr.Textbox(
                    label="Prompt 模板",
                    placeholder="支持变量: {repo_url}, {pr_title}, {pr_body}, {base_branch}, {pr_author}, {pr_diff}, {pr_number}",
                    lines=12,
                    max_lines=30,
                )

                with gr.Row():
                    save_btn = gr.Button("💾 保存角色", variant="primary")
                    delete_label_input = gr.Textbox(
                        label="删除 Label（输入名称后点击删除）",
                        placeholder="要删除的 label 名称",
                        scale=2,
                    )
                    delete_btn = gr.Button("🗑️ 删除角色", variant="stop")

                save_result = gr.Markdown("")

                load_btn.click(
                    fn=load_role_for_edit,
                    inputs=[edit_label],
                    outputs=[edit_label, edit_desc, edit_prompt, edit_action, edit_enabled],
                )
                save_btn.click(
                    fn=save_role,
                    inputs=[edit_label, edit_desc, edit_prompt, edit_action, edit_enabled],
                    outputs=[save_result],
                ).then(fn=refresh_roles, outputs=roles_table)

                delete_btn.click(
                    fn=delete_role,
                    inputs=[delete_label_input],
                    outputs=[save_result, roles_table],
                )

            # ── Tab 3: 实时日志 ──────────────────────────────────────────────
            with gr.TabItem("📋 实时日志"):
                gr.Markdown("### 服务运行日志（最近 200 行）")

                log_lines_slider = gr.Slider(
                    minimum=20, maximum=500, value=100, step=10,
                    label="显示行数",
                )
                log_display = gr.Code(
                    value=read_log_tail(100),
                    language=None,
                    label="app.log",
                    lines=30,
                    interactive=False,
                )

                with gr.Row():
                    refresh_log_btn = gr.Button("🔄 刷新日志", variant="secondary")
                    auto_refresh = gr.Checkbox(label="每5秒自动刷新", value=False)

                refresh_log_btn.click(
                    fn=read_log_tail,
                    inputs=[log_lines_slider],
                    outputs=[log_display],
                )

                # 定时自动刷新（通过 gr.Timer）
                timer = gr.Timer(value=5, active=False)
                auto_refresh.change(
                    fn=lambda active: gr.Timer(value=5, active=active),
                    inputs=[auto_refresh],
                    outputs=[timer],
                )
                timer.tick(
                    fn=read_log_tail,
                    inputs=[log_lines_slider],
                    outputs=[log_display],
                )

            # ── Tab 4: 使用说明 ──────────────────────────────────────────────
            with gr.TabItem("📖 使用说明"):
                gr.Markdown("""
## 快速开始

### 1. 环境变量配置
复制 `.env.example` 为 `.env` 并填写以下变量：

| 变量名 | 说明 | 是否必需 |
|--------|------|----------|
| `CURSOR_API_KEY` | Cursor API 密钥 | ✅ 必需 |
| `GITHUB_TOKEN` | GitHub Personal Access Token（需 `repo` 权限） | ✅ 必需 |
| `GITHUB_WEBHOOK_SECRET` | GitHub Webhook 密钥（建议设置） | ⚠️ 推荐 |
| `NGROK_TOKEN` | ngrok 认证 Token | ✅ 必需（公网访问） |
| `NGROK_DOMAIN` | ngrok 自定义域名（可选） | ❌ 可选 |

### 2. 启动服务
```bash
# 使用 Docker Compose（推荐）
docker compose up -d

# 或本地直接运行
pip install -r requirements.txt
python app.py
```

### 3. 配置 GitHub Webhook
1. 进入你的 GitHub 仓库 → **Settings** → **Webhooks** → **Add webhook**
2. **Payload URL**: 填写 `https://<your-ngrok-domain>/webhook`
   （服务启动后在「服务状态」Tab 中查看）
3. **Content type**: 选择 `application/json`
4. **Secret**: 填写与 `GITHUB_WEBHOOK_SECRET` 相同的值
5. **Events**: 选择 **Let me select individual events** → 勾选 **Pull requests**

### 4. 触发 Agent
在目标仓库的 PR 上添加以下 Label：
- `c-review` → 触发代码审查，结果自动回复为 PR 评论
- `c-edit` → 触发代码自动修改和提交

### 5. 自定义 Label
在「角色管理」Tab 中新增/修改 Label 配置，修改立即生效无需重启。

---

## Prompt 模板变量说明

| 变量 | 说明 |
|------|------|
| `{repo_url}` | 仓库 URL |
| `{pr_title}` | PR 标题 |
| `{pr_body}` | PR 描述内容 |
| `{base_branch}` | 目标合并分支 |
| `{pr_author}` | PR 作者 |
| `{pr_diff}` | PR 代码差异（自动获取） |
| `{pr_number}` | PR 编号 |
""")

    return demo


# ─── 挂载 Gradio 到 FastAPI ─────────────────────────────────────────────────────

gradio_app = build_gradio_ui()
app = gr.mount_gradio_app(fastapi_app, gradio_app, path="/")


# ─── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    logger.info("启动 Cursor Agent Webhook 服务，端口: %d", PORT)
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
        log_level="info",
    )
