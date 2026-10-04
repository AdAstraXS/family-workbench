ARG PYTHON_IMAGE=python:3.12-slim
ARG NODE_IMAGE=node:22.23.2-bookworm-slim
FROM ${NODE_IMAGE} AS article-runtime
WORKDIR /opt/article-extractor
COPY article_extractor/package*.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund

FROM ${PYTHON_IMAGE}

ARG PIP_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
ARG PIP_DEFAULT_TIMEOUT=300

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV NODE_PATH=/opt/article-extractor/node_modules

COPY --from=article-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=article-runtime /opt/article-extractor/node_modules /opt/article-extractor/node_modules

WORKDIR /app

COPY requirements.txt /app/
RUN pip install \
    --no-cache-dir \
    --index-url "${PIP_INDEX_URL}" \
    --timeout "${PIP_DEFAULT_TIMEOUT}" \
    --retries 5 \
    -r requirements.txt

COPY . /app/
