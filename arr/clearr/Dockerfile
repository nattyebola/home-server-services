FROM python:3-slim

WORKDIR /srv

# curl : python:3-slim ne l'embarque pas, requis par le healthcheck (même
# outil que tous les autres services du repo, cf. arr/docker-compose.yml).
RUN apt-get update && apt-get install -y --no-install-recommends curl \
	&& rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Pas de USER ici : le PUID/PGID voulu est fixé au runtime par `user:` dans
# arr/docker-compose.yml (cohérent avec le reste du repo) — /srv n'a besoin
# que d'être lisible (COPY laisse les fichiers world-readable par défaut),
# la seule écriture se fait sous /data_root (monté, voir docker-compose.yml).
ENTRYPOINT ["python", "-m", "app"]
CMD ["serve"]
