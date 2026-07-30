FROM python:3.11-slim

WORKDIR /app

# Playwright IS in this image -- live provisioning is enabled for the small,
# fixed set of vendors with a registered SignupRecipe (see signup_recipes.py).
# Everything else runs mocked; webapp/main.py decides per-request and the
# page states plainly which mode a given run used. No silent mocking.
# Install deps against a minimal stub package first -- an editable install
# only needs the package to exist at install time, not its real content
# (it symlinks back to src/ for imports). This keeps the expensive layer
# (pip + playwright's Chromium + apt deps) cached across ordinary source
# changes; only `COPY src ./src` below invalidates on a normal commit.
COPY pyproject.toml ./
RUN mkdir -p src/credforge && touch src/credforge/__init__.py
RUN pip install --no-cache-dir -e ".[web,llm,live]" \
    && playwright install --with-deps chromium

COPY src ./src
COPY webapp ./webapp
COPY examples ./examples

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["sh", "-c", "uvicorn webapp.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
