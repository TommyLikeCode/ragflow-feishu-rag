import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def feishu_module():
    pytest.importorskip("quart")
    try:
        return importlib.import_module("api.apps.feishu")
    except ModuleNotFoundError as exc:
        pytest.skip(f"feishu app test dependencies are unavailable: {exc}")


@pytest.mark.asyncio
async def test_bind_dialog_kbs_appends_and_dedupes(monkeypatch, feishu_module):
    dialog = SimpleNamespace(id="dlg-1", tenant_id="tenant-1", kb_ids=["kb-old", "kb-shared"])
    updates = {}

    monkeypatch.setattr(feishu_module.DialogService, "get_by_id", lambda dialog_id: (True, dialog))
    monkeypatch.setattr(
        feishu_module.DialogService,
        "update_by_id",
        lambda dialog_id, data: updates.update({"dialog_id": dialog_id, "data": data}) or 1,
    )

    data, err, code = feishu_module._bind_dialog_kbs("dlg-1", ["kb-new", "kb-shared"])

    assert err == ""
    assert code == 0
    assert data["before_kb_ids"] == ["kb-old", "kb-shared"]
    assert data["after_kb_ids"] == ["kb-old", "kb-shared", "kb-new"]
    assert updates == {
        "dialog_id": "dlg-1",
        "data": {"kb_ids": ["kb-old", "kb-shared", "kb-new"]},
    }


def test_bind_dialog_kbs_rejects_empty_kb_ids(feishu_module):
    data, err, code = feishu_module._bind_dialog_kbs("dlg-1", [])

    assert data is None
    assert err == "`kb_ids` must be a non-empty list"
    assert code == 400


@pytest.mark.asyncio
async def test_bootstrap_kb_create_returns_error_when_dialog_missing(monkeypatch, feishu_module):
    monkeypatch.setattr(feishu_module.DialogService, "get_by_id", lambda dialog_id: (False, None))

    data, err, code = await feishu_module._bootstrap_kb_create("dlg-missing", "Feishu Debug KB")

    assert data is None
    assert err == "Dialog not found: dlg-missing"
    assert code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tenant_ok", "embd_id", "expected_message", "expected_code"),
    [
        (False, None, "Tenant not found: tenant-1", 404),
        (True, "", "No embedding model configured for tenant: tenant-1", 400),
    ],
)
async def test_bootstrap_kb_create_returns_error_when_tenant_or_embedding_missing(
    monkeypatch,
    feishu_module,
    tenant_ok,
    embd_id,
    expected_message,
    expected_code,
):
    dialog = SimpleNamespace(id="dlg-1", tenant_id="tenant-1", kb_ids=[])
    tenant = SimpleNamespace(id="tenant-1", embd_id=embd_id)

    monkeypatch.setattr(feishu_module.DialogService, "get_by_id", lambda dialog_id: (True, dialog))
    monkeypatch.setattr(feishu_module.TenantService, "get_by_id", lambda tenant_id: (tenant_ok, tenant if tenant_ok else None))

    data, err, code = await feishu_module._bootstrap_kb_create("dlg-1", "Feishu Debug KB")

    assert data is None
    assert err == expected_message
    assert code == expected_code


