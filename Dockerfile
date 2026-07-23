FROM python:3.10-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY backend/ ./backend/
COPY data/ ./data/

# 创建数据目录
RUN mkdir -p /app/data/tenants

# 暴露端口
EXPOSE 8080

# 启动命令
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080", "--reload"]
