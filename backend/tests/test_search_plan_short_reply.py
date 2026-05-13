import pytest

from app.services.search_plan_short_reply import classify_search_plan_short_reply


@pytest.mark.parametrize(
    "text,expected",
    [
        ("да", "affirm"),
        ("Да!", "affirm"),
        (" ок ", "affirm"),
        ("хорошо", "affirm"),
        ("давай", "affirm"),
        ("подтверждаю", "affirm"),
        ("согласна", "affirm"),
        ("нет", "reject"),
        ("Неа.", "reject"),
        ("не подходит", "reject"),
        ("передумал", "reject"),
        ("отмена", "reject"),
        ("да, но в центре", "other"),
        ("не уверен", "other"),
        ("не", "other"),
        ("", "other"),
    ],
)
def test_classify_search_plan_short_reply(text: str, expected: str) -> None:
    assert classify_search_plan_short_reply(text) == expected