@pytest.mark.asyncio
async def test_bootstrap_kb_create_saves_kb_and_binds_dialog(monkeypatch, feishu_module):
    dialog = SimpleNamespace(id="dlg-1", tenant_id="tenant-1", kb_ids=["kb-existing"])
    tenant = SimpleNamespace(id="tenant-1", embd_id="emb-1")
    saved_payload = {}
    bind_calls = {}

    monkeypatch.setattr(feishu_module.DialogService, "get_by_id", lambda dialog_id: (True, dialog))
    monkeypatch.setattr(feishu_module.TenantService, "get_by_id", lambda tenant_id: (True, tenant))
    monkeypatch.setattr(
        feishu_module.KnowledgebaseService,
        "create_with_name",
        lambda **kwargs: (True, {"id": "kb-new", "name": kwargs["name"]}),
    )
    monkeypatch.setattr(
        feishu_module.KnowledgebaseService,
        "save",
        lambda **kwargs: saved_payload.update(kwargs) or True,
    )

    def fake_bind(dialog_id, kb_ids):
        bind_calls.update({"dialog_id": dialog_id, "kb_ids": kb_ids})
        return (
            {
                "dialog_id": dialog_id,
                "tenant_id": "tenant-1",
                "before_kb_ids": ["kb-existing"],
                "after_kb_ids": ["kb-existing", "kb-new"],
            },
            "",
            0,
        )

    monkeypatch.setattr(feishu_module, "_bind_dialog_kbs", fake_bind)

    data, err, code = await feishu_module._bootstrap_kb_create("dlg-1", "Feishu Debug KB")

    assert err == ""
    assert code == 0
    assert data == {
        "dialog_id": "dlg-1",
        "tenant_id": "tenant-1",
        "knowledgebase_id": "kb-new",
        "knowledgebase_name": "Feishu Debug KB",
        "kb_ids": ["kb-existing", "kb-new"],
    }
    assert saved_payload["id"] == "kb-new"
    assert saved_payload["name"] == "Feishu Debug KB"
    assert saved_payload["embd_id"] == "emb-1"
    assert bind_calls == {"dialog_id": "dlg-1", "kb_ids": ["kb-new"]}


@pytest.mark.asyncio
async def test_bootstrap_kb_text_requires_bound_kbs(monkeypatch, feishu_module):
    dialog = SimpleNamespace(id="dlg-1", tenant_id="tenant-1", kb_ids=[])

    monkeypatch.setattr(feishu_module.DialogService, "get_by_id", lambda dialog_id: (True, dialog))

    data, err, code = await feishu_module._bootstrap_kb_text("dlg-1", "hello", "test.txt")

    assert data is None
    assert err == "Dialog has no bound knowledgebase. Please call bootstrap-kb or bootstrap-kb-create first."
    assert code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "filename", "expected_message"),
    [
        (None, "test.txt", "`text` is invalid"),
        ("   ", "test.txt", "`text` is required"),
        ("hello", None, "`filename` is invalid"),
        ("hello", "   ", "`filename` is required"),
    ],
)
async def test_bootstrap_kb_text_validates_text_and_filename(feishu_module, text, filename, expected_message):
    data, err, code = await feishu_module._bootstrap_kb_text("dlg-1", text, filename)

    assert data is None
    assert err == expected_message
    assert code == 400


