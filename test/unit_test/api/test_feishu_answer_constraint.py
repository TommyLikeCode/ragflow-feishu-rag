from api.integrations.feishu_answer_constraint import apply_answer_constraint


def test_support_entry_question_returns_entry_list_only_and_stops_before_markdown_sections():
    question = "这个能力支持哪些入口？"
    answer = "支持飞书、Discord、Telegram、WhatsApp 入口。\n\n### 使用说明\n请参考文档。"
    reference = {
        "chunks": [
            {
                "content": "该能力当前支持飞书、Discord、Telegram、WhatsApp 入口。"
            }
        ]
    }

    constrained = apply_answer_constraint(question, answer, reference)

    assert constrained["rule"] == "support_entries_only"
    assert constrained["applied"] is True
    assert constrained["answer"] == "这个能力 支持飞书、Discord、Telegram、WhatsApp 等入口 [ID:0]"
    assert "###" not in constrained["answer"]


def test_non_support_question_keeps_existing_behavior_without_forcing_entry_list():
    question = "RAGFlow 是什么？"
    answer = ""
    reference = {
        "chunks": [
            {
                "content": "RAGFlow 是一个基于深度文档理解的 RAG 引擎。"
            }
        ]
    }

    constrained = apply_answer_constraint(question, answer, reference)

    assert constrained["rule"] in {"definition_template", "generic_fallback_definition"}
    assert "RAGFlow" in constrained["answer"]


def test_support_entry_question_regression_truncates_followup_markdown_sections():
    question = "Nanobot 支持哪些入口？"
    answer = "Sorry! No relevant content was found in the knowledge base!"
    chunk_text = (
        "# Nanobot 企业知识库测试文档 "
        "## 文档用途 这是一份用于测试 RAGFlow 企业知识库 Copilot 上传、解析、检索、引用和权限隔离的测试文档。 "
        "## Nanobot 平台简介 Nanobot 是一个企业级多渠道 AI Agent 平台，支持飞书、Discord、Telegram、WhatsApp 等入口。 "
        "## 核心能力 Nanobot 的核心能力包括： 1. 多渠道消息接入 2. 大模型统一路由 3. 工具调用 4. RAG 知识库问答 5. 异步任务队列 6. 企业权限隔离 "
        "## 适用场景 Nanobot 可以用于企业内部知识问答、工单处理、自动化运维、会议助手、流程审批提醒等场景。"
    )
    reference = {"chunks": [{"content": chunk_text}]}

    constrained = apply_answer_constraint(question, answer, reference)

    assert constrained["rule"] == "support_entries_only"
    assert constrained["applied"] is True
    assert "Nanobot 支持飞书、Discord、Telegram、WhatsApp 等入口 [ID:0]" in constrained["answer"]

    forbidden_tokens = [
        "核心能力",
        "适用场景",
        "多渠道消息接入",
        "工单处理",
    ]
    for token in forbidden_tokens:
        assert token not in constrained["answer"]
