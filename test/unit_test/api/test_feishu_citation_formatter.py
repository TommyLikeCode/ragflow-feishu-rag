from api.integrations.feishu_citation_formatter import format_answer_with_citations


def test_citation_snippet_prefers_sentence_with_channel_keywords_and_skips_markdown_titles():
    answer = "Nanobot 支持飞书、Discord、Telegram、WhatsApp 等入口 [ID:0]"
    chunk_text = (
        "# Nanobot 企业知识库测试文档 "
        "## 文档用途 这是一份用于测试的文档。 "
        "## Nanobot 平台简介 Nanobot 是一个企业级多渠道 AI Agent 平台，支持飞书、Discord、Telegram、WhatsApp 等入口。 "
        "## 核心能力 Nanobot 的核心能力包括：多渠道消息接入。"
    )
    references = [{"content": chunk_text, "document_name": "Nanobot 文档"}]

    formatted = format_answer_with_citations(answer, references, max_snippet_len=200)
    normalized_sources = formatted.get("normalized_sources", [])

    assert normalized_sources
    snippet = normalized_sources[0].get("snippet", "")
    assert "支持飞书、Discord、Telegram、WhatsApp" in snippet
    assert "Nanobot 是一个企业级多渠道 AI Agent 平台" in snippet
    assert not snippet.startswith("# Nanobot 企业知识库测试文档")
    assert "## 文档用途" not in snippet
    assert "平台简介" not in snippet


def test_citation_snippet_avoids_test_doc_intro_noise_when_channel_sentence_exists():
    answer = "Nanobot 支持飞书、Discord、Telegram、WhatsApp 等入口 [ID:0]"
    chunk_text = (
        "这是一份用于测试 RAGFlow 企业知识库 Copilot 上传、解析、检索、引用和权限隔离的测试文档。 "
        "## Nanobot 平台简介 Nanobot 是一个企业级多渠道 AI Agent 平台，支持飞书、Discord、Telegram、WhatsApp 等入口。 "
        "## 核心能力 Nanobot 的核心能力包括：多渠道消息接入。"
    )
    references = [{"content": chunk_text, "document_name": "nanobot_kb_test_doc.md"}]

    formatted = format_answer_with_citations(answer, references, max_snippet_len=220)
    normalized_sources = formatted.get("normalized_sources", [])

    assert normalized_sources
    snippet = normalized_sources[0].get("snippet", "")
    assert "支持飞书、Discord、Telegram、WhatsApp" in snippet
    assert "这是一份用于测试" not in snippet
    assert "## Nanobot 平台简介" not in snippet
    assert "## 文档用途" not in snippet
