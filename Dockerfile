FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY quazal ./quazal
COPY acb_rdv ./acb_rdv
COPY onlineconfig ./onlineconfig
RUN pip install --no-cache-dir . \
 && useradd --uid 1000 --user-group --no-create-home --shell /usr/sbin/nologin app

USER app

# game settings, CXB library, optional packet captures
VOLUME /data

# acb_rdv: UDP 21030 auth, 21031 secure, 40000-40199 relay (only if ACB_RDV_NAT_RELAY=1, off by default); TCP 21032 status
# onlineconfig: TCP 8080 (published as 80)
EXPOSE 21030/udp 21031/udp 40000-40199/udp 21032/tcp 8080/tcp

# one image, two services: `acb_rdv` (default) or `onlineconfig`
ENTRYPOINT ["python", "-m"]
CMD ["acb_rdv"]
