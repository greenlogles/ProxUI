# ProxUI2 Development Makefile

.PHONY: help install install-dev test test-coverage lint format security clean docker-build docker-test

help: ## Show this help message
	@echo "Available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install production dependencies
	pip install -r requirements.txt

install-dev: ## Install development dependencies
	pip install -r requirements.txt
	pip install -r requirements-test.txt
	pre-commit install

test: ## Run tests
	pytest tests/ -v

test-coverage: ## Run tests with coverage report
	pytest tests/ -v --cov=app --cov-report=term-missing --cov-report=html --cov-report=xml

test-watch: ## Run tests in watch mode
	pytest-watch -- tests/ -v

lint: ## Run linting checks
	black --check --diff .
	isort --check-only --diff .
	flake8 .
	mypy app.py --ignore-missing-imports --disable-error-code=import-untyped || true

format: ## Format code
	black .
	isort .

security: ## Run security checks
	bandit -r . -x tests/
	safety check

clean: ## Clean up generated files
	rm -rf __pycache__/
	rm -rf .pytest_cache/
	rm -rf htmlcov/
	rm -rf .coverage
	rm -rf coverage.xml
	rm -rf .mypy_cache/
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete

docker-build: ## Build Docker image
	docker build -t proxui2:latest .

docker-test: docker-build ## Test Docker image
	docker run --rm -p 8080:8080 --name proxui2-test proxui2:latest &
	sleep 10
	curl -f http://localhost:8080/connect || (docker logs proxui2-test && exit 1)
	docker stop proxui2-test

ci: lint test security ## Run all CI checks locally

dev-setup: install-dev ## Set up development environment
	@echo "Development environment setup complete!"
	@echo "Run 'make help' to see available commands."