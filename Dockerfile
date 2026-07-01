# Trade-Dash Phase 1 backend image. Shared by both the `api` and
# `event_bus` docker-compose services (they differ only by the command
# they run, both against this same image/dependency set).
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY event_types.py nlp_pipeline.py correlation_scorer.py unified_pipeline.py \
     event_bus.py freshness_guard.py api_service.py ./

EXPOSE 8000

# Default command runs the API service; docker-compose.yml's `event_bus`
# service overrides this to `python event_bus.py`.
CMD ["uvicorn", "api_service:app", "--host", "0.0.0.0", "--port", "8000"]
