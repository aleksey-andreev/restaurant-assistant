from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session as OrmSession, sessionmaker

from .models import GraphState, PipelineEvent


@dataclass
class GraphStateDTO:
    session_id: str
    current_node: str
    history: List[Dict[str, Any]]
    context: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class StateRepository:
    def __init__(self, session_maker: sessionmaker):
        self._session_maker = session_maker

    async def get_state_for_session(self, session_id: str) -> GraphStateDTO:
        with self._session_maker() as db:  # type: OrmSession
            row = (
                db.query(GraphState)
                .filter(GraphState.session_id == session_id)
                .one_or_none()
            )
            if row is None:
                row = GraphState(session_id=session_id, history=[], context={})
                db.add(row)
                db.commit()
                db.refresh(row)
            return GraphStateDTO(
                session_id=row.session_id,
                current_node=row.current_node,
                history=row.history or [],
                context=row.context or {},
            )

    async def append_history(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        reply: str,
    ) -> None:
        """
        Store the client-sent transcript for this turn + assistant reply.

        The SPA sends the full ``messages`` array each time (not deltas). Mirror it
        exactly, then append the new reply — avoids duplicate growth from extend().
        """
        with self._session_maker() as db:  # type: OrmSession
            row = (
                db.query(GraphState)
                .filter(GraphState.session_id == session_id)
                .one_or_none()
            )
            if row is None:
                row = GraphState(session_id=session_id, history=[], context={})
                db.add(row)

            snapshot = [dict(m) for m in messages] if messages else []
            snapshot.append({"role": "assistant", "content": reply})
            row.history = snapshot

            db.commit()

    async def merge_context_patch(self, session_id: str, patch: Dict[str, Any]) -> None:
        """Merge ``patch`` into persisted graph context (creates row if missing)."""
        if not patch:
            return
        with self._session_maker() as db:  # type: OrmSession
            row = (
                db.query(GraphState)
                .filter(GraphState.session_id == session_id)
                .one_or_none()
            )
            if row is None:
                row = GraphState(session_id=session_id, history=[], context={})
                db.add(row)
            ctx = dict(row.context or {})
            ctx.update(patch)
            row.context = ctx
            db.commit()

    async def update_current_node_and_context(
        self,
        session_id: str,
        current_node: str,
        context: Dict[str, Any],
    ) -> None:
        """
        Persist graph state changes (current_node + context) to database.

        We keep this separate from append_history so that LangGraph nodes can
        fill context incrementally and it will be available on subsequent turns.
        """
        with self._session_maker() as db:  # type: OrmSession
            row = (
                db.query(GraphState)
                .filter(GraphState.session_id == session_id)
                .one_or_none()
            )
            if row is None:
                row = GraphState(session_id=session_id, history=[], context={})
                db.add(row)

            row.current_node = current_node
            row.context = context
            db.commit()

    async def append_pipeline_events(
        self,
        session_id: str,
        batch_id: str,
        events: List[Dict[str, Any]],
    ) -> None:
        if not events:
            return
        with self._session_maker() as db:  # type: OrmSession
            for ev in events:
                st = str(ev.get("stage") or "")
                db.add(
                    PipelineEvent(
                        session_id=session_id,
                        batch_id=batch_id,
                        stage=st,
                        body=ev,
                    )
                )
            db.commit()

    async def list_pipeline_events(
        self,
        session_id: str,
        batch_id: Optional[str] = None,
        limit: int = 2000,
    ) -> List[Dict[str, Any]]:
        with self._session_maker() as db:  # type: OrmSession
            q = db.query(PipelineEvent).filter(PipelineEvent.session_id == session_id)
            if batch_id:
                q = q.filter(PipelineEvent.batch_id == batch_id)
            rows = q.order_by(PipelineEvent.id.asc()).limit(limit).all()
            out: List[Dict[str, Any]] = []
            for r in rows:
                out.append(
                    {
                        "id": r.id,
                        "session_id": r.session_id,
                        "batch_id": r.batch_id,
                        "stage": r.stage,
                        "body": r.body,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    }
                )
            return out

