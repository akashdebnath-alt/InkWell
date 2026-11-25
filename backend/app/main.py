import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi.exceptions import RequestValidationError
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from . import auth
from .config import settings
from .db import Document, DocumentVersion, ReferenceMaterial, User, get_db, init_db
from .rate_limit import auth_rate_limiter, user_rate_limiter

logger = logging.getLogger(__name__)
VERSION_INTERVAL = timedelta(minutes=5)
MAX_REFERENCE_BYTES = 1024 * 1024
WritingType = Literal["general", "poem", "story", "essay"]


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    if not settings.has_gemini_api_key:
        logger.warning(
            "GEMINI_API_KEY is missing or still a placeholder in backend/.env; "
            "AI routes will return 503 until it is configured."
        )
    if not settings.jwt_secret_key.strip():
        logger.warning(
            "JWT_SECRET_KEY is not configured; access tokens will be invalidated "
            "when the server restarts. Set it in backend/.env for persistent sessions."
        )
    yield


app = FastAPI(title="Inkwell API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
def open_frontend():
    frontend_origin = next(
        (origin for origin in settings.allowed_origins if origin != "*"),
        "http://localhost:5173",
    )
    return RedirectResponse(f"{frontend_origin.rstrip('/')}/")


@app.exception_handler(SQLAlchemyError)
async def database_error_handler(_, exc: SQLAlchemyError):
    logger.error(
        "Database operation failed",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "The database operation failed. Please try again."},
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(_, exc: RequestValidationError):
    errors = exc.errors()
    detail = errors[0]["msg"] if errors else "Invalid request."
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": detail},
    )


class RegisterIn(BaseModel):
    email: str = Field(min_length=6, max_length=320)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("A valid email address is required")
        return value


class LoginIn(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=128)


class DocumentCreate(BaseModel):
    title: str = Field(default="Untitled", min_length=1, max_length=255)
    content: str = ""


class DocumentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = None


class GhostIn(BaseModel):
    doc_id: int = Field(gt=0)
    text_before: str = Field(min_length=1, max_length=5000)
    writing_type: WritingType = "general"


class AssistIn(BaseModel):
    doc_id: int = Field(gt=0)
    mode: str = Field(min_length=1, max_length=32)
    request: str = Field(default="", max_length=2000)
    selection: str = Field(default="", max_length=10000)
    writing_type: WritingType = "general"


class IndexIn(BaseModel):
    doc_id: int
    text: str


def _reference_response(reference: ReferenceMaterial) -> dict:
    return {
        "id": reference.id,
        "document_id": reference.document_id,
        "title": reference.title,
    }


def _access_token_response(user: User) -> dict:
    return {
        "access_token": auth.create_access_token(user.id),
        "token_type": "bearer",
        "user": {"id": user.id, "email": user.email},
    }


def _owned_document(doc_id: int, user: User, db: Session) -> Document:
    document = db.get(Document, doc_id)
    if document is None or document.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document


