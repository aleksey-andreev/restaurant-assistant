import asyncio

import pytest

from app.services.receipt_save_dialog import is_save_receipt_intent, try_handle_receipt_save


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Сохрани", True),
        ("сохранить", True),
        ("Скачай PDF", True),
        ("да", False),
        ("", False),
        ("сохрани файл пожалуйста", True),
    ],
)
def test_is_save_receipt_intent(text: str, expected: bool) -> None:
    assert is_save_receipt_intent(text) is expected


def test_try_handle_receipt_save_marks_done() -> None:
    class FakeState:
        def __init__(self) -> None:
            self.context: dict = {
                "preorder_phase": "done",
                "save_receipt_offered": True,
                "save_receipt_done": False,
            }
            self.current_node = "preorder_done"

        def to_dict(self) -> dict:
            return {"context": self.context, "current_node": self.current_node}

    state = FakeState()

    class FakeRepo:
        async def get_state_for_session(self, _sid: str) -> FakeState:
            return state

        async def update_current_node_and_context(self, _sid: str, node: str, ctx: dict) -> None:
            state.context = ctx
            state.current_node = node

        async def append_history(self, _sid: str, _msgs: list, _reply: str) -> None:
            pass

    async def _run() -> None:
        out = await try_handle_receipt_save(
            session_id="s1",
            messages=[{"role": "user", "content": "Сохрани"}],
            client_action={"type": "save_receipt"},
            ctx=state.context,
            state_repository=FakeRepo(),  # type: ignore[arg-type]
        )
        assert out is not None
        assert out["reply"] == ""
        assert state.context.get("save_receipt_done") is True

    asyncio.run(_run())
