"""The mock service's Google-shaped surface (Phase 7 A1): the shapes `gmail` and
`google_calendar` will speak against Google itself, exercised without a connector in between."""

import base64
from email.message import EmailMessage

import httpx
import pytest

pytestmark = pytest.mark.asyncio

_AUTH = {"Authorization": "Bearer mock-access-1"}


def _raw(to: str, subject: str, body: str) -> str:
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    return base64.urlsafe_b64encode(bytes(message)).decode().rstrip("=")


async def test_a_draft_round_trips_through_base64url_rfc5322(mock_services_url: str) -> None:
    async with httpx.AsyncClient(base_url=mock_services_url) as http:
        resp = await http.post(
            "/gmail/v1/users/me/drafts",
            json={"message": {"raw": _raw("jordan@acmerobotics.example", "Renewal", "Hi Jordan")}},
            headers=_AUTH,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"].startswith("draft-")

        drafts = (await http.get("/gmail/drafts")).json()

    assert [(d["to"], d["subject"], d["body"]) for d in drafts] == [
        (["jordan@acmerobotics.example"], "Renewal", "Hi Jordan")
    ]


async def test_search_returns_ids_only_and_the_body_arrives_base64url(
    mock_services_url: str,
) -> None:
    async with httpx.AsyncClient(base_url=mock_services_url) as http:
        listed = (
            await http.get(
                "/gmail/v1/users/me/messages",
                params={"q": "from:jordan renewal", "maxResults": 5},
                headers=_AUTH,
            )
        ).json()
        assert [set(m) for m in listed["messages"]] == [{"id", "threadId"}]

        message = (
            await http.get(
                f"/gmail/v1/users/me/messages/{listed['messages'][0]['id']}", headers=_AUTH
            )
        ).json()

    headers = {h["name"]: h["value"] for h in message["payload"]["headers"]}
    assert headers["From"] == "jordan@acmerobotics.example"
    data = message["payload"]["body"]["data"]
    assert "renewal terms" in base64.urlsafe_b64decode(data + "==").decode()


async def test_free_busy_reports_the_attendees_seeded_meeting(mock_services_url: str) -> None:
    async with httpx.AsyncClient(base_url=mock_services_url) as http:
        seeded = (
            await http.get("/calendar/v3/calendars/primary/events", headers=_AUTH)
        ).json()["items"]
        acme = next(
            e
            for e in seeded
            if "jordan@acmerobotics.example" in [a["email"] for a in e["attendees"]]
        )
        free_busy = (
            await http.post(
                "/calendar/v3/freeBusy",
                json={
                    "timeMin": "2000-01-01T00:00:00+00:00",
                    "timeMax": "2100-01-01T00:00:00+00:00",
                    "items": [{"id": "jordan@acmerobotics.example"}, {"id": "nobody@example.com"}],
                },
                headers=_AUTH,
            )
        ).json()

    assert free_busy["calendars"]["jordan@acmerobotics.example"]["busy"] == [
        {"start": acme["start"]["dateTime"], "end": acme["end"]["dateTime"]}
    ]
    assert free_busy["calendars"]["nobody@example.com"]["busy"] == []


async def test_an_event_created_google_style_comes_back_from_the_event_list(
    mock_services_url: str,
) -> None:
    body = {
        "summary": "Renewal call",
        "start": {"dateTime": "2026-10-01T09:00:00+00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-10-01T09:30:00+00:00", "timeZone": "UTC"},
        "attendees": [{"email": "priya@globex.example"}],
    }
    async with httpx.AsyncClient(base_url=mock_services_url) as http:
        created = (
            await http.post(
                "/calendar/v3/calendars/primary/events",
                json=body,
                params={"sendUpdates": "none"},
                headers=_AUTH,
            )
        ).json()
        listed = (
            await http.get(
                "/calendar/v3/calendars/primary/events",
                params={"timeMin": "2026-10-01T00:00:00+00:00"},
                headers=_AUTH,
            )
        ).json()

    assert created["start"] == body["start"] and created["attendees"] == body["attendees"]
    assert [e["id"] for e in listed["items"]] == [created["id"]]


async def test_a_request_without_a_token_is_refused_in_googles_envelope(
    mock_services_url: str,
) -> None:
    async with httpx.AsyncClient(base_url=mock_services_url) as http:
        resp = await http.get("/gmail/v1/users/me/messages")

    assert resp.status_code == 401
    assert resp.json()["error"]["status"] == "UNAUTHENTICATED"


async def test_the_token_endpoint_issues_rotates_and_refuses_a_revoked_grant(
    mock_services_url: str,
) -> None:
    async with httpx.AsyncClient(base_url=mock_services_url) as http:
        first = (
            await http.post("/oauth/token", data={"grant_type": "authorization_code", "code": "c"})
        ).json()
        second = (
            await http.post(
                "/oauth/token",
                data={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
            )
        ).json()
        revoked = await http.post(
            "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "revoked"}
        )

    assert first["refresh_token"] and "refresh_token" not in second
    assert second["access_token"] != first["access_token"]
    assert revoked.status_code == 400 and revoked.json()["error"] == "invalid_grant"
