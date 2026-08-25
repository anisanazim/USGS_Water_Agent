FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src/ src/
COPY metadata/raw/ metadata/raw/

RUN pip install --no-cache-dir -e .

EXPOSE 9999

CMD ["uvicorn", "--app-dir", "src", "agent:create_app", "--factory", "--host", "0.0.0.0", "--port", "9999"]
