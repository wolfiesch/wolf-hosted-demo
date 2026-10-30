# LibreOffice is the comparison arm: the headless converter most agent sandboxes ship. The demo
# runs the current upstream release from The Document Foundation, not a distribution's older build.
FROM python:3.12-slim-bookworm AS libreoffice
ARG LO_VERSION=26.8.2
ARG LO_SHA256=8290c8f46cb33d6df8f681cb796e0697461ea070f86ec55b63c94d7eb9d9a476
RUN python3 -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], '/lo.tar.gz')" \
      "https://download.documentfoundation.org/libreoffice/stable/${LO_VERSION}/deb/x86_64/LibreOffice_${LO_VERSION}_Linux_x86-64_deb.tar.gz" \
 && echo "${LO_SHA256}  /lo.tar.gz" | sha256sum -c - \
 && mkdir /lo && tar -xzf /lo.tar.gz -C /lo --strip-components=1 \
 && mkdir /debs && mv /lo/DEBS/*.deb /debs/

FROM python:3.12-slim-bookworm
LABEL app=wolf-demo

# The upstream packages declare almost no system dependencies; these are the shared libraries
# headless conversion loads.
RUN --mount=type=bind,from=libreoffice,source=/debs,target=/debs \
    apt-get update \
 && apt-get install -y --no-install-recommends \
      /debs/*.deb \
      libxinerama1 libdbus-1-3 libcairo2 libnss3 libcups2 libsm6 libice6 libx11-xcb1 \
      libglib2.0-0 libfontconfig1 libfreetype6 libxrender1 libxext6 libxslt1.1 \
      fonts-dejavu-core fonts-liberation2 \
 && /opt/libreoffice26.8/program/soffice --version \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/app
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Pinned release binary; the build refuses any other bytes.
ARG WOLF_SHA256
COPY bin/wolf-x86_64-unknown-linux-musl /usr/local/bin/wolf
RUN echo "${WOLF_SHA256}  /usr/local/bin/wolf" | sha256sum -c - && chmod 0755 /usr/local/bin/wolf

COPY app ./app
COPY samples ./samples

RUN useradd --create-home --uid 10001 demo
USER demo

ENV WOLF_BIN=/usr/local/bin/wolf \
    SOFFICE_BIN=/opt/libreoffice26.8/program/soffice \
    LIBREOFFICE_ROOT=/opt/libreoffice26.8 \
    RUNS_DIR=/tmp/wolf-demo-runs \
    PYTHONUNBUFFERED=1

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--proxy-headers", "--forwarded-allow-ips", "*"]
