#!/bin/sh
set -e

# Ensure the data directory exists and is owned by the proxui user.
# This handles the case where Docker creates the bind-mount directory as root.
mkdir -p /app/data
chown -R proxui:proxui /app/data

exec gosu proxui "$@"
