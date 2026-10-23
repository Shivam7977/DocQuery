"""
Chat deletion as bulk SQL statements.

Deleting through the ORM (db.delete(session)) first loads every child row of
the chat: all messages, every chunk with its embedding, and the stored bytes of
every PDF. Then it deletes them one by one. For a large document that is slow
and memory-heavy, and the browser waits for the whole time.

Bulk DELETE statements remove the same rows in a few queries and never load them.
"""
from sqlalchemy.orm import Session

from app.db import models


def delete_chat_session(db: Session, chat_session_id) -> None:
    # Children first, so the foreign keys are satisfied at every step.
    for model in (models.DocumentChunk, models.Message, models.Document, models.UploadReservation):
        (db.query(model)
           .filter(model.chat_session_id == chat_session_id)
           .delete(synchronize_session=False))
    (db.query(models.ChatSession)
       .filter(models.ChatSession.id == chat_session_id)
       .delete(synchronize_session=False))
    db.commit()