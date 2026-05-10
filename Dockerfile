# syntax=docker/dockerfile:1
FROM public.ecr.aws/docker/library/python:3.11-slim

# ── System dependencies ───────────────────────────────────────────────────────
# libGL + libglib are required by PyMuPDF (pymupdf)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ──────────────────────────────────────────────────────────
COPY pageindex/ ./pageindex/
COPY api/       ./api/

# ── Runtime directories (overridden by bind mounts in compose) ────────────────
RUN mkdir -p /app/workspace /app/docs

# ── Expose port ───────────────────────────────────────────────────────────────
EXPOSE 8080

# ── Entry point ───────────────────────────────────────────────────────────────
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
