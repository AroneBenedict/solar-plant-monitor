FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
# Build a full-year demo database into the image so the container starts instantly.
RUN python -m solarmon.pipeline --db /app/solar.db --seed 11 --days 365

ENV SOLARMON_DB=/app/solar.db
EXPOSE 8000
CMD ["uvicorn", "solarmon.api:app", "--host", "0.0.0.0", "--port", "8000"]
