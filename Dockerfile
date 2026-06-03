FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WKA_AUTH_MODE=jwt \
    WKA_ALLOW_ROLE_HEADER=0

WORKDIR /app
COPY pyproject.toml README.md ./
COPY requirements-http.txt ./
RUN pip install --no-cache-dir -r requirements-http.txt
COPY . .
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
