#!/usr/bin/env bash
# Sobe o Agendador de forma persistente (setsid = session leader, nao morre
# quando o processo pai/terminal termina). Carrega variaveis do .env.
#
# INSTALL_DIR: pasta onde o pacote foi instalado. Por padrao usa a pasta deste
# script; sobrescreva exportando INSTALL_DIR antes de rodar, se preferir.
set -u
INSTALL_DIR="${INSTALL_DIR:-$(cd "$(dirname "$0")" && pwd)}"
DIR="$INSTALL_DIR"
PID_FILE="$DIR/app.pid"
LOG="$DIR/logs/app.log"

cd "$DIR"

# Carrega o .env (se existir) pro ambiente do processo (Client ID/Secret, marca,
# porta, admin, WhatsApp opcional etc). Formato: CHAVE=valor por linha.
if [ -f "$DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$DIR/.env"
  set +a
fi

PORT="${PORT:-5120}"

# ja rodando?
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "[start] ja rodando (pid $(cat "$PID_FILE"))"
  exit 0
fi

# porta ocupada por outra coisa?
if ss -ltn 2>/dev/null | grep -q "127.0.0.1:$PORT "; then
  echo "[start] porta $PORT ja ocupada"
  exit 0
fi

mkdir -p logs
# Rede de seguranca pro OAuth do Google atras de um reverse proxy que termina o
# TLS (o app roda em http local; o transporte externo E https de verdade).
export OAUTHLIB_INSECURE_TRANSPORT=1
# O Google devolve a UNIAO dos escopos concedidos, que pode conter mais do que
# foi pedido -> oauthlib estrito daria "Scope has changed". RELAX tolera isso.
export OAUTHLIB_RELAX_TOKEN_SCOPE=1
setsid env OAUTHLIB_INSECURE_TRANSPORT=1 OAUTHLIB_RELAX_TOKEN_SCOPE=1 \
  "$DIR/venv/bin/python3" "$DIR/app.py" "$PORT" \
  </dev/null >>"$LOG" 2>&1 &
echo $! > "$PID_FILE"
disown 2>/dev/null || true
echo "[start] iniciado pid $(cat "$PID_FILE") na porta $PORT"
