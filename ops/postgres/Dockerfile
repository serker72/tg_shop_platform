FROM postgres:17.11
# postgresql-17-partman is not version-pinned: PGDG apt keeps only the latest build.
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-17-partman \
    && rm -rf /var/lib/apt/lists/*
