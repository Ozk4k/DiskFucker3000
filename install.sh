#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "== DiskFucker3000: Linux setup =="

if ! command -v python3 >/dev/null 2>&1; then
    echo "Erro: Python 3 não encontrado. Instale Python 3 e tente novamente."
    exit 1
fi

if ! command -v lsblk >/dev/null 2>&1; then
    echo "Aviso: 'lsblk' não encontrado. Instale o pacote util-linux."
fi

if ! command -v udisksctl >/dev/null 2>&1; then
    echo "Aviso: 'udisksctl' não encontrado. Instale o pacote udisks2."
fi

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi

. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo
echo "Instalação concluída."
echo "Para executar:"
echo "  source .venv/bin/activate"
echo "  python DiskFucker3000.py"
