# Night shift on Railway: its own service, NOT the web API (the API builds with Nixpacks
# from the Procfile and stays untouched). Selected per service via
# RAILWAY_DOCKERFILE_PATH=scripts/night_shift/Dockerfile; build context = jobflow/.
# The Playwright image ships Chromium + its system libs; the tag must match the pin below.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt playwright==1.63.0

COPY . .
ENV PYTHONUNBUFFERED=1

# One walk per container start; the service restart policy is NEVER (a crash must not
# turn into a second walk). railway_run.py reads NS_* variables, see its docstring.
CMD ["python", "scripts/night_shift/railway_run.py"]
