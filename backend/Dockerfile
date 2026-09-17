FROM python:3.12-slim

WORKDIR /app
ENV PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt
COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
