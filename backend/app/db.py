from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    func,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import settings

engine_options = {"pool_pre_ping": True}
if settings.database_url.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False}
    if settings.database_url in {"sqlite://", "sqlite:///:memory:"}:
        engine_options["poolclass"] = StaticPool
engine = create_engine(settings.database_url, **engine_options)
if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()
LongText = Text().with_variant(LONGTEXT(), "mysql")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(320), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(255), nullable=False, default="Untitled")
    content = Column(LongText, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, server_default=func.current_timestamp())
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class ReferenceMaterial(Base):
    __tablename__ = "reference_materials"

    id = Column(Integer, primary_key=True)
    document_id = Column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = Column(String(255), nullable=False)
    text = Column(LongText, nullable=False)


class DocumentVersion(Base):
    __tablename__ = "document_versions"

    id = Column(Integer, primary_key=True)
    document_id = Column(
        Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content = Column(LongText, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.current_timestamp())


def init_db():
    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
