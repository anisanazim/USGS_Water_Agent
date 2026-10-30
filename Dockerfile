FROM python:3.12-slim

# 1. Create a non-root system user for production security
RUN useradd -m -u 1000 appuser

WORKDIR /home/appuser/app

# 2. Create and activate a virtual environment owned by appuser
ENV VIRTUAL_ENV=/home/appuser/venv
RUN python3 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# 3. Copy runtime dependencies and application source
COPY --chown=appuser:appuser pyproject.toml ./
COPY --chown=appuser:appuser src/ src/
COPY --chown=appuser:appuser metadata/raw/ metadata/raw/

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

# 4. Switch to the unprivileged user (never run as root in production)
USER appuser

EXPOSE 9999

CMD ["uvicorn", "--app-dir", "src", "agent:create_app", "--factory", "--host", "0.0.0.0", "--port", "9999"]
