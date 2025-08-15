# ProxUI Development Guide

## Testing Framework

ProxUI uses a comprehensive testing framework to ensure code quality and reliability. The test suite covers all major components including connection management, VM operations, API endpoints, and utility functions.

### Test Structure

```
tests/
├── __init__.py                    # Test package initialization
├── conftest.py                   # Pytest configuration and shared fixtures
├── test_connection_management.py # Authentication and connection renewal tests
├── test_vm_management.py         # VM/container operations tests
├── test_api_endpoints.py         # Flask API endpoint tests
├── test_utility_functions.py     # Utility function tests
└── test_integration.py           # End-to-end integration tests
```

### Running Tests

#### Local Development

```bash
# Set up development environment
make dev-setup

# Run all tests
make test

# Run tests with coverage report
make test-coverage

# Run tests in watch mode (auto-rerun on file changes)
make test-watch

# Run specific test file
pytest tests/test_connection_management.py -v

# Run specific test method
pytest tests/test_connection_management.py::TestConnectionManagement::test_is_authentication_error -v
```

#### Using pytest directly

```bash
# Basic test run
pytest tests/ -v

# With coverage
pytest tests/ -v --cov=app --cov-report=term-missing

# Run only unit tests (excluding integration)
pytest tests/ -v -m "not integration"

# Run only fast tests
pytest tests/ -v -m "not slow"
```

### Test Categories

#### 1. Connection Management Tests
- Authentication error detection
- Connection renewal mechanisms
- Cluster initialization and switching
- Connection metadata handling

#### 2. VM/Container Management Tests
- VM/container listing and filtering
- Configuration parsing (CPU, memory, network, storage)
- Guest agent integration
- SSH key parsing

#### 3. API Endpoint Tests
- REST API endpoints
- Request/response validation
- Error handling scenarios
- Authentication flows

#### 4. Utility Function Tests
- Time formatting (uptime humanization)
- Storage grouping and aggregation
- Connection helper functions
- Data transformation utilities

#### 5. Integration Tests
- Complete workflows (VM lifecycle)
- Authentication error recovery
- Multi-component interactions
- Error propagation testing

### Continuous Integration

#### GitHub Actions Workflow

The CI pipeline automatically runs on:
- **Pull Requests** to main and develop branches
- **Pushes** to main, develop, and version branches (v*)
- **Manual triggers** via workflow_dispatch

#### CI Pipeline Stages

1. **Test Matrix**
   - Python versions: 3.8, 3.9, 3.10, 3.11, 3.12
   - Ubuntu latest runner
   - Parallel execution across versions

2. **Code Quality Checks**
   ```bash
   black --check --diff .      # Code formatting
   isort --check-only --diff . # Import sorting
   flake8 .                    # Linting
   mypy app.py                 # Type checking
   ```

3. **Security Scanning**
   ```bash
   bandit -r . -x tests/       # Security linting
   safety check                # Dependency vulnerability scan
   ```

4. **Docker Testing**
   ```bash
   docker build -t proxui:test .
   docker run -d --name proxui-test -p 8080:8080 proxui:test
   curl -f http://localhost:8080/connect
   ```

#### Coverage Requirements

- **Minimum Coverage**: 80%
- **Reports**: Terminal, HTML, XML
- **Upload**: Codecov integration for coverage tracking

### Test Configuration

#### pytest.ini
```ini
[tool:pytest]
testpaths = tests
addopts = --verbose --cov=app --cov-fail-under=80
markers =
    slow: marks tests as slow
    integration: marks tests as integration tests
    unit: marks tests as unit tests
```

#### Coverage Configuration (pyproject.toml)
```toml
[tool.coverage.run]
source = ["app"]
omit = ["tests/*", "venv/*"]

[tool.coverage.report]
exclude_lines = [
    "pragma: no cover",
    "if __name__ == .__main__.:",
]
```

### Mock Strategy

Tests use comprehensive mocking to avoid external dependencies:

- **Proxmox API**: Mocked using unittest.mock
- **Database**: No real Proxmox server required
- **Network calls**: All API calls intercepted
- **File system**: Temporary files for config testing

### Writing New Tests

#### Test File Template
```python
import unittest
from unittest.mock import Mock, patch
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import function_to_test

class TestNewFeature(unittest.TestCase):
    def setUp(self):
        # Set up test data
        pass
    
    def tearDown(self):
        # Clean up
        pass
    
    def test_feature_success(self):
        # Test successful operation
        pass
    
    def test_feature_error_handling(self):
        # Test error scenarios
        pass
```

#### Using Fixtures
```python
def test_with_fixtures(self, setup_test_environment, mock_vm_data):
    # Use predefined fixtures from conftest.py
    result = function_under_test()
    assert result == expected_value
```

### Pre-commit Hooks

Development includes pre-commit hooks for code quality:

```bash
# Install hooks
pre-commit install

# Run manually
pre-commit run --all-files
```

Hooks include:
- Code formatting (black, isort)
- Linting (flake8)
- Security checks (bandit)
- Test execution (pytest)

### Performance Testing

For performance-sensitive operations:

```python
import time
import pytest

@pytest.mark.slow
def test_connection_performance(self):
    start_time = time.time()
    result = expensive_operation()
    end_time = time.time()
    
    assert end_time - start_time < 5.0  # Should complete in under 5 seconds
    assert result is not None
```

### Debugging Tests

```bash
# Run with pdb debugger
pytest tests/test_file.py::test_method --pdb

# Verbose output
pytest tests/ -v -s

# Show local variables on failure
pytest tests/ --tb=long

# Run last failed tests only
pytest --lf
```

### Test Data Management

Test data is managed through:
- **Fixtures**: Reusable test data in conftest.py
- **Mock objects**: Simulated Proxmox responses
- **Temporary files**: For configuration testing
- **Environment variables**: For test-specific settings

This testing framework ensures that all code changes are thoroughly validated before deployment, maintaining the reliability and security of the proxui application.