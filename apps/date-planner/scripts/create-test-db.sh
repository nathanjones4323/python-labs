#!/bin/sh
# Runs once, when the Postgres volume is first initialised (docker-compose.yml).
set -e
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -c "CREATE DATABASE date_planner_test"
