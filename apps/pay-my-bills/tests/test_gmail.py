import base64
from datetime import UTC, datetime

from conftest import fixture
from pay_my_bills.gmail import to_email


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _message(*parts: dict) -> dict:
    return {
        "id": "abc",
        "internalDate": "1792018198000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "SCE <sce@message.sce.com>"},
                {"name": "Subject", "value": "Bill is Ready"},
            ],
            "parts": list(parts),
        },
    }


def test_prefers_the_plain_text_part():
    message = _message(
        {"mimeType": "text/plain", "body": {"data": _b64("Amount Due $1.00")}},
        {"mimeType": "text/html", "body": {"data": _b64("<p>other</p>")}},
    )
    email = to_email(message)
    assert email.text == "Amount Due $1.00"
    assert email.sender == "SCE <sce@message.sce.com>"
    assert email.subject == "Bill is Ready"
    assert email.received_at == datetime(2026, 10, 14, 22, 49, 58, tzinfo=UTC)


def test_falls_back_to_html_nested_in_related():
    html = fixture("sce_bill_ready.html")
    message = _message(
        {
            "mimeType": "multipart/related",
            "parts": [{"mimeType": "text/html", "body": {"data": _b64(html)}}],
        }
    )
    assert "Amount Due | $86.96" in to_email(message).text
