#!/usr/bin/env bash
# Publica o app com HTTPS (Caddy). Uso:  sudo bash ~/meuprecinho/deploy/setup-public.sh gerto.com.br [porta]
# Idempotente. Não mexe no serviço do app (price-watch) nem no Tailscale.
set -euo pipefail

DOMAIN="${1:-}"; PORT="${2:-}"
ok()   { printf '\033[32m✔ %s\033[0m\n' "$*"; }
fail() { printf '\033[31m✘ %s\033[0m\n' "$*" >&2; exit 1; }
[ -n "$DOMAIN" ] || fail "Informe o domínio: sudo bash setup-public.sh gerto.com.br"
[ "$(id -u)" -eq 0 ] || fail "Rode com sudo"

# 1) Porta do app (lida do próprio serviço, se não for informada)
if [ -z "$PORT" ]; then
  PID="$(systemctl show price-watch -p MainPID --value 2>/dev/null || echo 0)"
  PORT="$(ss -ltnpH 2>/dev/null | awk -v p="pid=$PID," '$0 ~ p {n=split($4,a,":"); print a[n]; exit}')"
  PORT="${PORT:-8000}"
fi
curl -fsS -m 5 "http://127.0.0.1:$PORT/healthz" >/dev/null \
  || fail "O app não respondeu em 127.0.0.1:$PORT. Confira com: ss -ltnp  (e informe a porta como 2º argumento)"
ok "App respondendo em 127.0.0.1:$PORT"

# 2) O domínio precisa apontar para esta VM (senão o certificado falha e o limite do Let's Encrypt queima)
EXT="$(curl -fsS -m 5 -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null \
  || curl -fsS -m 8 https://api.ipv4.ip.sb/ip 2>/dev/null || true)"
DNS="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')"
[ -n "$EXT" ] || fail "Não consegui descobrir o IP externo desta VM"
[ -n "$DNS" ] || fail "O domínio $DOMAIN ainda não resolve. Crie o registro A no Registro.br e aguarde alguns minutos."
[ "$EXT" = "$DNS" ] || fail "$DOMAIN aponta para $DNS, mas esta VM é $EXT. Corrija o registro A e tente de novo."
ok "$DOMAIN aponta para esta VM ($EXT)"

# 3) Caddy
if ! command -v caddy >/dev/null; then
  apt-get update -qq && apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gpg
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy
fi
ok "Caddy instalado ($(caddy version | cut -d' ' -f1))"

# 4) Configuração. Escuta só no IP interno da VM: o Tailscale Serve (se ainda ligado) usa a porta 443 no IP do Tailscale.
LOCAL_IP="$(ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1); exit}')"
[ -n "$LOCAL_IP" ] || fail "Não achei o IP interno da VM"
[ -f /etc/caddy/Caddyfile ] && cp -n /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak || true
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
	bind $LOCAL_IP
	encode gzip
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options "nosniff"
		Referrer-Policy "strict-origin-when-cross-origin"
		Content-Security-Policy "frame-ancestors 'none'"
		-Server
	}
	reverse_proxy 127.0.0.1:$PORT
}
CADDY
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null || fail "Configuração do Caddy inválida"
systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy
ok "Caddy configurado e reiniciado"

# 5) Verificação (o certificado pode levar até ~1 minuto)
for i in $(seq 1 20); do
  if curl -fsS -m 8 "https://$DOMAIN/healthz" >/dev/null 2>&1; then
    ok "https://$DOMAIN está no ar"
    echo "Próximo passo: abra https://$DOMAIN no celular com DADOS MÓVEIS e entre."
    exit 0
  fi
  sleep 3
done
echo "Últimas linhas do Caddy:"; journalctl -u caddy -n 15 --no-pager | tail -15
fail "O endereço ainda não respondeu. Confira se as portas 80 e 443 estão liberadas no Google Cloud."
