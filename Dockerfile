# Serving Lambda image — FastAPI + Mangum, served via the corpus artifact (no ChromaDB).
# Base: AWS Lambda Python runtime interface (includes the RIC). Region-agnostic.
FROM public.ecr.aws/lambda/python:3.12

# Build deps for any wheels without manylinux (kept minimal; pyarrow/boto3 ship wheels).
COPY pyproject.toml ${LAMBDA_TASK_ROOT}/
COPY src/ ${LAMBDA_TASK_ROOT}/src/

# Install the package + the server and aws extras (fastapi, mcp, pyjwt, mangum, boto3, pyarrow).
# --no-cache-dir keeps the image lean.
RUN python -m pip install --no-cache-dir "${LAMBDA_TASK_ROOT}[server,aws]"

# Defense-in-depth / misconfig-scanner hygiene, parity with the hardened Fargate ingest
# image. Bare numeric UID on purpose: this AL2023-minimal base ships no shadow-utils, so
# the Fargate `useradd` pattern would fail the build — and no chown is needed because deps
# install (as root) world-readable into /var/lang/.../site-packages and /tmp is world-
# writable (1777). Note: Lambda overrides USER at runtime, running the function as its own
# managed sandbox user, so this is scanner-facing only, not a runtime identity change. Must
# stay AFTER the pip install (which needs root) and BEFORE CMD.
USER 1000:1000

# Mangum adapter exported at module import.
CMD ["pep_oracle.server.handler"]
