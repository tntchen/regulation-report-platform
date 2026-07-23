"""
监管接口文件导出测试（范围B）
覆盖：TXT/XML 生成格式正确、行数与结果表一致、文件列表、
     下载白名单拦截路径穿越、任务非 completed → 409、审计 task.export

运行方式: python -m pytest tests/test_interface_file.py -v
"""

import asyncio
import os
import tempfile

# 在导入 app 之前切换到临时测试库 + 固定 JWT 密钥（与其他测试同模式）
_tmpdir = tempfile.mkdtemp(prefix="rrp_test_export_")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_tmpdir}/test_platform.db")
os.environ.setdefault("UPLOAD_DIR", f"{_tmpdir}/tenants")
os.environ.setdefault("DEMO_DB_PATH", f"{_tmpdir}/demo_biz.db")
os.environ.setdefault("TASK_WORK_DIR", f"{_tmpdir}/tasks")
os.environ.setdefault("LOG_DIR", f"{_tmpdir}/logs")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
os.environ.setdefault("DEBUG", "false")

import pytest
from fastapi.testclient import TestClient

from backend.config import settings
from backend.main import app
from backend.api.export import export_router
from backend.services import task_service, interface_file_service
from backend.mcp.demo_dataset import demo_dataset

settings.task_worker_enabled = False

# 路由注册由协调者统一做（main.py 归 C）；测试内手动挂载本范围路由
app.include_router(export_router, prefix="/v1")

TASK_ID = "TASK_EXPORT_T001"
TARGET_TABLE = "rpt_g01_housing_loan"

# 结果表种子数据（2 行，验证行数一致性）
_RESULT_ROWS = [
    ("C001", "U001", 801200.0, 4.35, "1"),
    ("C002", "U002", 300000.0, 3.10, "1"),
]


@pytest.fixture(scope="module")
def client():
    """TestClient 上下文触发 lifespan（建表 + 种子用户）"""
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module", autouse=True)
def seed_env(client):
    """准备：演示数据集 + 结果表 + completed 任务（模块级一次）"""
    asyncio.run(demo_dataset.aensure_seeded())
    # 装载目标结果表（rpt_ 前缀允许写/删）
    demo_dataset.drop_table(TARGET_TABLE)
    demo_dataset.execute_script([
        f"""CREATE TABLE {TARGET_TABLE} (
            contract_no TEXT, cust_id TEXT, loan_balance REAL,
            execute_rate REAL, five_classify TEXT)""",
        *[
            f"INSERT INTO {TARGET_TABLE} VALUES ('{r[0]}', '{r[1]}', {r[2]}, {r[3]}, '{r[4]}')"
            for r in _RESULT_ROWS
        ],
    ])
    # 登记 completed 任务（report_config 指定目标表）
    asyncio.run(task_service.save_task_state({
        "task_id": TASK_ID,
        "tenant_id": "T001",
        "status": "completed",
        "report_config": {"target_table": TARGET_TABLE},
    }))
    # 非 completed 任务（验证 409）
    asyncio.run(task_service.save_task_state({
        "task_id": "TASK_EXPORT_RUNNING",
        "tenant_id": "T001",
        "status": "executing",
        "report_config": {"target_table": TARGET_TABLE},
    }))
    return True


