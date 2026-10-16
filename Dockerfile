FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

# Settings come from environment variables (set them in Railway's Variables tab):
#   ALCHEMY_API_KEY  or  VIBESCREENER_RPC   -> which node to read from
#   VIBESCREENER_DB                         -> optional; defaults to the attached Railway volume
# "run" backfills token launches once, then follows the chain forever.
CMD ["python", "-m", "vibescreener", "run"]
