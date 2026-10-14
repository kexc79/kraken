FROM python:3.12-slim-bookworm

ARG IC_URL=https://download.oracle.com/otn_software/linux/instantclient/1932000/instantclient-basic-linux.x64-19.32.0.0.0dbru.zip
ARG IC_SHA256=1749ca1eb5f75b038f2b7ce0abc42eb4575928159259fc4fc851c54a8ca7b002
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip libaio1 libnsl2 \
    && mkdir -p /opt/oracle \
    && curl --fail --location --retry 3 "$IC_URL" -o /tmp/instantclient.zip \
    && echo "$IC_SHA256  /tmp/instantclient.zip" | sha256sum -c - \
    && unzip -q /tmp/instantclient.zip -d /opt/oracle \
    && ln -s /opt/oracle/instantclient_19_32 /opt/oracle/instantclient \
    && echo /opt/oracle/instantclient > /etc/ld.so.conf.d/oracle-instantclient.conf \
    && ldconfig \
    && rm /tmp/instantclient.zip \
    && apt-get purge -y --auto-remove curl unzip \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    APP_CONFIG_DIR=/config APP_LOG_DIR=/logs \
    ORACLE_CLIENT=/opt/oracle/instantclient \
    ORACLE_CLIENT_CONFIG=/config/oracle \
    LD_LIBRARY_PATH=/opt/oracle/instantclient \
    TZ=Europe/Minsk
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chmod=644 as_3.py app_config.py check_config.py ./
CMD ["python", "as_3.py"]
