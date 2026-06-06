# Use python:3.10-slim as base
FROM python:3.10-slim

# Install system dependencies for Playwright and Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    unzip \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Set up user with UID 1000 (required by Hugging Face)
RUN useradd -m -u 1000 user
WORKDIR /home/user/app

# Copy requirements and install python packages
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (specifically chromium)
RUN playwright install chromium
# Install system dependencies for Playwright browsers
RUN playwright install-deps

# Copy the rest of the application files
COPY --chown=user . .

# Set up a writeable directory for data (e.g. SQLite database)
# Hugging Face mounts persistent storage at /data if configured.
USER root
RUN mkdir -p /data && chown -R user:user /data
USER user

# Set environment variables
ENV PORT=7860
ENV DATABASE_PATH=/data/products.db

# Expose the port
EXPOSE 7860

# Command to run the application
CMD ["python", "app.py"]
