ARG SUPPORT_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
ARG OPEND_BASE_IMAGE=docker.1ms.run/library/ubuntu:22.04

FROM ${SUPPORT_IMAGE} AS support
FROM ${OPEND_BASE_IMAGE}

ENV HOME=/var/lib/futu \
    TZ=Asia/Shanghai

COPY --from=support /etc/ssl/certs /etc/ssl/certs
COPY --from=support /usr/share/zoneinfo/Asia/Shanghai /usr/share/zoneinfo/Asia/Shanghai

COPY entrypoint.sh /usr/local/bin/opend-entrypoint
RUN chmod 0755 /usr/local/bin/opend-entrypoint \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime

WORKDIR /opt/futu
EXPOSE 11111

ENTRYPOINT ["/usr/local/bin/opend-entrypoint"]
