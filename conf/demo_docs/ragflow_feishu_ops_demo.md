# RAGFlow 飞书 Copilot 运维手册

本示例文档用于技术运维知识库演示，方便排查飞书 Copilot 相关启动、日志与验收问题。

## 后端启动

RAGFlow 后端可通过项目中的 Python 启动命令运行，通常需要先激活虚拟环境，再启动 `api/ragflow_server.py`。

## Feishu sidecar 启动

飞书 sidecar 可通过 `scripts/run_feishu_ws.py` 启动，并以独立日志记录消息接入情况。

## 日志查看

可查看 `logs/feishu_sidecar_local.log` 来排查飞书长连接、消息接入与转发问题。

## 常用健康检查

- `/api/v1/feishu/metrics`：查看消息处理、成功率与指标。
- `/api/v1/feishu/health`：查看 WS、worker 与队列健康状态。

## 最终验收

可运行 `scripts/final_feishu_rag_acceptance.sh` 完成只读验收。

## 建议提问

- RAGFlow 后端怎么启动？
- 飞书 sidecar 日志怎么看？
- 最终验收脚本怎么运行？
- metrics 接口有什么作用？
