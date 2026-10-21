import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, Integer, DateTime, ForeignKey, Text, LargeBinary, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from pgvector.sqlalchemy import Vector
from app.db.database import Base

EMBEDDING_DIM = 384


class User(Base):
    __tablename__ = "users"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=True)
    google_id = Column(String, unique=True, nullable=True)
    is_verified = Column(Boolean, default=False, nullable=False)
    verification_code = Column(String, nullable=True)
    verification_code_expires_at = Column(DateTime, nullable=True)
    verification_attempts = Column(Integer, default=0, nullable=False)  # brute-force protection on /verify-signup
    failed_login_attempts = Column(Integer, default=0, nullable=False)  # brute-force protection on /login
    lockout_until = Column(DateTime, nullable=True)  # set once failed_login_attempts hits the threshold
    reset_code = Column(String, nullable=True)  # kept separate from verification_code — different flow/concern
    reset_code_expires_at = Column(DateTime, nullable=True)
    reset_attempts = Column(Integer, default=0, nullable=False)  # brute-force protection on /reset-password
    created_at = Column(DateTime, default=datetime.utcnow)

    chat_sessions = relationship("ChatSession", back_populates="user")


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    is_guest = Column(Boolean, default=False, nullable=False)
    title = Column(String, default="New chat")
    total_upload_bytes = Column(Integer, default=0, nullable=False)
    last_active_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="chat_sessions")
    messages = relationship("Message", back_populates="chat_session", cascade="all, delete-orphan")
    chunks = relationship("DocumentChunk", back_populates="chat_session", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="chat_session", cascade="all, delete-orphan")
    upload_reservations = relationship("UploadReservation", back_populates="chat_session", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chat_session_id = Column(UUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=False)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    sources_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    chat_session = relationship("ChatSession", back_populates="messages")


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chat_session_id = Column(UUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=False, index=True)
    filename = Column(String, nullable=False)
    page = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    embedding = Column(Vector(EMBEDDING_DIM), nullable=False)
    element_type = Column(String, default="text", nullable=False)  # "text" | "table" (more types as Phase 4 adds them)
    section_title = Column(String, nullable=True)  # nearest heading this chunk falls under, if any

    chat_session = relationship("ChatSession", back_populates="chunks")


class Document(Base):
    __tablename__ = "documents"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chat_session_id = Column(UUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=False)
    filename = Column(String, nullable=False)
    display_title = Column(String, nullable=True)  # LLM-extracted topic/title, for natural-language matching
    content_type = Column(String, default="application/pdf")
    data = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    chat_session = relationship("ChatSession", back_populates="documents")


class UploadReservation(Base):
    """Temporary quota reservation while a PDF is being processed."""
    __tablename__ = "upload_reservations"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    chat_session_id = Column(UUID(as_uuid=True), ForeignKey("chat_sessions.id"), nullable=False, index=True)
    size_bytes = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    chat_session = relationship("ChatSession", back_populates="upload_reservations")


class RateLimitBucket(Base):
    """Persistent fixed-window counters shared by all application workers."""
    __tablename__ = "rate_limit_buckets"
    __table_args__ = (UniqueConstraint("subject_key", "scope", name="uq_rate_limit_subject_scope"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    subject_key = Column(String(64), nullable=False, index=True)
    scope = Column(String(40), nullable=False)
    window_start = Column(DateTime, nullable=False)
    request_count = Column(Integer, nullable=False, default=0)