# Serving Lambda image — FastAPI + Mangum, served via the corpus artifact (no ChromaDB).
# Base: AWS Lambda Python runtime interface (includes the RIC). Region-agnostic.
FROM public.ecr.aws/lambda/python:3.12

# Build deps for any wheels without manylinux (kept minimal; pyarrow/boto3 ship wheels).
COPY pyproject.toml ${LAMBDA_TASK_ROOT}/
COPY src/ ${LAMBDA_TASK_ROOT}/src/

# Install the package + the server and aws extras (fastapi, mcp, pyjwt, mangum, boto3, pyarrow).
# --no-cache-dir keeps the image lean.
RUN python -m pip install --no-cache-dir "${LAMBDA_TASK_ROOT}[server,aws]"

# Mangum adapter exported at module import.
CMD ["pep_oracle.server.handler"]
