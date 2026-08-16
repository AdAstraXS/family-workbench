ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ARG PIP_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
ARG PIP_DEFAULT_TIMEOUT=300

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/
RUN pip install \
    --no-cache-dir \
    --index-url "${PIP_INDEX_URL}" \
    --timeout "${PIP_DEFAULT_TIMEOUT}" \
    --retries 5 \
    -r requirements.txt

COPY . /app/
