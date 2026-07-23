# 环境验证清单（待 NAS 环境执行）

> 记录日期：2026-07-23
> 背景：本机 Docker 不可用，MySQL 真实链路代码已就绪但未实测。用户提供一台 **NAS 可用于 Docker 部署与 MySQL 安装**，以下事项需在该环境一并验证。

## 一、NAS 环境准备

- [ ] NAS 上安装/确认 Docker 与 docker compose
- [ ] 确认 NAS 与开发机网络互通（端口映射、防火墙）
- [ ] （可选）NAS 上部署 MySQL 8 容器，创建演示库与 `mcp_readonly` 只读账号

## 二、MySQL 真实链路验证（Day 6-7 遗留）

- [ ] 执行 `scripts/seed_mysql.py`：建表 + 灌入演示数据 + 创建只读账号
- [ ] 只读账号验证：`mcp_readonly` 执行写操作被数据库层拒绝（第二道防线实测）
- [ ] `query_schema` 走 information_schema 返回真实表结构（含列注释 COMMENT）
- [ ] `execute_sql` 真实执行：行数上限截断、10s 语句超时生效
- [ ] AST 白名单在 MySQL 方言下复测（45 个用例中的方言相关项）
- [ ] 6Agent 全链路 on MySQL：门禁 pass + 数字孪生差异勾稽（EAST vs 1104 = 15,600 元）
- [ ] Agent 4 测试验证切换 MySQL 执行（方言真实化，替换 SQLite 语法级验证）

## 三、Docker 部署验证（运维向）

- [ ] `docker-compose up -d` 整站起服务（backend + MySQL）
- [ ] Dockerfile 整改验证：`--reload` 移除、data/ 不打入镜像（L3 项，可提前验证）
- [ ] /health 深度检查在容器内正常（DB/向量目录/AI 连通）
- [ ] 前端 dist 托管方式验证（nginx 容器或后端静态托管）

## 四、回归

- [ ] 全套 pytest + M1/M2/M3 冒烟在 NAS 部署环境跑通
- [ ] 演示脚本（任务大厅一键演示）在 NAS 部署上完整走一遍

---

## 更新记录

- **2026-07-30（L2 Day 10）**：本清单已被 README「边界与能力声明」引用为
  MySQL 真实链路实测的唯一权威清单；NAS 环境就绪后按上文逐项打勾并回填结果。
