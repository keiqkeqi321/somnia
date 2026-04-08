# 07 - Docker 部署

## 概述

Docker 方案让用户**零环境依赖**运行 Somnia，不需要安装 Python 或 Node.js。

## Dockerfile

位置：`OpenAgent/Dockerfile`

```dockerfile
FROM python:3.12-slim

# 系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# 复制并安装
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

ENTRYPOINT ["somnia"]
CMD []
```

## 构建与运行

### 构建镜像

```bash
cd OpenAgent
docker build -t somnia .
```

### 交互式运行

```bash
docker run -it somnia
```

### 单次对话

```bash
docker run -it somnia chat "你好"
```

### 带环境变量（API Key 等）

```bash
docker run -it -e ANTHROPIC_API_KEY=sk-xxx somnia
```

### 挂载工作目录

```bash
docker run -it -v $(pwd):/workspace -w /workspace somnia
```

### 从 GitHub 直接构建（不 clone）

```bash
docker build -t somnia https://github.com/keiqkeqi321/learn-claude-code.git#OpenAgent_by_codex:OpenAgent
```

## 发布到 Docker Hub（可选）

如果需要让用户直接 `docker pull`：

### 1. 登录 Docker Hub

```bash
docker login
```

### 2. 打 tag 并推送

```bash
docker tag somnia:latest your-dockerhub-user/somnia:latest
docker tag somnia:latest your-dockerhub-user/somnia:0.3.2

docker push your-dockerhub-user/somnia:latest
docker push your-dockerhub-user/somnia:0.3.2
```

### 3. 用户使用

```bash
docker pull your-dockerhub-user/somnia
docker run -it your-dockerhub-user/somnia
```

## .dockerignore

位置：`OpenAgent/.dockerignore`

```
.git
.github
__pycache__
*.pyc
*.egg-info
dist
build
tests
*.md
scripts
npm
.openagent*
```

减小镜像体积，只打包运行所需文件。

## 常见问题

### 1. 构建慢

Docker 首次构建需要下载基础镜像，后续利用缓存会快很多。如果代码没变，Docker 会复用之前的层。

### 2. 代理配置

```bash
docker build --build-arg HTTP_PROXY=http://proxy:port \
             --build-arg HTTPS_PROXY=http://proxy:port \
             -t somnia .
```

### 3. 镜像体积优化

当前基于 `python:3.12-slim`，镜像约 150MB。如需更小：

```dockerfile
FROM python:3.12-alpine   # ~50MB，但可能有兼容问题
```
