# Docker deploy for the Meltwater Sentiment Tagger.
#
# Uses Microsoft's official Playwright image, which already bundles Chromium AND
# every system library it needs (libnss3, libatk, fonts, etc.) — so there is no
# "--with-deps"/root problem and no "libnss3.so: cannot open shared object file"
# at runtime. Works on any Docker host: Hugging Face Spaces (free, 16 GB RAM),
# Fly.io, Google Cloud Run, Railway, a plain VM, etc.
#
# Keep the tag in sync with the "playwright" pin in requirements.txt (>=1.48).
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

# Install Python deps first (better layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The base image ships Chromium, but pin the exact build Playwright expects.
RUN python -m playwright install chromium

# App code.
COPY . .

# Hugging Face Spaces routes to port 7860 by default; other hosts inject $PORT.
# Default to 7860 but honour $PORT if the platform sets one.
ENV PORT=7860
EXPOSE 7860

# One worker keeps memory low (the API apply path only opens a short-lived
# browser for login); the long timeout covers the login + tagging round-trip.
# `sh -c` so ${PORT} is expanded at runtime.
CMD ["sh", "-c", "gunicorn webapp.app:app --workers 1 --timeout 900 --bind 0.0.0.0:${PORT}"]
