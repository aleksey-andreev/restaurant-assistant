from __future__ import annotations

import secrets
from typing import Optional

from sqlalchemy.orm import Session as OrmSession, sessionmaker

from .models import Session as SessionModel


class SessionStore:
    def __init__(self, session_maker: sessionmaker):
        self._session_maker = session_maker

    async def get_or_create_session(
        self, session_id: Optional[str]
    ) -> SessionModel:
        # Using sync SQLAlchemy for simplicity; wrap in a thread pool in real async app.
        with self._session_maker() as db:  # type: OrmSession
            if session_id:
                existing = (
                    db.query(SessionModel)
                    .filter(SessionModel.session_id == session_id)
                    .one_or_none()
                )
                if existing:
                    return existing

            new_id = secrets.token_hex(16)
            session = SessionModel(session_id=new_id)
            db.add(session)
            db.commit()
            db.refresh(session)
            return session

