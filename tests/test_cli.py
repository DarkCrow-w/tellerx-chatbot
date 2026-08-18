from app.cli import _safe_error
from app.qwen import QwenAPIError


def test_safe_error_contains_status_and_code_but_not_message() -> None:
    output = _safe_error(
        "embedding",
        QwenAPIError("secret response detail", status_code=400, code="overdue-payment"),
    )
    assert '"http_status": 400' in output
    assert '"error_code": "overdue-payment"' in output
    assert "secret response detail" not in output
