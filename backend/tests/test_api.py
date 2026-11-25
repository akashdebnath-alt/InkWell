import json
from datetime import datetime, timedelta
from io import BytesIO
from types import SimpleNamespace

from docx import Document as WordDocument

from app import main, rag
from app.config import Settings
from app.db import DocumentVersion, SessionLocal
from app.exports import create_docx, create_pdf
from conftest import auth_headers, register


def test_register_login_and_document_crud(client):
    token = register(client)
    headers = auth_headers(token)

    login = client.post(
        "/auth/login",
        json={"email": "WRITER@example.com", "password": "secure-password"},
    )
    assert login.status_code == 200
    assert login.json()["access_token"]

    created = client.post(
        "/documents",
        headers=headers,
        json={"title": "First draft", "content": "Opening line"},
    )
    assert created.status_code == 201
    document_id = created.json()["id"]
    assert client.get(f"/documents/{document_id}", headers=headers).json()["content"] == "Opening line"

    updated = client.put(
        f"/documents/{document_id}",
        headers=headers,
        json={"title": "Revised draft", "content": "A changed line"},
    )
    assert updated.status_code == 200
    assert updated.json()["content"] == "A changed line"

    assert client.delete(f"/documents/{document_id}", headers=headers).status_code == 204
    assert client.get(f"/documents/{document_id}", headers=headers).status_code == 404


def test_user_cannot_read_another_users_document(client):
    owner_headers = auth_headers(register(client))
    other_headers = auth_headers(register(client, "other@example.com"))
    created = client.post(
        "/documents",
        headers=owner_headers,
        json={"title": "Private", "content": "Private text"},
    )

    document_id = created.json()["id"]
    assert client.get(
        f"/documents/{document_id}",
        headers=other_headers,
    ).status_code == 404
    assert client.get(
        f"/documents/{document_id}/export?format=docx",
        headers=other_headers,
    ).status_code == 404


def test_document_versions_are_snapshotted_after_five_minutes(client, monkeypatch):
    headers = auth_headers(register(client))
    document_id = client.post(
        "/documents",
        headers=headers,
        json={"title": "Timed", "content": "first"},
    ).json()["id"]
    current_time = [datetime(2026, 1, 1, 12, 0, 0)]
    monkeypatch.setattr(main, "_utcnow", lambda: current_time[0])

    client.put(
        f"/documents/{document_id}",
        headers=headers,
        json={"content": "second"},
    )
    versions_at_start = client.get(
        f"/documents/{document_id}/versions",
        headers=headers,
    ).json()
    assert len(versions_at_start) == 1

    current_time[0] += timedelta(minutes=4, seconds=59)
    client.put(
        f"/documents/{document_id}",
        headers=headers,
        json={"content": "third"},
    )
    assert len(
        client.get(f"/documents/{document_id}/versions", headers=headers).json()
    ) == 1

    current_time[0] += timedelta(seconds=1)
    client.put(
        f"/documents/{document_id}",
        headers=headers,
        json={"content": "fourth"},
    )
    versions = client.get(
        f"/documents/{document_id}/versions",
        headers=headers,
    ).json()
    assert len(versions) == 2
    with SessionLocal() as db:
        latest = db.get(DocumentVersion, versions[0]["id"])
        assert latest.content == "third"


def test_restoring_version_preserves_current_content(client, monkeypatch):
    headers = auth_headers(register(client))
    document_id = client.post(
        "/documents",
        headers=headers,
        json={"title": "Restore", "content": "before"},
    ).json()["id"]
    initial_time = datetime(2026, 2, 1, 12, 0, 0)
    monkeypatch.setattr(main, "_utcnow", lambda: initial_time)
    client.put(
        f"/documents/{document_id}",
        headers=headers,
        json={"content": "after"},
    )
    version_id = client.get(
        f"/documents/{document_id}/versions",
        headers=headers,
    ).json()[0]["id"]
    monkeypatch.setattr(
        rag,
        "index_document",
        lambda *_args, **_kwargs: 0,
    )

    restored = client.post(
        f"/documents/{document_id}/restore/{version_id}",
        headers=headers,
    )
    assert restored.status_code == 200
    assert restored.json()["content"] == "before"
    versions = client.get(
        f"/documents/{document_id}/versions",
        headers=headers,
    ).json()
    assert len(versions) == 2
    with SessionLocal() as db:
        current_snapshot = db.get(DocumentVersion, versions[0]["id"])
        assert current_snapshot.content == "after"


