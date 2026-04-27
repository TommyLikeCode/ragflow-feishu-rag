# RAGFlow Feishu Enterprise Copilot 二次开发版说明

本文档说明本仓库在 RAGFlow 基础上新增或强化的飞书企业 Copilot 相关能力。

## 二次开发能力清单

1. 飞书机器人问答
- 提供飞书消息事件接入与问答回复链路。

2. Feishu WS sidecar + worker queue
- 通过 WS sidecar 接收事件，结合 worker queue 实现异步处理。

3. ACL v2 权限隔离
- 按用户身份与策略进行知识库访问控制。

4. 多知识库切换
- 支持以下指令：
- /知识库
- /当前知识库
- /切换知识库
- /使用全部知识库

5. 文档上传闭环
- 支持上传、解析、入库与检索链路。

6. Citation v2 引用回答
- 支持引用来源格式化与可读化展示。

7. Query Rewrite / Answer Constraint
- 支持查询改写与回答约束，提升稳定性与一致性。

8. 后台管理页 /admin
- 提供管理与诊断页面能力。

9. 自动化评测 eval
- 支持评测脚本执行与指标输出。

10. metrics / health
- 提供运行指标与健康检查接口。

## 验收脚本

可使用以下脚本进行最终验收：

- scripts/final_feishu_rag_acceptance.sh

该脚本将检查关键接口可用性，并执行 eval 指标阈值验证。
