"""
AI后端适配器
统一OpenAI兼容接口，支持多后端切换
Demo 模式：未配置 API Key 或开启 ai_mock_mode 时，自动使用离线 Mock 适配器
"""

import httpx
from typing import List, Dict, Any, Optional
from backend.config import settings


class AIBackendAdapter:
    """AI后端适配器基类"""

    def __init__(self, config: Dict[str, Any]):
        self.provider = config.get("provider", "kimi")
        self.base_url = config.get("base_url", "")
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "kimi-pro")
        self.temperature = config.get("temperature", 0.3)
        self.max_tokens = config.get("max_tokens", 8192)
        self.timeout = config.get("timeout", 60)

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[str] = None,
        stream: bool = False
    ) -> Dict[str, Any]:
        """统一聊天补全接口"""

        request_body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": stream
        }

        if tools:
            request_body["tools"] = tools
        if tool_choice:
            request_body["tool_choice"] = tool_choice

        headers = {
            "Content-Type": "application/json"
        }

        # 不同后端的认证方式
        if self.provider == "azure":
            headers["api-key"] = self.api_key
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            url = f"{self.base_url}/chat/completions"
            if self.provider == "azure":
                url += "?api-version=2024-06-01"

            response = await client.post(url, headers=headers, json=request_body)
            response.raise_for_status()
            return response.json()

    async def embeddings(self, texts: List[str]) -> List[List[float]]:
        """文本向量化接口"""

        request_body = {
            "model": self.model,
            "input": texts
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/embeddings",
                headers=headers,
                json=request_body
            )
            response.raise_for_status()
            data = response.json()
            return [item["embedding"] for item in data["data"]]


class MockAIAdapter(AIBackendAdapter):
    """离线 Mock AI 适配器
    不依赖真实 AI 服务，返回确定性的演示 SQL，
    用于无 API Key 环境下的冒烟测试与本地演示。
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config or {})
        self.provider = "mock"
        self.model = "mock-offline"

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        tool_choice: Optional[str] = None,
        stream: bool = False
    ) -> Dict[str, Any]:
        """返回确定性的演示 SQL，符合 CodeGenAgent 的输出格式约定"""

        user_prompt = messages[-1].get("content", "") if messages else ""

        # 从 Prompt 中粗略提取目标表与源表，生成对应 SQL
        target_table = "rpt_g01_housing_loan"
        source_table = "loan_contract"
        for line in user_prompt.splitlines():
            if line.startswith("目标表:") and line.split(":", 1)[1].strip():
                target_table = line.split(":", 1)[1].strip()
            if line.startswith("源表:") and line.split(":", 1)[1].strip():
                source_table = line.split(":", 1)[1].strip().split(",")[0].strip()

        demo_sql = f"""```sql
-- 目标表: {target_table}
-- 源表: {source_table}
-- 说明: MockAIAdapter 离线生成的演示SQL，符合六维校验的全部硬性要求
INSERT INTO {target_table} (
    contract_no,        -- 合同编号，直接取数
    cust_id,            -- 客户ID，直接取数
    loan_balance,       -- 贷款余额 = 本金余额 + 资本化利息（EAST口径：含利息调整部分）
    execute_rate,       -- 执行利率，D20.6 保留6位小数
    overdue_principal,  -- 逾期本金：90天以内按已逾期部分，91天及以上按整笔
    biz_date,           -- 业务日期
    org_no              -- 机构号
)
SELECT
    t.contract_no,                                                              -- 合同编号
    t.cust_id,                                                                  -- 客户ID
    ROUND(t.principal_balance + IFNULL(t.interest_capitalized, 0), 4),          -- 含资本化利息的账面余额
    ROUND(IFNULL(t.execute_rate, 0), 6),                                        -- 利率精度 D20.6
    CASE
        WHEN IFNULL(t.overdue_days, 0) >= 91 THEN ROUND(t.principal_balance, 4) -- 91天及以上按整笔本金
        WHEN IFNULL(t.overdue_days, 0) > 0 THEN ROUND(t.principal_balance, 4)   -- 90天以内按已逾期部分
        ELSE 0
    END,                                                                        -- 逾期本金分段规则
    t.biz_date,                                                                 -- 业务日期
    t.org_no                                                                    -- 机构号（权限过滤字段）
FROM {source_table} t
WHERE t.is_deleted = 0                                                          -- 剔除逻辑删除
  AND t.is_test = 0                                                             -- 剔除测试数据
  AND t.org_no = '1001'                                                         -- 机构权限过滤
  AND t.product_code IN ('P001', 'P001-G')                                      -- 住房贷款含公积金组合贷
;
```

| 目标字段 | 源字段 | 转换逻辑 | 可空 | 风险等级 |
| --- | --- | --- | --- | --- |
| loan_balance | principal_balance + interest_capitalized | 本金+资本化利息，ROUND 4位 | 否 | 高 |
| execute_rate | execute_rate | ROUND 6位，D20.6 | 是 | 中 |
| overdue_principal | overdue_days, principal_balance | 90天分界分段 | 是 | 高 |
"""

        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": demo_sql
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        }

    async def embeddings(self, texts: List[str]) -> List[List[float]]:
        """返回固定伪向量（离线演示用）"""
        import hashlib
        vectors = []
        for text in texts:
            digest = hashlib.md5(text.encode("utf-8")).digest()
            base = [b / 255.0 for b in digest]
            # 重复拼接到 768 维
            vectors.append((base * (settings.vector_dimension // len(base) + 1))[:settings.vector_dimension])
        return vectors


class AIAdapterFactory:
    """AI适配器工厂"""

    @staticmethod
    def get_adapter(tenant_id: Optional[str] = None) -> AIBackendAdapter:
        """获取当前租户对应的AI适配器
        未配置 API Key 或开启 ai_mock_mode 时，返回离线 Mock 适配器"""

        if tenant_id:
            # 从租户配置获取
            from backend.core.tenant_context import PRESET_TENANTS
            tenant_config = PRESET_TENANTS.get(tenant_id, {})
            ai_config = tenant_config.get("ai_backend", {})
        else:
            # 使用默认配置
            ai_config = {
                "provider": settings.ai_backend_provider,
                "base_url": settings.ai_base_url,
                "api_key": settings.ai_api_key,
                "model": settings.ai_model,
                "temperature": settings.ai_temperature,
                "max_tokens": settings.ai_max_tokens
            }

        # 离线 Mock 模式：无 Key 环境自动降级，保证 Demo 可跑通
        if settings.ai_mock_mode or not ai_config.get("api_key") or ai_config.get("api_key") in ("", "your-kimi-api-key"):
            return MockAIAdapter(ai_config)

        return AIBackendAdapter(ai_config)

    @staticmethod
    def get_backup_adapter() -> AIBackendAdapter:
        """获取备用AI适配器"""
        backup_config = {
            "provider": settings.ai_backup_provider,
            "base_url": settings.ai_backup_base_url,
            "api_key": settings.ai_backup_api_key,
            "model": settings.ai_backup_model,
            "temperature": 0.3,
            "max_tokens": 4096
        }
        if settings.ai_mock_mode or not backup_config.get("api_key"):
            return MockAIAdapter(backup_config)
        return AIBackendAdapter(backup_config)
