# Python is pinned to 3.12 (ADR-0006): wheels for the inference stack do not yet follow the
# author's local 3.14, and the arm64 target narrows availability further. That pin is also why the
# baseline embedder runs under ONNX Runtime and never torch (ADR-0008) — the torch and
# sentence-transformers wheels narrow the arm64 target this image has to hit (ADR-0001).
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
# The evaluation set ships too, for the same reason: `eval gate` needs no API key and no network, so
# the container can measure its own retrieval. Without this, `ingest` and `corpus validate` work in
# the image and `eval gate` is the one command that does not.
COPY eval ./eval

# The baseline embedder's weights, vendored into the image and sha256-verified as they land — the
# same rigour the manifest applies to `documents.sha256`, and the reason the fetch is a subcommand
# rather than a `curl` here: the digests live in `garage/embedding.py` and are checked by the same
# code that later loads the files, so there is no second copy to drift (ADR-0008).
#
# Not committed to git, and never will be: 470 MB of weights is not repository content. The layer is
# last among the heavy steps and its inputs do not change, so it caches across every source edit.
ENV GARAGE_EMBEDDER_DIR=/opt/garage/embedder
RUN python -m garage embedder fetch

# The commit this image was built from. Passed by CI, empty on a local build — and empty is handled
# rather than faked: the service falls back to asking git and answers `unknown` when there is no git,
# which is what an image built from a tarball honestly is.
#
# It is not decoration. `garage/cache.py` puts it in the answer-cache key, so a deploy that changes
# the prompt starts with a cold cache instead of serving the previous build's answers under this
# build's version stamp. **Last** among the layers on purpose: it changes on every commit, and above
# the embedder fetch it would invalidate 470 MB of weights every time.
ARG GARAGE_GIT_SHA=""
ENV GARAGE_GIT_SHA=${GARAGE_GIT_SHA}

EXPOSE 8000
# Entry through the package, not through `uvicorn` directly: the bind address is configuration
# like everything else, and going through `main()` means a misconfigured container fails at boot.
CMD ["python", "-m", "garage", "serve"]
