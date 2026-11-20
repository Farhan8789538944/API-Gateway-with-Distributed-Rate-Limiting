# One image for all Python services (gateway, products, orders, setup/admin).
# Each container runs exactly one process; the command differs per service.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Hash-checked, fully pinned dependency install (reproducible).
COPY requirements.lock ./
RUN pip install --require-hashes --no-deps -r requirements.lock

COPY shared ./shared
COPY database ./database
COPY gateway ./gateway
COPY services ./services

RUN useradd --system --uid 10001 --no-create-home app
USER app

EXPOSE 8000
CMD ["python", "-c", "print('set a command per service in compose.yaml')"]
