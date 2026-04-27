# 🧠 RAGFlow Feishu Enterprise Copilot

> 基于 RAGFlow 二次开发的企业知识库 Copilot。  
> 接入飞书作为统一办公入口，支持文档上传、权限隔离、多知识库切换、引用溯源、后台管理与自动化评测。

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12-blue">
  <img alt="Base" src="https://img.shields.io/badge/Base-RAGFlow-green">
  <img alt="Channel" src="https://img.shields.io/badge/Channel-Feishu-00A1FF">
  <img alt="RAG" src="https://img.shields.io/badge/RAG-Enterprise%20KB-purple">
  <img alt="License" src="https://img.shields.io/badge/License-Apache--2.0-lightgrey">
</p>

本项目不是从零实现 RAG 引擎，而是基于 [RAGFlow](https://github.com/infiniflow/ragflow) 的文档解析、知识库、检索和问答能力，面向企业飞书办公场景进行二次开发。

- 原 RAGFlow README 已归档到：[README_RAGFLOW_ORIGINAL.md](./README_RAGFLOW_ORIGINAL.md)
- 详细二次开发说明见：[README_FEISHU_COPILOT.md](./README_FEISHU_COPILOT.md)
- 原许可证与版权信息保持不变，见：[LICENSE](./LICENSE)

---

## ✨ Killer Features

- **飞书企业办公入口**：通过 Feishu WS Sidecar 接入飞书机器人消息，支持私聊 / 群聊中的企业知识库问答。
- **ACL v2 权限隔离**：支持 public / department / personal / allow / deny 规则，检索前先过滤可访问知识库。
- **多知识库切换**：支持 `/知识库`、`/当前知识库`、`/切换知识库`、`/使用全部知识库`。
- **文档上传闭环**：后台上传文档后自动解析、切 chunk、入索引，并可在飞书中问答命中新文档。
- **Citation v2 引用回答**：回答中使用 `[来源1]`，底部展示来源文档与命中片段。
- **Query Rewrite + Answer Constraint**：优化短问、追问和入口类问题，提升回答稳定性。
- **管理后台**：提供 Dashboard、知识库、文档、ACL、知识库路由、评测结果等页面。
- **自动化评测与验收**：提供 eval 脚本和 final acceptance 脚本，方便持续验证。

---

## 📢 更新重点

| 模块 | 增强内容 |
|---|---|
| Feishu WS Sidecar | 飞书长连接接入、消息解析、异步分发 |
| Worker Queue | 异步处理飞书消息，避免阻塞回调 |
| ACL v2 | public / department / personal / allow / deny 权限隔离 |
| KB Selection | 会话级知识库切换，与 ACL 结果取交集 |
| Query Runner | 串联 ACL、Rewrite、Retrieval、Constraint、Citation |
| Upload Workflow | 文档上传、解析、chunk 预览、飞书问答命中 |
| Citation Formatter | `[ID:x] -> [来源x]`，来源去重与 snippet 清洗 |
| Admin MVP | 企业知识库后台管理页 |
| Eval / Acceptance | 自动评测与最终验收脚本 |

---

## 🧩 技术架构

```text
飞书用户
  │
  ▼
Feishu WS Sidecar
  │
  ▼
Message Queue / Worker
  │
  ▼
FeishuMessageContext
  │
  ▼
ACL v2 权限过滤
  │
  ▼
KB Selection 多知识库选择
  │
  ▼
Query Rewrite
  │
  ▼
RAGFlow Retrieval
  │
  ▼
Answer Constraint
  │
  ▼
Citation Formatter
  │
  ▼
飞书引用回答

最终检索范围：

final_kb_ids = ACL 可访问知识库 ∩ 用户当前选择知识库
🌟 主要能力
1. 飞书问答入口

通过飞书长连接接收消息，经 sidecar 和 worker queue 异步处理，再进入 RAGFlow 问答链路。

2. 企业权限隔离

ACL v2 支持公共、部门、个人和显式 allow / deny 规则，避免用户越权检索。

3. 多知识库切换

飞书用户可以通过命令查看、切换和恢复知识库范围，且切换结果不会绕过权限系统。

4. 文档上传闭环

后台上传文档后，系统完成解析、切片、入索引，最终可在飞书中问答命中新内容。

5. 引用回答

Citation v2 将底层 chunk 引用转成用户可读的 [来源1]，并展示来源文档与命中片段。

6. 管理后台

后台入口：

http://127.0.0.1:9380/admin

包含 Dashboard、知识库管理、文档管理、ACL 调试、知识库路由、评测结果等页面。

🚀 快速开始
1. 启动 RAGFlow 后端
cd /home/tom/code/ragflow
source .venv/bin/activate

nohup env PYTHONPATH=/home/tom/code/ragflow \
  ./.venv/bin/python api/ragflow_server.py \
  > logs/ragflow_server.log 2>&1 &

检查：

curl -I http://127.0.0.1:9380/admin
2. 启动飞书 Sidecar

飞书环境变量建议放在 .env.feishu.local，不要提交真实密钥。

常见变量名：

FEISHU_APP_ID
FEISHU_APP_SECRET
FEISHU_APP_TOKEN
FEISHU_DEFAULT_DIALOG_ID

启动：

cd /home/tom/code/ragflow
source .venv/bin/activate

set -a
source .env.feishu.local
set +a

nohup env PYTHONPATH=/home/tom/code/ragflow \
  ./.venv/bin/python scripts/run_feishu_ws.py \
  > logs/feishu_sidecar_local.log 2>&1 &

查看日志：

tail -f logs/feishu_sidecar_local.log
📡 飞书演示命令

在飞书中依次发送：

/知识库
/当前知识库
/切换知识库 Feishu Debug KB
/当前知识库
Nanobot 支持哪些入口？
/使用全部知识库
/当前知识库

预期回答示例：

Nanobot 支持飞书、Discord、Telegram、WhatsApp 等入口 [来源1]

本回答基于以下资料生成：
1. nanobot_kb_test_doc.md：Nanobot 是一个企业级多渠道 AI Agent 平台，支持飞书、Discord、Telegram、WhatsApp 等入口（来源1）
🧪 自动化验收
cd /home/tom/code/ragflow
source .venv/bin/activate
bash scripts/final_feishu_rag_acceptance.sh

检查项包括：

/admin
/api/v1/feishu/ping
/api/v1/feishu/metrics
/api/v1/feishu/health
/api/v1/feishu/debug_acl
/api/admin/kbs
/api/admin/documents
/api/admin/kb-selections
scripts/eval_feishu_rag.py
📁 重点代码结构
api/integrations/
├── feishu_message_context.py      # 飞书消息上下文
├── feishu_message_handler.py      # 飞书文本处理与命令分流
├── feishu_ws_bridge.py            # 飞书 WS 事件桥接
├── feishu_task_queue.py           # 异步任务队列
├── feishu_kb_acl.py               # ACL v2 权限过滤
├── feishu_kb_selection.py         # 多知识库切换状态
├── feishu_query_runner.py         # 飞书 RAG 问答统一入口
├── query_rewrite.py               # Query Rewrite
├── feishu_answer_constraint.py    # 回答约束
├── feishu_citation_formatter.py   # Citation v2
└── feishu_metrics.py              # 运行指标

api/apps/
├── feishu_app.py                  # 飞书 API / debug / health / metrics
└── admin_mvp_app.py               # 后台管理 API

scripts/
├── run_feishu_ws.py
├── eval_feishu_rag.py
└── final_feishu_rag_acceptance.sh
🗺️ Roadmap
 飞书机器人问答接入
 Feishu WS Sidecar
 Worker Queue 异步处理
 ACL v2 权限隔离
 多知识库切换
 文档上传闭环
 Citation v2 引用回答
 Query Rewrite / Answer Constraint
 后台管理页
 自动化评测与验收脚本
 后台登录鉴权
 文档上传去重
 用户 / 部门同步
 Docker Compose 一键演示环境
📝 与原 RAGFlow 的关系

本项目基于 RAGFlow 进行企业办公场景二次开发，主要新增飞书入口、权限隔离、多知识库切换、引用回答、后台管理和评测闭环。

原项目说明保存在：

README_RAGFLOW_ORIGINAL.md

原许可证和版权信息保持不变：

LICENSE