FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e '.[openai,moomoo]'
COPY config ./config
RUN useradd --create-home --uid 10001 longvol && chown -R longvol:longvol /app
USER longvol
ENTRYPOINT ["longvol"]
CMD ["healthcheck", "--data-dir", "/app/data", "--state-dir", "/app/state"]
