FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
WORKDIR /app/backend
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --user-group --create-home appuser
RUN python -m playwright install --with-deps chromium
COPY app ./app
COPY run.py docker_entrypoint.py ./
EXPOSE 8000
# Bootstrap only prepares log ownership, then drops to appuser before starting Python API.
ENTRYPOINT ["python", "docker_entrypoint.py"]
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8000"]
