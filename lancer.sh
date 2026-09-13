#!/usr/bin/env bash
# Lance tout (Mac, Linux) : ./lancer.sh
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then
    exec python3 lancer.py "$@"
elif command -v python >/dev/null 2>&1; then
    exec python lancer.py "$@"
else
    echo "Python 3 est introuvable. Installez-le (https://www.python.org/downloads/) puis relancez."
    exit 1
fi
