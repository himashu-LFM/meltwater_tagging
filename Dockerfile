# Bulletproof deploy for the "Apply to Meltwater" browser automation.
#
# The official Playwright image ships Chromium AND every system library it needs
# (libnss3, libatk, libgbm, …) preinstalled — so there's no apt-get / root /
# "--with-deps" step to fail, and no "Executable doesn't exist" at runtime. This
# is the standard way to run Playwright on Render (Docker runtime).
#
# The tag MUST track the Playwright version pip installs (see requirements.txt).
# If pip resolves a newer Playwright than this image, bump this tag to match
# (mcr.microsoft.com/playwright/python:v<version>-jammy) and redeploy.
FROM mcr.microsoft.com/playwright/python:v1.61.0-jammy

WORKDIR /app

# Python deps first (better layer caching).
COPY meltwater_tagger/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Guarantee the Chromium build matches the installed Playwright. Runs as root
# inside the image build, so no su/apt problem — and the image already has the
# system libraries, so plain "install chromium" (no --with-deps) is enough.
RUN python -m playwright install chromium

# App code.
COPY meltwater_tagger/ ./

# Render injects $PORT at runtime.
ENV PORT=10000
EXPOSE 10000
CMD ["sh", "-c", "gunicorn webapp.app:app --timeout 600 --workers 2 --bind 0.0.0.0:$PORT"]