def test_docx_and_pdf_exports_keep_formatting_and_safe_filename(client):
    headers = auth_headers(register(client))
    content = json.dumps(
        {
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 1, "textAlign": "center"},
                    "content": [
                        {
                            "type": "text",
                            "text": "Title",
                            "marks": [{"type": "bold"}],
                        }
                    ],
                },
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Body"}],
                },
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Item"}],
                                }
                            ],
                        }
                    ],
                },
            ],
        }
    )
    docx = create_docx(content)
    word = WordDocument(BytesIO(docx))
    assert word.paragraphs[0].text == "Title"
    assert word.paragraphs[0].alignment == 1
    assert word.paragraphs[0].runs[0].bold
    assert any(p.style.name == "List Bullet" for p in word.paragraphs)
    assert create_pdf(content).startswith(b"%PDF-")

    document_id = client.post(
        "/documents",
        headers=headers,
        json={"title": "Draft/Name?", "content": content},
    ).json()["id"]
    word_response = client.get(
        f"/documents/{document_id}/export?format=docx",
        headers=headers,
    )
    pdf_response = client.get(
        f"/documents/{document_id}/export?format=pdf",
        headers=headers,
    )
    assert word_response.status_code == 200
    assert word_response.content.startswith(b"PK")
    assert 'filename="Draft_Name_.docx"' in word_response.headers["content-disposition"]
    assert pdf_response.status_code == 200
    assert pdf_response.content.startswith(b"%PDF-")
    assert client.get(
        f"/documents/{document_id}/export?format=exe",
        headers=headers,
    ).status_code == 400


def test_suggest_returns_503_without_gemini_key(client):
    headers = auth_headers(register(client))
    document_id = client.post(
        "/documents",
        headers=headers,
        json={"title": "Writing", "content": ""},
    ).json()["id"]

    response = client.post(
        "/suggest",
        headers=headers,
        json={"doc_id": document_id, "text_before": "A sufficiently long opening"},
    )
    assert response.status_code == 503
    assert "Configure GEMINI_API_KEY" in response.json()["detail"]


def test_auth_endpoints_limit_repeated_attempts(client):
    payload = {"email": "unknown@example.com", "password": "incorrect"}
    for _ in range(10):
        assert client.post("/auth/login", json=payload).status_code == 401
    limited = client.post("/auth/login", json=payload)

    assert limited.status_code == 429
    assert limited.headers["retry-after"]


