# Single Dockerfile for every service in the system.
# The service code is selected with the SERVICE build argument, so all images
# share the same layers for the common library and the protobuf stubs.
FROM python:3.12-slim

ARG SERVICE

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app:/app/gen

WORKDIR /app

COPY backend/common/requirements.txt /app/common/requirements.txt
RUN pip install --no-cache-dir -r /app/common/requirements.txt

# Shared gRPC contracts and the stubs generated from them.
COPY proto/ /app/proto/
RUN mkdir -p /app/gen && python -m grpc_tools.protoc \
      --proto_path=/app/proto \
      --python_out=/app/gen \
      --grpc_python_out=/app/gen \
      /app/proto/*.proto

# Shared library and the service package selected by the build argument.
COPY backend/common/ /app/common/
COPY backend/${SERVICE}/ /app/service/

# Static web UI served by the BFF.
COPY frontend/web/ /app/frontend/web/

CMD ["python", "-m", "service.main"]
