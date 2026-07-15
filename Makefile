.PHONY: install load run test docker-build

install:
	python3 -m pip install -r requirements-dev.txt

load:
	python3 etl.py load data/orders.csv

run:
	uvicorn app:app --host 0.0.0.0 --port 8000

test:
	python3 -m pytest -q

docker-build:
	docker build -t orders-api:latest .
