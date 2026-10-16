# Development

Backend and frontend run independently; the Vite dev server proxies `/api` and
`/ws` to the backend.

```bash
cd backend
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install --require-hashes -r requirements-dev.lock
pip install --no-deps --no-build-isolation -e ".[dev]"
SENTINEL_DATA_DIR=./data SENTINEL_ENABLE_API_DOCS=true \
  uvicorn app.main:app --reload --port 8090
```

```bash
cd frontend
npm install
npm run dev
```

Checks, all of which run in CI:

```bash
cd backend && ruff check . && ruff format --check . && mypy && pytest
```

```bash
cd frontend && npm run lint && npx tsc -b && npm test && npm run build
```
