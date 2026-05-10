"""
PageIndex REST API
==================
Wraps PageIndexClient as HTTP endpoints so any project can index documents
and query their tree structures over the network.

Endpoints
---------
GET  /health
POST /index                              — upload file, get doc_id back
GET  /documents                          — list all indexed documents
GET  /documents/{doc_id}                 — document metadata
GET  /documents/{doc_id}/structure       — full tree structure
GET  /documents/{doc_id}/pages?pages=5-7 — page text content
DELETE /documents/{doc_id}              — remove document
"""

import json
import os
import shutil
import tempfile
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

from pageindex import PageIndexClient

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WORKSPACE_DIR = os.getenv("PAGEINDEX_WORKSPACE", "/app/workspace")
DOCS_DIR = os.getenv("PAGEINDEX_DOCS_DIR", "/app/docs")
MODEL = os.getenv("PAGEINDEX_MODEL", "anthropic/claude-sonnet-4-5")

SUPPORTED_EXTENSIONS = {".pdf", ".md", ".markdown"}

# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------

_client: PageIndexClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise the shared PageIndexClient on startup."""
    global _client
    Path(WORKSPACE_DIR).mkdir(parents=True, exist_ok=True)
    Path(DOCS_DIR).mkdir(parents=True, exist_ok=True)

    _client = PageIndexClient(
        model=MODEL,
        workspace=WORKSPACE_DIR,
    )
    print(f"[PageIndex API] Workspace : {WORKSPACE_DIR}")
    print(f"[PageIndex API] Docs dir  : {DOCS_DIR}")
    print(f"[PageIndex API] Model     : {MODEL}")
    print(f"[PageIndex API] Documents loaded: {len(_client.documents)}")
    yield
    # Nothing to teardown


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="PageIndex API",
    description="Vectorless reasoning-based RAG — index and retrieve document structure over HTTP.",
    version="1.0.0",
    lifespan=lifespan,
)


def _get_client() -> PageIndexClient:
    if _client is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    return _client


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", tags=["System"])
def health():
    """Returns service status and count of indexed documents."""
    client = _get_client()
    return {"status": "ok", "indexed_documents": len(client.documents)}


@app.post("/index", tags=["Indexing"])
def index_document(file: UploadFile = File(...)):
    """
    Upload a PDF or Markdown file and index it.

    - Saves the file permanently in the docs directory.
    - Builds the PageIndex tree structure and persists it in the workspace.
    - Returns the assigned `doc_id`.
    """
    client = _get_client()

    ext = Path(file.filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Supported: {', '.join(SUPPORTED_EXTENSIONS)}",
        )

    # Save uploaded file permanently to the docs directory
    dest_path = Path(DOCS_DIR) / file.filename
    # Avoid collisions by appending a counter if the name exists
    counter = 1
    stem = Path(file.filename).stem
    while dest_path.exists():
        dest_path = Path(DOCS_DIR) / f"{stem}_{counter}{ext}"
        counter += 1

    try:
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save file: {e}")

    try:
        doc_id = client.index(str(dest_path))
    except Exception as e:
        # Clean up saved file on indexing failure
        dest_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Indexing failed: {e}")

    return {
        "doc_id": doc_id,
        "filename": dest_path.name,
        "message": "Document indexed successfully",
    }


@app.get("/documents", tags=["Retrieval"])
def list_documents():
    """Return a list of all indexed document IDs and their metadata."""
    client = _get_client()
    result = []
    for doc_id in client.documents:
        try:
            meta = json.loads(client.get_document(doc_id))
        except Exception:
            meta = {"doc_id": doc_id}
        result.append(meta)
    return {"documents": result, "total": len(result)}


@app.get("/documents/{doc_id}", tags=["Retrieval"])
def get_document(doc_id: str):
    """Return metadata for a specific document (name, description, page count, etc.)."""
    client = _get_client()
    raw = client.get_document(doc_id)
    data = json.loads(raw)
    if "error" in data:
        raise HTTPException(status_code=404, detail=data["error"])
    return data


@app.get("/documents/{doc_id}/structure", tags=["Retrieval"])
def get_structure(doc_id: str):
    """
    Return the full hierarchical tree structure for a document.

    Text fields are omitted to keep the response compact.
    Use `/pages` to fetch the actual text content for specific page ranges.
    """
    client = _get_client()
    raw = client.get_document_structure(doc_id)
    data = json.loads(raw)
    if isinstance(data, dict) and "error" in data:
        raise HTTPException(status_code=404, detail=data["error"])
    return {"doc_id": doc_id, "structure": data}


@app.get("/documents/{doc_id}/pages", tags=["Retrieval"])
def get_pages(
    doc_id: str,
    pages: str = Query(
        ...,
        description="Page range string. Examples: '5', '5-7', '3,8,12', '1-3,7'",
        example="1-3",
    ),
):
    """
    Return the text content for the specified pages of a document.

    - For PDF documents, pages are physical page numbers (1-indexed).
    - For Markdown documents, pages correspond to heading line numbers.
    """
    client = _get_client()
    raw = client.get_page_content(doc_id, pages)
    data = json.loads(raw)
    if isinstance(data, dict) and "error" in data:
        status = 404 if "not found" in data["error"].lower() else 400
        raise HTTPException(status_code=status, detail=data["error"])
    return {"doc_id": doc_id, "pages": pages, "content": data}


@app.delete("/documents/{doc_id}", tags=["Indexing"])
def delete_document(doc_id: str):
    """
    Remove a document from the index.

    Deletes the workspace JSON and meta entry. The original uploaded file in
    the docs directory is NOT deleted (you may still need it).
    """
    client = _get_client()
    if doc_id not in client.documents:
        raise HTTPException(status_code=404, detail=f"Document {doc_id} not found")

    # Remove from in-memory store
    del client.documents[doc_id]

    # Remove workspace JSON
    if client.workspace:
        doc_json = client.workspace / f"{doc_id}.json"
        doc_json.unlink(missing_ok=True)
        # Rebuild meta index without this entry
        meta_path = client.workspace / "_meta.json"
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                meta.pop(doc_id, None)
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
            except Exception:
                pass  # Non-fatal

    return {"doc_id": doc_id, "message": "Document removed from index"}
