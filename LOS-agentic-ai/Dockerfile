FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# tesseract-ocr  : word-level boxes for name spacing recovery
# poppler-utils  : pdf2image needs it to render PDF licences
# libgl1/libglib : OpenCV runtime, pulled in by rapidocr
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        poppler-utils \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY risk_main.py main.py ./
COPY README.md .env.example ./

# Download the OCR models at BUILD time. Without this the first request has to
# fetch them, which is slow and fails outright on a network that blocks the
# model host.
RUN python -c "from rapidocr_onnxruntime import RapidOCR; RapidOCR()"

RUN mkdir -p /app/runtime/uploads /app/runtime/audit

EXPOSE 8010

# risk_main and main serve the SAME application -- risk_main.py is a one-line
# alias kept because existing deployment scripts reference `risk_main:app`.
# Either works; this one is the compatible spelling.
#
# (An earlier comment here claimed main.py failed to import because of a bug
# in the extraction chain. That has not been true for some time: main.py
# imports and serves cleanly, verified on a fresh Python 3.12 environment
# built from requirements.txt alone.)
CMD ["uvicorn", "risk_main:app", "--host", "0.0.0.0", "--port", "8010"]