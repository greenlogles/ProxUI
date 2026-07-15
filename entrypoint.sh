#!/bin/sh
set -e

# Ensure the data directory exists and is owned by the proxui user.
# This handles the case where Docker creates the bind-mount directory as root.
mkdir -p /app/data
chown -R proxui:proxui /app/data

# Drop from root to the proxui user with setpriv (util-linux, already in the
# base image) instead of gosu, which is a static Go binary that drags Go stdlib
# CVEs into vulnerability scans despite not using those code paths.
exec setpriv --reuid proxui --regid proxui --init-groups "$@"