def _document_response(document: Document) -> dict:
    return {
        "id": document.id,
        "user_id": document.user_id,
        "title": document.title,
        "content": document.content,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@app.post("/auth/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)):
    auth_rate_limiter.check(request.client.host if request.client else "unknown")
    if db.scalar(select(User.id).where(User.email == body.email)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already registered")
    user = User(email=body.email, password_hash=auth.hash_password(body.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email is already registered",
        ) from None
    db.refresh(user)
    return _access_token_response(user)


@app.post("/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    auth_rate_limiter.check(request.client.host if request.client else "unknown")
    email = body.email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not auth.verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    return _access_token_response(user)


@app.get("/documents")
def list_documents(
    user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)
):
    documents = db.scalars(
        select(Document)
        .where(Document.user_id == user.id)
        .order_by(Document.updated_at.desc(), Document.id.desc())
    ).all()
    return [_document_response(document) for document in documents]


@app.post("/documents", status_code=status.HTTP_201_CREATED)
def create_doc(
    body: DocumentCreate,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    document = Document(user_id=user.id, title=body.title, content=body.content)
    db.add(document)
    db.commit()
    db.refresh(document)
    return _document_response(document)


@app.get("/documents/{doc_id}")
def get_doc(
    doc_id: int,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    return _document_response(_owned_document(doc_id, user, db))


@app.put("/documents/{doc_id}")
def save_doc(
    doc_id: int,
    body: DocumentUpdate,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    document = _owned_document(doc_id, user, db)
    previous_content = document.content
    changed = False
    if body.title is not None and body.title != document.title:
        document.title = body.title
        changed = True
    if body.content is not None and body.content != document.content:
        document.content = body.content
        changed = True

    if changed:
        now = _utcnow()
        latest_version = db.scalar(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document.id)
            .order_by(DocumentVersion.created_at.desc(), DocumentVersion.id.desc())
            .limit(1)
        )
        if latest_version is None or now - latest_version.created_at >= VERSION_INTERVAL:
            db.add(
                DocumentVersion(
                    document_id=document.id,
                    content=previous_content,
                    created_at=now,
                )
            )
        document.updated_at = now
        db.commit()
        db.refresh(document)
    return _document_response(document)


@app.get("/documents/{doc_id}/export")
def export_document(
    doc_id: int,
    format: str,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    document = _owned_document(doc_id, user, db)
    if format not in {"docx", "pdf"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Choose docx or pdf as the export format.",
        )

    from . import exports

    if format == "docx":
        content = exports.create_docx(document.content)
        media_type = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
    else:
        content = exports.create_pdf(document.content)
        media_type = "application/pdf"
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", document.title).strip(" .")
    safe_title = (safe_title[:120].rstrip(" .") or "document")
    if re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", safe_title, re.IGNORECASE):
        safe_title = f"_{safe_title}"
    filename = f"{safe_title}.{format}"
    fallback_filename = filename.encode("ascii", "replace").decode("ascii")
    fallback_filename = re.sub(r"[^\x20-\x7e]", "_", fallback_filename)
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{fallback_filename}"; '
                f"filename*=UTF-8''{quote(filename, safe='')}"
            )
        },
    )


@app.get("/documents/{doc_id}/versions")
def list_document_versions(
    doc_id: int,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    _owned_document(doc_id, user, db)
    versions = db.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == doc_id)
        .order_by(DocumentVersion.created_at.desc(), DocumentVersion.id.desc())
    ).all()
    return [
        {"id": version.id, "created_at": version.created_at}
        for version in versions
    ]


@app.post("/documents/{doc_id}/restore/{version_id}")
def restore_document_version(
    doc_id: int,
    version_id: int,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    document = _owned_document(doc_id, user, db)
    version = db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.id == version_id,
            DocumentVersion.document_id == doc_id,
        )
    )
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document version not found.",
        )

    now = _utcnow()
    db.add(
        DocumentVersion(
            document_id=doc_id,
            content=document.content,
            created_at=now,
        )
    )
    document.content = version.content
    document.updated_at = now
    db.commit()
    db.refresh(document)

    from . import exports, rag

    try:
        rag.index_document(doc_id, exports.extract_plain_text(document.content))
    except rag.RAG_ERRORS as exc:
        logger.exception("Could not re-index restored document %s", doc_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The version was restored, but its search index could not be updated.",
        ) from exc
    return _document_response(document)


@app.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_doc(
    doc_id: int,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    document = _owned_document(doc_id, user, db)
    from . import rag

    try:
        rag.delete_document(doc_id)
    except rag.RAG_ERRORS as exc:
        logger.exception("Could not remove Chroma data for document %s", doc_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not remove this document's search data. Please try again.",
        ) from exc
    db.delete(document)
    db.commit()


@app.post("/reference", status_code=status.HTTP_201_CREATED)
async def upload_reference(
    doc_id: int = Form(...),
    file: UploadFile = File(...),
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    _owned_document(doc_id, user, db)
    filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not filename or Path(filename).suffix.lower() not in {".txt", ".md"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Upload a .txt or .md reference file.",
        )
    if len(filename) > 255:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reference filenames must be 255 characters or fewer.",
        )

    contents = await file.read(MAX_REFERENCE_BYTES + 1)
    if len(contents) > MAX_REFERENCE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Reference files must be 1 MB or smaller.",
        )
    try:
        text = contents.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reference files must use UTF-8 text encoding.",
        ) from exc
    if not text.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reference files cannot be empty.",
        )

    from . import rag

    reference = ReferenceMaterial(document_id=doc_id, title=filename, text=text)
    db.add(reference)
    db.flush()
    try:
        rag.index_reference(doc_id, reference.id, text)
    except rag.RAG_ERRORS as exc:
        db.rollback()
        try:
            rag.delete_reference(doc_id, reference.id)
        except rag.RAG_ERRORS:
            logger.exception(
                "Could not clean up Chroma chunks for failed reference %s",
                reference.id,
            )
        logger.exception("Could not index uploaded reference for document %s", doc_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The reference could not be indexed. Please try again.",
        ) from exc
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        try:
            rag.delete_reference(doc_id, reference.id)
        except rag.RAG_ERRORS:
            logger.exception(
                "Could not clean up Chroma chunks for uncommitted reference %s",
                reference.id,
            )
        raise
    db.refresh(reference)
    return _reference_response(reference)


