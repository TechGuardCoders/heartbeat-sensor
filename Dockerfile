# syntax=docker/dockerfile:1
# Multi-stage, multi-arch (amd64+arm64). Stage 1 runs the test suite as a
# build gate; stage 2 ships a minimal non-root runtime image.

FROM python:3.12-slim AS build
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir pytest && pytest -q

FROM python:3.12-slim
WORKDIR /app
RUN useradd --create-home --uid 1000 appuser
COPY --from=build --chown=appuser:appuser /app/metronome.py /app/
USER appuser
CMD ["python", "-c", "import metronome; print('metronome OK')"]
