# Contributing

## Development setup

```bash
git clone https://github.com/your-org/stevedore.git
cd stevedore
pip install pytest ruff
```

## Running tests

```bash
python -m pytest test_index.py -v
```

No AWS credentials needed — the test suite uses an in-process mock.

## Linting

```bash
ruff check index.py
```

## Building the Docker image

```bash
# Single arch (fast, for local testing)
docker build -t stevedore .

# Multi-arch (matches CI)
docker buildx build --platform linux/amd64,linux/arm64 -t stevedore .
```

## Pull requests

- Keep the test suite green.
- Add tests for new behaviour — see `test_index.py` for patterns.
- One logical change per PR.
- Update `README.md` if the change affects configuration or behaviour.
