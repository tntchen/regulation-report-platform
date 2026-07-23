FROM python:3.10-slim

WORKDIR /app

# 安装系统依赖（curl 用于 HEALTHCHECK）
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY backend/ ./backend/

# 创建运行时数据目录（数据通过 volume 挂载，不在镜像中）
RUN mkdir -p /app/data/tenants /app/data/tasks /app/data/logs

# 暴露端口
EXPOSE 8080

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fs http://localhost:8080/health || exit 1

# 启动命令（生产环境去掉 --reload）
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080"]
