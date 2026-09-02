#!/usr/bin/env bash
# Referencia de deploy: publica o Agendador num subdominio via Caddy.
#
# Este e um MODELO. Ajuste as variaveis abaixo e rode como referencia (ou copie
# o bloco pro seu Caddyfile na mao). Requer Caddy instalado e DNS do subdominio
# apontando pra este servidor.
#
#   SUBDOMINIO  -> o dominio publico do agendador (ex: agendar.seudominio.com)
#   INSTALL_DIR -> pasta onde o pacote foi instalado (ex: /opt/agendador)
#   PORT        -> porta local do app Flask (default 5120)
set -u
SUBDOMINIO="${SUBDOMINIO:-agendar.seudominio.com}"
INSTALL_DIR="${INSTALL_DIR:-/opt/agendador}"
PORT="${PORT:-5120}"

# 1) garante o app no ar
bash "$INSTALL_DIR/start.sh" >/dev/null 2>&1 || true

# 2) bloco de configuracao do Caddy pra este subdominio.
#    O Caddy cuida do certificado TLS automaticamente (Let's Encrypt) desde que
#    o DNS ja aponte pra este servidor.
CADDY_BLOCK="$(cat <<CADDYBLOCO
${SUBDOMINIO} {
	header X-Robots-Tag "noindex, nofollow"
	reverse_proxy 127.0.0.1:${PORT}
}
CADDYBLOCO
)"

echo "=== Bloco Caddy sugerido para ${SUBDOMINIO} ==="
echo "$CADDY_BLOCK"
echo "================================================"
echo
echo "Como aplicar (exemplo com Caddyfile em /etc/caddy/Caddyfile):"
echo "  1) Adicione o bloco acima ao /etc/caddy/Caddyfile"
echo "  2) sudo caddy validate --config /etc/caddy/Caddyfile"
echo "  3) sudo systemctl reload caddy"
echo
echo "Depois, acesse: https://${SUBDOMINIO}/"
