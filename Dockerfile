FROM python:3.10-slim

# 安装系统级依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 预置 ngrok 二进制文件（加速启动，避免运行时下载）
ARG NGROK_VERSION=3
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then NGROK_ARCH="amd64"; \
    elif [ "$ARCH" = "aarch64" ]; then NGROK_ARCH="arm64"; \
    else NGROK_ARCH="amd64"; fi && \
    curl -sSL "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-${NGROK_ARCH}.tgz" \
    -o /tmp/ngrok.tgz && \
    tar -xzf /tmp/ngrok.tgz -C /usr/local/bin && \
    rm /tmp/ngrok.tgz && \
    chmod +x /usr/local/bin/ngrok

WORKDIR /app

# 先复制依赖文件，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY app.py .

# 复制默认角色配置（容器内作为初始值，会被挂载卷覆盖）
COPY roles_config.json .

# 创建持久化数据目录
RUN mkdir -p /data && \
    ln -sf /data/roles_config.json /app/roles_config.json || true && \
    ln -sf /data/app.log /app/app.log || true

EXPOSE 7860

# 使用 shell 形式以支持环境变量替换
CMD ["python", "app.py"]
