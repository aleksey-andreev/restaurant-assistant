from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from fastapi import APIRouter, Cookie, Depends, Query, Response
from pydantic import BaseModel, Field

from ..services.graph_runner import GraphRunner, get_graph_runner


router = APIRouter(tags=["dialog"])


class Message(BaseModel):
    role: str
    content: str


class SelectBookingCandidateAction(BaseModel):
    type: Literal["select_booking_candidate"] = "select_booking_candidate"
    index: int = Field(ge=0, description="0-based index in final_recommendations / recommendations / shortlist")


class SubmitBookingAction(BaseModel):
    type: Literal["submit_booking"] = "submit_booking"
    starts_at: str = Field(..., min_length=1, description="ISO-8601 datetime from the booking form")
    guest_count: int = Field(..., ge=1)
    guest_name: str = Field(..., min_length=1)
    guest_phone: str = Field(..., min_length=1)


class ConfirmSearchPlanAction(BaseModel):
    type: Literal["confirm_search_plan"] = "confirm_search_plan"


DialogClientAction = Annotated[
    Union[SelectBookingCandidateAction, SubmitBookingAction, ConfirmSearchPlanAction],
    Field(discriminator="type"),
]


class DialogRequest(BaseModel):
    messages: List[Message]
    client_action: Optional[DialogClientAction] = None


class DialogResponse(BaseModel):
    reply: str
    session_id: str
    state: Dict[str, Any]


class NewSessionResponse(BaseModel):
    session_id: str


class PipelineEventRow(BaseModel):
    id: int
    session_id: str
    batch_id: str
    stage: str
    body: Dict[str, Any]
    created_at: Optional[str] = None


@router.post("/dialog", response_model=DialogResponse)
async def dialog_endpoint(
    payload: DialogRequest,
    response: Response,
    session_id: Optional[str] = Cookie(default=None, alias="session_id"),
    graph_runner: GraphRunner = Depends(get_graph_runner),
) -> DialogResponse:
    """
    Main entry point for the SPA to interact with the LLM-driven graph.
    """
    ca: Optional[Dict[str, Any]] = None
    if payload.client_action is not None:
        ca = payload.client_action.model_dump()
    result = await graph_runner.run_dialog(
        messages=[m.model_dump() for m in payload.messages],
        session_id=session_id,
        client_action=ca,
    )
    # Persist session between chat turns.
    response.set_cookie(
        key="session_id",
        value=str(result["session_id"]),
        httponly=True,
        samesite="lax",
        path="/",
    )
    return DialogResponse(
        reply=result["reply"],
        session_id=result["session_id"],
        state=result.get("state", {}),
    )


@router.post("/dialog/session/new", response_model=NewSessionResponse)
async def new_dialog_session(
    response: Response,
    graph_runner: GraphRunner = Depends(get_graph_runner),
) -> NewSessionResponse:
    """
    Start a fresh conversation: new ``session_id``, new cookie, empty graph state
    on first dialog turn. Old session rows remain in DB (analytics / pipeline_events).
    """
    session = await graph_runner.session_store.get_or_create_session(None)
    new_id = session.session_id
    response.set_cookie(
        key="session_id",
        value=new_id,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return NewSessionResponse(session_id=new_id)


@router.get(
    "/dialog/sessions/{session_id}/pipeline-events",
    response_model=List[PipelineEventRow],
)
async def list_session_pipeline_events(
    session_id: str,
    batch_id: Optional[str] = Query(
        default=None,
        description="If set, only events from this graph run (one user turn)",
    ),
    limit: int = Query(default=2000, ge=1, le=10000),
    graph_runner: GraphRunner = Depends(get_graph_runner),
) -> List[PipelineEventRow]:
    rows = await graph_runner.state_repository.list_pipeline_events(
        session_id=session_id,
        batch_id=batch_id,
        limit=limit,
    )
    return [PipelineEventRow.model_validate(r) for r in rows]

