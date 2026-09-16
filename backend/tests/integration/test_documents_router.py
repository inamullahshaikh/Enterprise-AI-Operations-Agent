"""Coverage for `relay_api/routers/documents.py` (docs/system-design.md section 11.1, FR-16):
the knowledge-base upload endpoint, its file-type validation, sha256-based dedup on re-upload,
and cross-tenant isolation — matching the RBAC/404-vs-403 conventions the rest of the API
already follows (`test_cross_tenant.py`, `test_connectors_router.py`).
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from httpx import AsyncClient

from relay_api.deps import get_ingest_dispatcher, get_object_store
from relay_api.main import app

pytestmark = pytest.mark.asyncio


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def __call__(self, workspace_id: uuid.UUID, document_id: uuid.UUID) -> None:
        self.calls.append((workspace_id, document_id))


class _FakeObjectStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        self.blobs[key] = data

    async def get_bytes(self, key: str) -> bytes:
        return self.blobs[key]


@contextmanager
def _overrides(dispatcher: _RecordingDispatcher) -> Iterator[None]:
    app.dependency_overrides[get_ingest_dispatcher] = lambda: dispatcher
    app.dependency_overrides[get_object_store] = lambda: _FakeObjectStore()
    try:
        yield
    finally:
        del app.dependency_overrides[get_ingest_dispatcher]
        del app.dependency_overrides[get_object_store]


async def _register_and_workspace(client: AsyncClient, email: str) -> tuple[dict, uuid.UUID]:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "correct horse battery staple", "full_name": "Dev"},
    )
    assert resp.status_code == 201, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    ws_resp = await client.post("/api/v1/workspaces", json={"name": "Dev Co"}, headers=headers)
    return headers, uuid.UUID(ws_resp.json()["id"])


async def test_uploading_a_markdown_document_enqueues_ingestion(client: AsyncClient) -> None:
    headers, workspace_id = await _register_and_workspace(client, "docs-upload@example.com")
    recorder = _RecordingDispatcher()
    with _overrides(recorder):
        resp = await client.post(
            f"/api/v1/workspaces/{workspace_id}/documents",
            files={"file": ("playbook.md", b"# Playbook\n\nBody.", "text/markdown")},
            headers=headers,
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "playbook.md"
    assert body["mime_type"] == "text/markdown"
    assert body["status"] == "queued"
    assert len(recorder.calls) == 1
    assert recorder.calls[0] == (workspace_id, uuid.UUID(body["id"]))


async def test_uploading_an_unsupported_extension_is_rejected(client: AsyncClient) -> None:
    headers, workspace_id = await _register_and_workspace(client, "docs-badext@example.com")
    with _overrides(_RecordingDispatcher()):
        resp = await client.post(
            f"/api/v1/workspaces/{workspace_id}/documents",
            files={"file": ("archive.zip", b"PK\x03\x04", "application/zip")},
            headers=headers,
        )
    assert resp.status_code == 400


async def test_reuploading_identical_bytes_returns_the_existing_document_without_reingesting(
    client: AsyncClient,
) -> None:
    headers, workspace_id = await _register_and_workspace(client, "docs-dedup@example.com")
    recorder = _RecordingDispatcher()
    with _overrides(recorder):
        first = await client.post(
            f"/api/v1/workspaces/{workspace_id}/documents",
            files={"file": ("notes.txt", b"Same content.", "text/plain")},
            headers=headers,
        )
        second = await client.post(
            f"/api/v1/workspaces/{workspace_id}/documents",
            files={"file": ("notes.txt", b"Same content.", "text/plain")},
            headers=headers,
        )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert len(recorder.calls) == 1  # only the first upload enqueued ingestion


async def test_list_and_get_document(client: AsyncClient) -> None:
    headers, workspace_id = await _register_and_workspace(client, "docs-list@example.com")
    with _overrides(_RecordingDispatcher()):
        upload_resp = await client.post(
            f"/api/v1/workspaces/{workspace_id}/documents",
            files={"file": ("notes.txt", b"Content.", "text/plain")},
            headers=headers,
        )
    document_id = upload_resp.json()["id"]

    list_resp = await client.get(f"/api/v1/workspaces/{workspace_id}/documents", headers=headers)
    assert list_resp.status_code == 200
    assert [d["id"] for d in list_resp.json()] == [document_id]

    get_resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/documents/{document_id}", headers=headers
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["title"] == "notes.txt"


async def test_non_member_gets_404_not_403_on_documents_routes(client: AsyncClient) -> None:
    _owner_headers, workspace_id = await _register_and_workspace(client, "docs-owner@example.com")
    outsider_headers, _ws = await _register_and_workspace(client, "docs-outsider@example.com")

    list_resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/documents", headers=outsider_headers
    )
    assert list_resp.status_code == 404

    with _overrides(_RecordingDispatcher()):
        upload_resp = await client.post(
            f"/api/v1/workspaces/{workspace_id}/documents",
            files={"file": ("notes.txt", b"Content.", "text/plain")},
            headers=outsider_headers,
        )
    assert upload_resp.status_code == 404

    get_resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/documents/{uuid.uuid4()}", headers=outsider_headers
    )
    assert get_resp.status_code == 404


async def test_a_viewer_can_list_but_not_upload(client: AsyncClient) -> None:
    owner_headers, workspace_id = await _register_and_workspace(
        client, "docs-viewer-owner@example.com"
    )
    viewer_headers, _ws = await _register_and_workspace(client, "docs-viewer@example.com")
    add_resp = await client.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={"email": "docs-viewer@example.com", "role": "viewer"},
        headers=owner_headers,
    )
    assert add_resp.status_code == 201

    list_resp = await client.get(
        f"/api/v1/workspaces/{workspace_id}/documents", headers=viewer_headers
    )
    assert list_resp.status_code == 200

    with _overrides(_RecordingDispatcher()):
        upload_resp = await client.post(
            f"/api/v1/workspaces/{workspace_id}/documents",
            files={"file": ("notes.txt", b"Content.", "text/plain")},
            headers=viewer_headers,
        )
    assert upload_resp.status_code == 403
