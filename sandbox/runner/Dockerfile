# PaperCI sandbox base image. Third-party repos run here, never on the host.
# Promoted packages (see paperci/envlearn.py) are appended to base-packages.txt.
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INPUT=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    CUDA_VISIBLE_DEVICES=""

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git curl ca-certificates procps unzip \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 runner \
 && python -m venv /opt/venv \
 && mkdir -p /work /work/out /work/data \
 && chown -R runner:runner /opt/venv /work

ENV PATH=/opt/venv/bin:$PATH
USER runner
COPY --chown=runner:runner base-packages.txt /tmp/base-packages.txt
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchvision \
 && pip install --no-cache-dir -r /tmp/base-packages.txt

WORKDIR /work
CMD ["sleep", "infinity"]
