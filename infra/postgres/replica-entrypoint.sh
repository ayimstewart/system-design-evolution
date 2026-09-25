#!/bin/sh
# A read replica is a copy of the primary that keeps streaming new changes.
# First boot: clone the primary with pg_basebackup (-R writes standby config).
set -e
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "replica: cloning from $PRIMARY_HOST"
  until pg_basebackup -d "host=$PRIMARY_HOST port=5432 user=replicator password=$REPLICATION_PASSWORD" \
        -D "$PGDATA" -Fp -Xs -R -P; do
    echo "replica: primary not ready, retrying in 2s"
    rm -rf "${PGDATA:?}"/*
    sleep 2
  done
  chmod 0700 "$PGDATA"
fi
exec postgres
