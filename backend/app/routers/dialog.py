from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from ..services.graph_runner import GraphRunner, get_graph_runner
from ..services.toka_gateway import TokaGatewayError, get_toka_gateway


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
    table_id: Optional[str] = Field(
        default=None,
        description="Toka table id; omit or empty for automatic smallest-free-table selection",
    )


class ConfirmSearchPlanAction(BaseModel):
    type: Literal["confirm_search_plan"] = "confirm_search_plan"


class ConfirmPreorderOfferAction(BaseModel):
    type: Literal["confirm_preorder_offer"] = "confirm_preorder_offer"


class PreorderDeclineOfferAction(BaseModel):
    type: Literal["preorder_decline_offer"] = "preorder_decline_offer"


class PreorderChooseManualAction(BaseModel):
    type: Literal["preorder_choose_manual"] = "preorder_choose_manual"


class PreorderLlmPickAction(BaseModel):
    type: Literal["preorder_llm_pick"] = "preorder_llm_pick"
    preferences_text: Optional[str] = Field(default=None, max_length=4000)


class PreorderSubmitCartAction(BaseModel):
    type: Literal["preorder_submit_cart"] = "preorder_submit_cart"
    lines: List[Dict[str, Any]] = Field(default_factory=list)


class PreorderConfirmOrderAction(BaseModel):
    type: Literal["preorder_confirm_order"] = "preorder_confirm_order"


class PreorderAmendAction(BaseModel):
    type: Literal["preorder_amend"] = "preorder_amend"


DialogClientAction = Annotated[
    Union[
        SelectBookingCandidateAction,
        SubmitBookingAction,
        ConfirmSearchPlanAction,
        ConfirmPreorderOfferAction,
        PreorderDeclineOfferAction,
        PreorderChooseManualAction,
        PreorderLlmPickAction,
        PreorderSubmitCartAction,
        PreorderConfirmOrderAction,
        PreorderAmendAction,
    ],
    Field(discriminator="type"),
]


class DialogRequest(BaseModel):
    messages: List[Message]
    client_action: Optional[DialogClientAction] = None
    client_time_zone: Optional[str] = Field(
        default=None,
        max_length=128,
        description="IANA timezone (e.g. Europe/Moscow); stored once per session if not set yet",
    )


class DialogResponse(BaseModel):
    reply: str
    session_id: str
    state: Dict[str, Any]


class NewSessionRequest(BaseModel):
    client_time_zone: Optional[str] = Field(
        default=None,
        max_length=128,
        description="IANA timezone from the browser; stored in graph context for Toka list date",
    )


class NewSessionResponse(BaseModel):
    session_id: str


class PipelineEventRow(BaseModel):
    id: int
    session_id: str
    batch_id: str
    stage: str
    body: Dict[str, Any]
    created_at: Optional[str] = None


class BookingTableRow(BaseModel):
    id: str
    title: str
    capacity: int
    status: str
    free_after: Optional[str] = None


class BookingTableOptionsResponse(BaseModel):
    tables: List[BookingTableRow]
    toka_list_date: Optional[str] = None


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
        client_time_zone=payload.client_time_zone,
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
    body: Optional[NewSessionRequest] = None,
    graph_runner: GraphRunner = Depends(get_graph_runner),
) -> NewSessionResponse:
    """
    Start a fresh conversation: new ``session_id``, new cookie, empty graph state
    on first dialog turn. Old session rows remain in DB (analytics / pipeline_events).
    """
    session = await graph_runner.session_store.get_or_create_session(None)
    new_id = session.session_id
    if body and body.client_time_zone and body.client_time_zone.strip():
        await graph_runner.state_repository.merge_context_patch(
            new_id, {"client_time_zone": body.client_time_zone.strip()[:128]}
        )
    response.set_cookie(
        key="session_id",
        value=new_id,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return NewSessionResponse(session_id=new_id)


@router.get("/dialog/booking-table-options", response_model=BookingTableOptionsResponse)
async def booking_table_options(
    starts_at: str = Query(..., min_length=1),
    guest_count: int = Query(..., ge=1),
    duration_minutes: int = Query(120, ge=1, le=24 * 60),
    session_id: Optional[str] = Cookie(default=None, alias="session_id"),
    graph_runner: GraphRunner = Depends(get_graph_runner),
) -> BookingTableOptionsResponse:
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id cookie required")
    st = await graph_runner.state_repository.get_state_for_session(session_id)
    ctx = st.context or {}
    if not bool(ctx.get("booking_pending")):
        raise HTTPException(status_code=400, detail="booking is not active")
    cand = ctx.get("booking_selected_candidate")
    if not isinstance(cand, dict):
        raise HTTPException(status_code=400, detail="no restaurant selected for booking")
    tz_raw = ctx.get("client_time_zone")
    ctz = str(tz_raw).strip()[:128] if isinstance(tz_raw, str) and tz_raw.strip() else None
    try:
        gateway = await get_toka_gateway()
        data = await gateway.list_booking_table_options(
            restaurant_ref=dict(cand),
            starts_at=starts_at,
            guest_count=guest_count,
            duration_minutes=duration_minutes,
            client_time_zone=ctz,
        )
    except TokaGatewayError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    rows = data.get("tables") or []
    out: List[BookingTableRow] = []
    if isinstance(rows, list):
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                out.append(BookingTableRow.model_validate(r))
            except Exception:
                continue
    tld = data.get("toka_list_date")
    tld_s = str(tld) if isinstance(tld, str) and tld.strip() else None
    return BookingTableOptionsResponse(tables=out, toka_list_date=tld_s)


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
