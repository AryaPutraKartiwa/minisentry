FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Copy only what the install needs first, so code changes don't bust the dependency layer.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[server]"

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
CMD ["uvicorn", "minisentry_server.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