@app.get("/documents/{doc_id}/references")
def list_references(
    doc_id: int,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    _owned_document(doc_id, user, db)
    references = db.scalars(
        select(ReferenceMaterial)
        .where(ReferenceMaterial.document_id == doc_id)
        .order_by(ReferenceMaterial.id)
    ).all()
    return [_reference_response(reference) for reference in references]


@app.delete("/references/{reference_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_reference(
    reference_id: int,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    reference = db.get(ReferenceMaterial, reference_id)
    if reference is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Reference not found")
    _owned_document(reference.document_id, user, db)
    from . import rag

    try:
        rag.delete_reference(reference.document_id, reference.id)
    except rag.RAG_ERRORS as exc:
        logger.exception("Could not remove Chroma data for reference %s", reference.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not remove this reference's search data. Please try again.",
        ) from exc
    db.delete(reference)
    db.commit()


def _require_ai_key():
    if not settings.has_gemini_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI features are unavailable. Configure GEMINI_API_KEY in backend/.env.",
        )


def _retrieve_context(rag, doc_id: int, query: str, **kwargs) -> list[str]:
    try:
        return rag.retrieve(doc_id, query, **kwargs)
    except rag.RAG_ERRORS as exc:
        logger.exception("Could not retrieve writing context for document %s", doc_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Writing context is temporarily unavailable. Please try again.",
        ) from exc


@app.post("/index")
def index(
    body: IndexIn,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    _owned_document(body.doc_id, user, db)
    from . import rag

    try:
        return {"chunks": rag.index_document(body.doc_id, body.text)}
    except rag.RAG_ERRORS as exc:
        logger.exception("Could not index document %s", body.doc_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The document could not be indexed. Please try again.",
        ) from exc


def _suggest_events(suggestion: str):
    words = suggestion.split()
    for index, word in enumerate(words):
        text = word if index == len(words) - 1 else f"{word} "
        yield f"data: {json.dumps({'text': text})}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/suggest")
def suggest(
    body: GhostIn,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    _owned_document(body.doc_id, user, db)
    if not body.text_before.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Type some text before requesting a suggestion.",
        )
    user_rate_limiter.check(user.id)
    _require_ai_key()
    from . import llm, rag

    context = _retrieve_context(
        rag,
        body.doc_id,
        body.text_before[-300:],
        text_before_cursor=body.text_before,
    )
    suggestion = llm.ghost(
        body.text_before[-1500:],
        context,
        writing_type=body.writing_type,
    )
    return StreamingResponse(
        _suggest_events(suggestion),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/assist")
def assist(
    body: AssistIn,
    user: User = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    _owned_document(body.doc_id, user, db)
    from . import llm

    if body.mode not in llm.ASSIST_SYSTEMS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Choose a supported writing-assistant mode.",
        )
    if not body.selection.strip() and body.mode != "story_ideas":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select a word or passage in your document first.",
        )
    if body.mode == "story_ideas" and not (
        body.selection.strip() or body.request.strip()
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select some draft text or enter a story prompt first.",
        )
    user_rate_limiter.check(user.id)
    _require_ai_key()
    from . import rag

    context = _retrieve_context(
        rag,
        body.doc_id,
        body.selection.strip() or body.request.strip() or body.writing_type,
    )
    result = llm.assist(
        body.mode,
        body.request,
        body.selection,
        context,
        writing_type=body.writing_type,
    )
    return {"mode": body.mode, **result}
