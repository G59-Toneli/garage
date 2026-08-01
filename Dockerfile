# Python is pinned to 3.12 (ADR-0006): torch and sentence-transformers wheels do not yet
# follow the author's local 3.14, and the arm64 target narrows availability further.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependency layer first, so source edits do not reinstall the world.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --upgrade pip && pip install -e ".[dev]"

COPY tests ./tests
# The fixture Corpus ships in the image: it is the permanent deterministic test base, so the
# container can verify its own tooling without any of the operator's material present.
COPY corpus ./corpus

EXPOSE 8000
# Entry through the package, not through `uvicorn` directly: the bind address is configuration
# like everything else, and going through `main()` means a misconfigured container fails at boot.
CMD ["python", "-m", "garage", "serve"]
