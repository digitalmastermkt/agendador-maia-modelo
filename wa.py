"""
Camada WhatsApp (OPCIONAL) do Agendador.
Envia mensagens via Evolution API, local e leve.
Isolada pra falhar graciosamente: nunca derruba o fluxo de agendamento.

Configuracao por ambiente (ver .env.example):
  EVOLUTION_URL, EVOLUTION_INSTANCE, EVOLUTION_API_KEY.
Se qualquer um faltar, o envio vira no-op gracioso (o agendamento segue normal,
so sem a mensagem automatica de WhatsApp).

DRY-RUN: se AGENDAR_WA_DRYRUN=1 no ambiente, NAO envia de verdade;
so registra no log o payload que seria enviado (usado em testes).
"""
import os
import re
import json
import time

import requests

import config


def _load_evolution_key():
    """Retorna a apikey da Evolution lida do ambiente (config.EVOLUTION_API_KEY).
    None se nao configurada."""
    key = (getattr(config, "EVOLUTION_API_KEY", "") or "").strip()
    return key or None


def normalize_phone(raw):
    """Normaliza um telefone pro padrao WhatsApp 55DDDNUMERO (so digitos).
    Regras:
      - remove tudo que nao for digito
      - se ja comeca com 55 e tem 12-13 digitos, mantem
      - se tem 10 ou 11 digitos (DDD + numero), prefixa 55
      - fora disso, devolve None (numero suspeito)
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    # tira zeros internacionais tipo 0055
    digits = digits.lstrip("0")
    if digits.startswith("55") and len(digits) in (12, 13):
        return digits
    if len(digits) in (10, 11):  # DDD + numero (fixo/celular)
        return "55" + digits
    if digits.startswith("55") and len(digits) in (11, 12):
        # ja tem 55 mas numero curto — deixa passar mesmo assim
        return digits
    return None


def _log(line):
    try:
        os.makedirs(os.path.join(config.BASE, "logs"), exist_ok=True)
        with open(os.path.join(config.BASE, "logs", "wa.log"), "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except Exception:
        pass


def send_text(phone_raw, message, slug="agendar"):
    """Envia texto via Evolution API. Retorna dict {ok, error, dryrun, number}.
    Nunca lanca excecao pra fora.
    Se o WhatsApp nao estiver configurado (sem instancia/apikey), vira no-op
    gracioso — o agendamento segue normalmente, so sem a mensagem automatica."""
    number = normalize_phone(phone_raw)
    if not number:
        _log(f"[{slug}] SKIP numero invalido raw={phone_raw!r}")
        return {"ok": False, "error": "numero-invalido", "dryrun": False, "number": None}

    if os.environ.get(config.WA_DRYRUN_ENV) == "1":
        _log(f"[{slug}] DRYRUN -> {number} :: {message!r}")
        return {"ok": True, "error": None, "dryrun": True, "number": number}

    # WhatsApp opcional: sem instancia configurada, no-op gracioso.
    instance = (getattr(config, "EVOLUTION_INSTANCE", "") or "").strip()
    if not instance:
        _log(f"[{slug}] SKIP WhatsApp desligado (sem EVOLUTION_INSTANCE)")
        return {"ok": False, "error": "wa-desligado", "dryrun": False, "number": number}

    key = _load_evolution_key()
    if not key:
        _log(f"[{slug}] FAIL sem apikey Evolution")
        return {"ok": False, "error": "sem-apikey", "dryrun": False, "number": number}

    endpoint = f"{config.EVOLUTION_URL.rstrip('/')}/message/sendText/{config.EVOLUTION_INSTANCE}"
    try:
        resp = requests.post(
            endpoint,
            json={"number": number, "text": message},
            headers={"apikey": key, "Content-Type": "application/json"},
            timeout=30,
        )
    except requests.RequestException as e:
        _log(f"[{slug}] FAIL rede -> {number}: {e}")
        return {"ok": False, "error": f"network: {e}", "dryrun": False, "number": number}

    if 200 <= resp.status_code < 300:
        _log(f"[{slug}] OK -> {number} status={resp.status_code}")
        return {"ok": True, "error": None, "dryrun": False, "number": number}

    _log(f"[{slug}] FAIL http {resp.status_code} -> {number}: {resp.text[:200]}")
    return {"ok": False, "error": f"http {resp.status_code}: {resp.text[:200]}",
            "dryrun": False, "number": number}
