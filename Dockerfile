# Hugging Face Docker Space: console + company app + Playwright Chromium on one port (7860).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PORT=7860

COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt \
    && playwright install --with-deps chromium \
    && chmod -R a+rx /ms-playwright

# Spaces run the container as uid 1000.
RUN useradd -m -u 1000 user && mkdir -p /app && chown user /app
WORKDIR /app
COPY --chown=user . /app
USER user

EXPOSE 7860
CMD ["python", "space.py"]