def login(client, username="admin", password="Admin@1234"):
    r = client.post("/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ---------- TXT 生成 ----------

def test_export_txt_format(client):
    """TXT：竖线分隔 + 首行表头字段名 + 行数与结果表一致 + 审计落库"""
    headers = login(client)
    r = client.post(f"/v1/tenants/T001/tasks/{TASK_ID}/export-interface-file",
                    headers=headers, json={"format": "txt"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_name"] == f"{TARGET_TABLE}.txt"
    assert body["format"] == "txt"
    assert body["row_count"] == len(_RESULT_ROWS)

    # 预览：表头 + 数据行，竖线分隔
    lines = body["preview"]
    assert lines[0] == "contract_no|cust_id|loan_balance|execute_rate|five_classify"
    assert lines[1].startswith("C001|U001|")
    assert len(lines) == 1 + len(_RESULT_ROWS)

    # 落盘文件内容与行数一致
    path = os.path.join(interface_file_service.exports_dir(TASK_ID), body["file_name"])
    with open(path, encoding="utf-8") as f:
        file_lines = f.read().strip().splitlines()
    assert len(file_lines) == 1 + len(_RESULT_ROWS)

    # 审计 task.export
    logs = client.get("/v1/tenants/T001/audit-logs?action=task.export",
                      headers=headers).json()
    assert any(l["resource"] == TASK_ID and l["detail"]["format"] == "txt"
               for l in logs["logs"])


# ---------- XML 生成 ----------

def test_export_xml_format(client):
    """XML：简单元素嵌套 + 行数一致 + 元素名即字段名"""
    headers = login(client)
    r = client.post(f"/v1/tenants/T001/tasks/{TASK_ID}/export-interface-file",
                    headers=headers, json={"format": "xml"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_name"] == f"{TARGET_TABLE}.xml"
    assert body["row_count"] == len(_RESULT_ROWS)

    path = os.path.join(interface_file_service.exports_dir(TASK_ID), body["file_name"])
    with open(path, encoding="utf-8") as f:
        content = f.read()
    assert content.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert f'<report table="{TARGET_TABLE}">' in content
    assert content.count("<row>") == len(_RESULT_ROWS)
    assert "<contract_no>C001</contract_no>" in content
    assert "<loan_balance>801200.0</loan_balance>" in content

    # 非法格式 → 400
    r = client.post(f"/v1/tenants/T001/tasks/{TASK_ID}/export-interface-file",
                    headers=headers, json={"format": "csv"})
    assert r.status_code == 400


# ---------- 文件列表 ----------

def test_list_export_files(client):
    """GET exports：列出已生成的 txt+xml 两个文件"""
    headers = login(client)
    r = client.get(f"/v1/tenants/T001/tasks/{TASK_ID}/exports", headers=headers)
    assert r.status_code == 200
    names = {f["file_name"] for f in r.json()["files"]}
    assert names == {f"{TARGET_TABLE}.txt", f"{TARGET_TABLE}.xml"}
    assert all(f["size"] > 0 for f in r.json()["files"])


# ---------- 下载与白名单 ----------

def test_download_export_file(client):
    """下载：内容可读；路径穿越（../）被白名单拦截"""
    headers = login(client)
    r = client.get(f"/v1/tenants/T001/tasks/{TASK_ID}/exports/{TARGET_TABLE}.txt",
                   headers=headers)
    assert r.status_code == 200
    assert "contract_no|cust_id|" in r.text

    # 路径穿越：URL 编码 ../ 直达服务层 → 404（不暴露目录信息）
    r = client.get(f"/v1/tenants/T001/tasks/{TASK_ID}/exports/..%2F..%2Fsecret.txt",
                   headers=headers)
    assert r.status_code in (404, 405, 422), r.status_code

    # 文件名含非法字符（斜杠编码后的另一个变体）与不存在文件 → 404
    assert client.get(f"/v1/tenants/T001/tasks/{TASK_ID}/exports/nope.txt",
                      headers=headers).status_code == 404
    assert client.get(f"/v1/tenants/T001/tasks/{TASK_ID}/exports/..hidden",
                      headers=headers).status_code == 404


# ---------- 状态语义 ----------

def test_export_requires_completed(client):
    """任务非 completed → 409；任务不存在/非本租户 → 404；未认证 → 401"""
    headers = login(client)
    r = client.post("/v1/tenants/T001/tasks/TASK_EXPORT_RUNNING/export-interface-file",
                    headers=headers, json={"format": "txt"})
    assert r.status_code == 409
    assert "completed" in r.json()["detail"]

    # 列表/下载同样 409
    assert client.get("/v1/tenants/T001/tasks/TASK_EXPORT_RUNNING/exports",
                      headers=headers).status_code == 409

    # 不存在任务 → 404
    assert client.post("/v1/tenants/T001/tasks/NOPE/export-interface-file",
                       headers=headers, json={"format": "txt"}).status_code == 404

    # 未认证 → 401
    assert client.post(f"/v1/tenants/T001/tasks/{TASK_ID}/export-interface-file",
                       json={"format": "txt"}).status_code == 401