def test_cors_reads_configured_origins_and_defaults_to_vite(client):
    settings = Settings(
        database_url="sqlite://",
        cors_origins="http://localhost:5173, https://writing.example",
    )
    assert settings.allowed_origins == [
        "http://localhost:5173",
        "https://writing.example",
    ]
    assert Settings(database_url="sqlite://", cors_origins="").allowed_origins == [
        "http://localhost:5173"
    ]
    allowed = client.options(
        "/auth/login",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    denied = client.options(
        "/auth/login",
        headers={
            "Origin": "https://untrusted.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-origin" not in denied.headers


def test_suggest_uses_the_selected_writing_form(client, monkeypatch):
    headers = auth_headers(register(client))
    document_id = client.post(
        "/documents",
        headers=headers,
        json={"title": "Poem", "content": ""},
    ).json()["id"]
    monkeypatch.setattr(main, "_require_ai_key", lambda: None)
    monkeypatch.setattr(rag, "retrieve", lambda *_args, **_kwargs: [])
    from app import llm

    received = {}

    def fake_ghost(text_before, context, writing_type="general"):
        received["writing_type"] = writing_type
        return "over quiet water"

    monkeypatch.setattr(llm, "ghost", fake_ghost)
    response = client.post(
        "/suggest",
        headers=headers,
        json={
            "doc_id": document_id,
            "text_before": "The moon is rising",
            "writing_type": "poem",
        },
    )

    assert response.status_code == 200
    assert received["writing_type"] == "poem"
    suggestion_chunks = [
        json.loads(line[len("data: ") :])["text"]
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]
    assert "".join(suggestion_chunks) == "over quiet water"


def test_story_ideas_can_use_a_prompt_without_selected_text(client, monkeypatch):
    headers = auth_headers(register(client))
    document_id = client.post(
        "/documents",
        headers=headers,
        json={"title": "Story", "content": ""},
    ).json()["id"]
    monkeypatch.setattr(main, "_require_ai_key", lambda: None)
    monkeypatch.setattr(
        rag,
        "retrieve",
        lambda *_args, **_kwargs: ["A town where nobody remembers yesterday."],
    )
    from app import llm

    received = {}

    def fake_assist(mode, request, selection, context, writing_type="general"):
        received.update(
            mode=mode,
            request=request,
            selection=selection,
            context=context,
            writing_type=writing_type,
        )
        return {"options": ["A concise premise."] * 5}

    monkeypatch.setattr(llm, "assist", fake_assist)
    response = client.post(
        "/assist",
        headers=headers,
        json={
            "doc_id": document_id,
            "mode": "story_ideas",
            "request": "Give me a mystery premise",
            "writing_type": "story",
        },
    )

    assert response.status_code == 200
    assert response.json()["mode"] == "story_ideas"
    assert len(response.json()["options"]) == 5
    assert received["selection"] == ""
    assert received["writing_type"] == "story"
    assert received["context"] == ["A town where nobody remembers yesterday."]


def test_story_ideas_llm_uses_story_guidance_without_selected_text(monkeypatch):
    from app import llm

    ideas = [
        "A lighthouse keeper receives tomorrow's weather reports.",
        "A family inherits a house that appears only in photographs.",
        "A village votes each year on which memory to forget.",
        "A map redraws itself around its owner's secrets.",
        "Two rivals discover they share the same impossible dream.",
    ]

    class FakeModels:
        request = None

        def generate_content(self, **kwargs):
            self.request = kwargs
            return SimpleNamespace(text=json.dumps(ideas))

    models = FakeModels()
    monkeypatch.setattr(
        llm,
        "_client",
        lambda: SimpleNamespace(models=models, close=lambda: None),
    )

    result = llm.assist(
        "story_ideas",
        "Give me a mystery premise",
        "",
        ["A town where nobody remembers yesterday."],
        writing_type="story",
    )

    assert result == {"options": ideas}
    assert "Treat this as fiction." in models.request["config"].system_instruction
    assert models.request["config"].response_mime_type == "application/json"
    assert "town where nobody remembers yesterday" in models.request["contents"]
    assert models.request["model"] == "gemini-3.8-flash"


def test_gemini_auth_error_explains_how_to_fix_the_key():
    from app import llm

    error = SimpleNamespace(code=401)
    response = llm._gemini_error(error)

    assert response.status_code == 503
    assert "GEMINI_API_KEY" in response.detail


def test_full_writing_assistant_reference_export_restore_flow(client, monkeypatch):
    headers = auth_headers(register(client))
    first_content = json.dumps(
        {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "A first saved passage."}],
                }
            ],
        }
    )
    document_id = client.post(
        "/documents",
        headers=headers,
        json={"title": "Flow", "content": first_content},
    ).json()["id"]
    second_content = json.dumps(
        {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "A second saved passage."}],
                }
            ],
        }
    )
    monkeypatch.setattr(main, "_require_ai_key", lambda: None)
    monkeypatch.setattr(rag, "index_document", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(rag, "index_reference", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(
        rag,
        "retrieve",
        lambda *_args, **_kwargs: ["A reference style note."],
    )
    from app import llm

    monkeypatch.setattr(llm, "ghost", lambda *_args, **_kwargs: "under the stars")
    monkeypatch.setattr(
        llm,
        "assist",
        lambda *_args, **_kwargs: {"options": ["glowing", "shining"]},
    )

    client.put(
        f"/documents/{document_id}",
        headers=headers,
        json={"content": second_content},
    )
    assert client.post(
        "/index",
        headers=headers,
        json={"doc_id": document_id, "text": "A second saved passage."},
    ).status_code == 200
    suggestion = client.post(
        "/suggest",
        headers=headers,
        json={"doc_id": document_id, "text_before": "A sufficiently long sentence"},
    )
    assert suggestion.status_code == 200
    suggestion_chunks = [
        json.loads(line[len("data: ") :])["text"]
        for line in suggestion.text.splitlines()
        if line.startswith("data: {")
    ]
    assert "".join(suggestion_chunks) == "under the stars"

    assistant = client.post(
        "/assist",
        headers=headers,
        json={
            "doc_id": document_id,
            "mode": "synonyms",
            "selection": "second",
        },
    )
    assert assistant.status_code == 200
    assert assistant.json()["options"] == ["glowing", "shining"]

    uploaded = client.post(
        "/reference",
        headers=headers,
        data={"doc_id": str(document_id)},
        files={"file": ("voice.txt", b"An example writing voice.", "text/plain")},
    )
    assert uploaded.status_code == 201
    assert client.get(
        f"/documents/{document_id}/references",
        headers=headers,
    ).json()[0]["title"] == "voice.txt"

    assert client.get(
        f"/documents/{document_id}/export?format=docx",
        headers=headers,
    ).content.startswith(b"PK")
    assert client.get(
        f"/documents/{document_id}/export?format=pdf",
        headers=headers,
    ).content.startswith(b"%PDF-")

    third_content = second_content.replace("second", "third")
    client.put(
        f"/documents/{document_id}",
        headers=headers,
        json={"content": third_content},
    )
    version_id = client.get(
        f"/documents/{document_id}/versions",
        headers=headers,
    ).json()[0]["id"]
    restored = client.post(
        f"/documents/{document_id}/restore/{version_id}",
        headers=headers,
    )
    assert restored.status_code == 200
    assert restored.json()["content"] == first_content
    with SessionLocal() as db:
        latest_version = db.get(
            DocumentVersion,
            client.get(
                f"/documents/{document_id}/versions",
                headers=headers,
            ).json()[0]["id"],
        )
        assert latest_version.content == third_content
