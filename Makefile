.PHONY: install test lint db evaluate serve

install:
	pip install -e ".[dev]"

lint:
	ruff check .

test:
	pytest --cov --cov-report=term-missing

db:
	python -m solarmon.pipeline --db solar.db --seed 11 --days 365

evaluate:
	python -m solarmon.evaluate --out results

serve:
	SOLARMON_DB=solar.db uvicorn solarmon.api:app --port 8000
