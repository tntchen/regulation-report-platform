# NAS 部署说明

> 适用：群晖 NAS 单容器自托管部署，Watchtower 自动更新

## 快速部署

```bash
# 1. 在 NAS 上创建目录
mkdir -p ~/regulation-platform && cd ~/regulation-platform

# 2. 放入 compose 和 .env
cp deploy/docker-compose.yml ./docker-compose.yml
cp deploy/.env.example ./.env
# 编辑 .env，务必修改 SECRET_KEY

# 3. 首次启动
/usr/local/bin/docker compose up -d

# 4. 验证
/usr/local/bin/docker compose ps
/usr/local/bin/docker logs -f regulation-platform
# 健康检查
curl http://localhost:8080/health
```

## 初始化数据

首次部署后，执行种子脚本初始化演示租户和制度文档：

```bash
/usr/local/bin/docker exec regulation-platform python scripts/seed_tenants.py
/usr/local/bin/docker exec regulation-platform python scripts/seed_regulations.py
```

## 日常发布流程

```
代码推送 main → GitHub Actions 手动触发 release → 构建镜像推 Docker Hub
→ watchtower 5 分钟内自动 pull + recreate NAS 容器
→ 验证：docker exec regulation-platform cat /app/VERSION
```

## 回滚

编辑 `docker-compose.yml`，将 `image:` 改为具体旧版本号（如 `:0.3.0`），然后 `compose up -d`。
恢复追新：改回 `:latest` 即可。

## 注意事项

- `.env` 中的 `SECRET_KEY` 生产环境必须修改为随机字符串
- 数据持久化在 Docker volume `app_data` 中，路径 `/app/data`
- 演示模式默认开启（`AI_MOCK_MODE=true`），接入真实 AI 需配置 `AI_BASE_URL` + `AI_API_KEY` 并关闭 mock