@pytest.mark.asyncio
async def test_bootstrap_kb_text_parse_failure_still_cleans_conversation(monkeypatch, feishu_module):
    dialog = SimpleNamespace(id="dlg-1", tenant_id="tenant-1", kb_ids=["kb-1"])
    saved_conversation_ids = []
    deleted_conversation_ids = []

    monkeypatch.setattr(feishu_module.DialogService, "get_by_id", lambda dialog_id: (True, dialog))
    monkeypatch.setattr(
        feishu_module.API4ConversationService,
        "save",
        lambda **kwargs: saved_conversation_ids.append(kwargs["id"]) or True,
    )
    monkeypatch.setattr(
        feishu_module.API4ConversationService,
        "delete_by_id",
        lambda conversation_id: deleted_conversation_ids.append(conversation_id) or 1,
    )

    async def fake_thread_pool_exec(func, *args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(feishu_module, "thread_pool_exec", fake_thread_pool_exec)

    data, err, code = await feishu_module._bootstrap_kb_text("dlg-1", "hello world", "test.txt")

    assert data is None
    assert err == "Failed to upload or parse bootstrap text: boom"
    assert code == 500
    assert saved_conversation_ids == deleted_conversation_ids
    assert len(saved_conversation_ids) == 1


@pytest.mark.asyncio
async def test_bootstrap_kb_text_returns_doc_ids_on_success(monkeypatch, feishu_module):
    dialog = SimpleNamespace(id="dlg-1", tenant_id="tenant-1", kb_ids=["kb-1"])
    deleted_conversation_ids = []

    monkeypatch.setattr(feishu_module.DialogService, "get_by_id", lambda dialog_id: (True, dialog))
    monkeypatch.setattr(feishu_module.API4ConversationService, "save", lambda **kwargs: True)
    monkeypatch.setattr(
        feishu_module.API4ConversationService,
        "delete_by_id",
        lambda conversation_id: deleted_conversation_ids.append(conversation_id) or 1,
    )

    async def fake_thread_pool_exec(func, *args, **kwargs):
        return ["doc-1", "doc-2"]

    monkeypatch.setattr(feishu_module, "thread_pool_exec", fake_thread_pool_exec)

    data, err, code = await feishu_module._bootstrap_kb_text("dlg-1", "hello world", "test.txt")

    assert err == ""
    assert code == 0
    assert data["dialog_id"] == "dlg-1"
    assert data["filename"] == "test.txt"
    assert data["kb_ids"] == ["kb-1"]
    assert data["doc_ids"] == ["doc-1", "doc-2"]
    assert data["status"] == "success"
    assert "conversation_id" not in data
    assert len(deleted_conversation_ids) == 1


@pytest.mark.asyncio
async def test_bootstrap_kb_create_route_returns_success_json_in_allowed_env(monkeypatch, feishu_module):
    monkeypatch.setenv("FEISHU_BOOTSTRAP_ENV", "dev")

    async def fake_helper(dialog_id, name):
        return {
            "dialog_id": dialog_id,
            "tenant_id": "tenant-1",
            "knowledgebase_id": "kb-1",
            "knowledgebase_name": name,
            "kb_ids": ["kb-1"],
        }, "", 0

    monkeypatch.setattr(feishu_module, "_bootstrap_kb_create", fake_helper)

    client = feishu_module.app.test_client()
    response = await client.post(
        "/api/v1/feishu/bootstrap-kb-create",
        json={"dialog_id": "dlg-1", "name": "Feishu Debug KB"},
    )

    assert response.status_code == 200
    payload = await response.get_json()
    assert payload == {
        "code": 0,
        "message": "success",
        "data": {
            "dialog_id": "dlg-1",
            "tenant_id": "tenant-1",
            "knowledgebase_id": "kb-1",
            "knowledgebase_name": "Feishu Debug KB",
            "kb_ids": ["kb-1"],
        },
    }


@pytest.mark.asyncio
async def test_bootstrap_route_returns_403_json_in_blocked_env(monkeypatch, feishu_module):
    for key in (
        "FEISHU_BOOTSTRAP_ENV",
        "RAGFLOW_ENV",
        "APP_ENV",
        "ENV",
        "FEISHU_BOOTSTRAP_ENABLED",
        "RAGFLOW_BOOTSTRAP_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)

    helper_called = {"called": False}

    async def fake_helper(dialog_id, name):
        helper_called["called"] = True
        return {}, "", 0

    monkeypatch.setattr(feishu_module, "_bootstrap_kb_create", fake_helper)

    client = feishu_module.app.test_client()
    response = await client.post(
        "/v1/feishu/bootstrap-kb-create",
        json={"dialog_id": "dlg-1", "name": "Feishu Debug KB"},
    )

    assert response.status_code == 200
    payload = await response.get_json()
    assert payload["code"] == 403
    assert "debug-only/bootstrap-only" in payload["message"]
    assert payload["data"] is None
    assert helper_called["called"] is False


@pytest.mark.asyncio
async def test_bootstrap_kb_text_route_wraps_helper_failure_json(monkeypatch, feishu_module):
    monkeypatch.setenv("FEISHU_BOOTSTRAP_ENV", "test")

    async def fake_helper(dialog_id, text, filename):
        return None, "helper failed", 500

    monkeypatch.setattr(feishu_module, "_bootstrap_kb_text", fake_helper)

    client = feishu_module.app.test_client()
    response = await client.post(
        "/api/v1/feishu/bootstrap-kb-text",
        json={"dialog_id": "dlg-1", "text": "hello", "filename": "test.txt"},
    )

    assert response.status_code == 200
    payload = await response.get_json()
    assert payload == {
        "code": 500,
        "message": "helper failed",
        "data": None,
    }
