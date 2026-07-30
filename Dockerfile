FROM python:3.11-slim

WORKDIR /app

# Playwright IS in this image -- live provisioning is enabled for the small,
# fixed set of vendors with a registered SignupRecipe (see signup_recipes.py).
# Everything else runs mocked; webapp/main.py decides per-request and the
# page states plainly which mode a given run used. No silent mocking.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[web,llm,live]" \
    && playwright install --with-deps chromium

COPY webapp ./webapp
COPY examples ./examples

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["sh", "-c", "uvicorn webapp.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
