# RAGFlow Feishu Enterprise Copilot 详细技术说明

本文档用于说明本仓库在 RAGFlow 基础上的企业知识库 Copilot 二次开发内容。  
项目首页见：[README.md](./README.md)。  
原 RAGFlow 说明见：[README_RAGFLOW_ORIGINAL.md](./README_RAGFLOW_ORIGINAL.md)。

---

## 1. 项目定位

本项目基于 RAGFlow 的文档解析、知识库、检索和问答能力，面向企业飞书办公场景进行二次开发。

目标是实现一套可演示、可验证、具备企业工程特征的知识库 Copilot：

```text
企业员工在飞书中提问
  -> 系统根据用户身份过滤可访问知识库
  -> 支持用户切换当前知识库范围
  -> RAGFlow 检索企业文档
  -> 返回带来源引用的回答
  -> 后台可上传文档、调试权限、查看评测和运行状态
2. 总体架构
┌─────────────────────────────────────────────────────────────────────┐
│                         飞书用户 / 企业员工                          │
│                  私聊 / 群聊 / 文本命令 / 知识库问答                  │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Feishu WS Sidecar                            │
│  负责接入飞书长连接事件，接收 message event，并统一转为内部消息上下文    │
│  files: scripts/run_feishu_ws.py, feishu_ws_bridge.py               |
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      Message Queue / Worker                         │
│  异步排队处理飞书消息，避免回调阻塞，支持日志、metrics、失败计数         │
│  files: feishu_task_queue.py, feishu_message_handler.py             │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       FeishuMessageContext                          │
│  统一抽取 message_id / chat_id / open_id / text / file_key 等上下文   │
│  file: feishu_message_context.py                                    │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         ACL v2 权限过滤                              │
│  public / department / personal / allowed_user_ids / denied_user_ids│
│  先过滤用户可访问的知识库，避免越权检索                                 │
│  file: feishu_kb_acl.py                                             │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      KB Selection 多知识库选择                       │
│  支持 /知识库、/当前知识库、/切换知识库、/使用全部知识库                │
│  final_kb_ids = ACL 可访问知识库 ∩ 用户当前选择知识库                  │
│  file: feishu_kb_selection.py                                       │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          Query Rewrite                              │
│  对短问、追问、指代问题进行规则化改写，提高检索命中率                    │
│  file: query_rewrite.py                                             │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         RAGFlow Retrieval                           │
│  复用 RAGFlow 的文档解析、chunk、embedding、ES / 向量检索与问答能力    │
│  final_kb_ids 作为检索范围传入，保证只查授权知识库                     │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       Answer Constraint                             │
│  对模型答案进行约束：兜底修正、入口类问题收敛、避免 Markdown 噪声        │
│  file: feishu_answer_constraint.py                                  │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       Citation Formatter                            │
│  将 [ID:0] 转为 [来源1]，展示来源文档和命中片段，支持去重与 snippet 清洗 │
│  file: feishu_citation_formatter.py                                 │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         飞书引用回答                                 │
└─────────────────────────────────────────────────────────────────────┘
3. 飞书接入链路

飞书消息通过 WS sidecar 接入。

关键模块：

scripts/run_feishu_ws.py
api/integrations/feishu_ws_bridge.py
api/integrations/feishu_task_queue.py
api/integrations/feishu_message_handler.py
api/integrations/feishu_ws_client.py

处理流程：

飞书事件
  -> ws_bridge 解析事件
  -> 构造 FeishuMessageContext
  -> 写入本地异步队列
  -> worker 消费消息
  -> 命令分流或 RAG 问答
  -> 通过 Feishu client 回复
4. ACL v2 权限系统

ACL v2 用于在检索前过滤知识库，避免越权访问。

配置文件：

conf/feishu_kb_acl.json

核心字段：

{
  "scope": "public",
  "owner_user_id": "",
  "department_id": "",
  "allowed_user_ids": [],
  "denied_user_ids": []
}

支持规则：

scope	含义
public	公共知识库
department	部门知识库
personal	个人知识库
no_policy	未配置策略，默认拒绝

优先级：

explicit deny > explicit allow > public > department match > personal owner > no_policy deny

调试接口：

curl -sS "http://127.0.0.1:9380/api/v1/feishu/debug_acl?dialog_id=61f317d03cca11f1a2315f1d7123b9fc&open_id=ou_test_citation" | python3 -m json.tool
5. 多知识库切换

用户可以在飞书中切换当前问答范围。

命令：

/知识库
/当前知识库
/切换知识库 Feishu Debug KB
/使用全部知识库

状态文件：

logs/feishu_kb_selection.json

selection key 规则：

chat_id:<chat_id>:open_id:<open_id>

最终检索范围：

final_kb_ids = acl_filtered_kb_ids ∩ selected_kb_ids

如果用户未选择知识库，则使用 ACL 允许的全部知识库。

6. 文档上传闭环

后台文档页支持上传文件到指定知识库。

闭环流程：

上传文档
  -> 创建 Document
  -> 解析文档
  -> 切分 chunk
  -> 写入索引
  -> 文档列表可见
  -> chunk 可预览
  -> 飞书问答可命中

后台 API：

POST /api/admin/documents/upload
GET  /api/admin/documents
GET  /api/admin/documents/:id
7. Citation v2 引用回答

Citation v2 负责将底层 [ID:x] 引用转换成飞书用户可读格式。

输入：

Nanobot 支持飞书、Discord、Telegram、WhatsApp 等入口 [ID:0]

输出：

Nanobot 支持飞书、Discord、Telegram、WhatsApp 等入口 [来源1]

本回答基于以下资料生成：
1. nanobot_kb_test_doc.md：Nanobot 是一个企业级多渠道 AI Agent 平台，支持飞书、Discord、Telegram、WhatsApp 等入口（来源1）

增强点：

来源编号转换
文档名兜底
snippet 清洗
Markdown 标题去除
answer-aware 片段选择
重复来源去重
8. Query Rewrite 与 Answer Constraint

Query Rewrite 用于短问补全和指代改写。

Answer Constraint 用于控制最终回答质量：

有检索结果但模型 fallback 时，用来源片段生成答案
对入口类问题只输出入口列表
避免带出 Markdown 后续章节
保持引用标记

示例：

用户：Nanobot 支持哪些入口？
回答：Nanobot 支持飞书、Discord、Telegram、WhatsApp 等入口 [来源1]
9. 管理后台

后台入口：

http://127.0.0.1:9380/admin

页面：

页面	功能
Dashboard	系统状态、问答统计、健康度
知识库管理	KB 列表、scope、权限摘要
文档管理	上传文档、解析状态、chunk 预览
ACL 调试	查看用户对 dialog 的 KB 访问决策
知识库路由	查看和清除 KB selection
评测结果	查看 eval 输出与引用指标
10. 自动化评测

评测脚本：

python scripts/eval_feishu_rag.py \
  --dialog-id 61f317d03cca11f1a2315f1d7123b9fc \
  --eval-set conf/feishu_eval_set.json \
  --output logs/final_feishu_eval_result.json

指标：

指标	含义
hit_rate	综合命中率
answer_keyword_match_rate	答案关键词匹配率
source_keyword_match_rate	来源关键词匹配率
citation_coverage_rate	引用覆盖率
avg_latency_seconds	平均耗时
avg_citation_count	平均引用数
11. 最终验收
cd /home/tom/code/ragflow
source .venv/bin/activate
bash scripts/final_feishu_rag_acceptance.sh

验收脚本会检查：

/admin
/api/v1/feishu/ping
/api/v1/feishu/metrics
/api/v1/feishu/health
/api/v1/feishu/debug_acl
/api/admin/kbs
/api/admin/documents
/api/admin/kb-selections
scripts/eval_feishu_rag.py