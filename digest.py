#!/usr/bin/env python3
"""
Resumo matinal do Agendador (OPCIONAL).
Roda todo dia de manha (cron) e envia ao dono, via OUTBOX opcional (NUNCA API do
Telegram direto), a lista dos agendamentos DO DIA (horario, nome do cliente,
parceiro ?p=, link do Meet se houver).

So envia se config.OUTBOX_DIR estiver configurado. Sem OUTBOX_DIR, o resumo e
apenas impresso no console (util com --dry-run).

Se nao houver nenhum agendamento, manda "sem agendamentos hoje".

Idempotente: grava um marcador data/digest_sent_YYYY-MM-DD.flag; se ja existe,
nao reenvia (protege contra rodar 2x no mesmo dia). Use --force pra ignorar o
marcador, e --dry-run pra so imprimir o texto sem escrever no outbox nem marcar.

Fuso do "dia" e da exibicao dos horarios: config.TIMEZONE.
"""
import os
import sys
import json
import time
import sqlite3
import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

# Fuso do "hoje" e da exibicao dos horarios no resumo.
DIGEST_TZ = ZoneInfo(config.TIMEZONE)


def db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def build_message(today=None):
    """Monta o texto do resumo dos agendamentos de HOJE (DIGEST_TZ).
    Retorna (texto, n_agendamentos)."""
    if today is None:
        today = datetime.datetime.now(DIGEST_TZ).date()

    conn = db()
    rows = conn.execute(
        "SELECT * FROM bookings WHERE status='confirmed' AND is_test=0 "
        "ORDER BY slot_start ASC"
    ).fetchall()
    conn.close()

    todays = []
    for r in rows:
        try:
            st = datetime.datetime.fromisoformat(r["slot_start"])
        except Exception:
            continue
        if st.tzinfo is None:
            st = st.replace(tzinfo=ZoneInfo(config.TIMEZONE))
        st_local = st.astimezone(DIGEST_TZ)
        if st_local.date() == today:
            todays.append((st_local, r))

    data_str = today.strftime("%d/%m/%Y")
    if not todays:
        return (f"Agendador — resumo de {data_str}: sem agendamentos hoje.", 0)

    linhas = [f"Agendador — agendamentos de hoje ({data_str}):", ""]
    for st_local, r in todays:
        hora = st_local.strftime("%H:%M")
        nome = r["name"] or "(sem nome)"
        parceiro = (r["partner"] or "").strip() or "direto"
        item = f"• {hora} — {nome} (parceiro: {parceiro})"
        if r["meet_link"]:
            item += f"\n   Meet: {r['meet_link']}"
        linhas.append(item)
    return ("\n".join(linhas), len(todays))


def write_outbox(text):
    """Escreve JSON no outbox (integracao Telegram opcional). chat_id vem da config."""
    os.makedirs(config.OUTBOX_DIR, exist_ok=True)
    mid = f"agendar-digest-{int(time.time()*1000)}"
    payload = {"chat_id": config.NOTIFY_TELEGRAM_CHAT_ID, "text": text}
    path = os.path.join(config.OUTBOX_DIR, f"{mid}.json")
    with open(path, "w") as f:
        json.dump(payload, f, ensure_ascii=False)
    return path


def main():
    force = "--force" in sys.argv
    dry = "--dry-run" in sys.argv

    today = datetime.datetime.now(DIGEST_TZ).date()
    text, n = build_message(today)

    if dry:
        print("=== DRY-RUN (nao escreve outbox, nao marca) ===")
        print(f"chat_id: {config.NOTIFY_TELEGRAM_CHAT_ID}")
        print(f"agendamentos hoje: {n}")
        print("---")
        print(text)
        return 0

    # OUTBOX opcional: sem OUTBOX_DIR configurado, so imprime (nada a enviar).
    if not getattr(config, "OUTBOX_DIR", ""):
        print("[digest] OUTBOX_DIR nao configurado (integracao Telegram desligada). "
              "Resumo abaixo:")
        print(text)
        return 0

    flag = os.path.join(config.DATA_DIR, f"digest_sent_{today.isoformat()}.flag")
    if os.path.exists(flag) and not force:
        print(f"[digest] ja enviado hoje ({today}); nada a fazer (use --force pra reenviar).")
        return 0

    path = write_outbox(text)
    with open(flag, "w") as f:
        f.write(datetime.datetime.now(DIGEST_TZ).isoformat())
    # limpeza leve: remove flags de dias anteriores pra nao acumular
    try:
        for fn in os.listdir(config.DATA_DIR):
            if fn.startswith("digest_sent_") and fn.endswith(".flag") \
               and fn != os.path.basename(flag):
                os.remove(os.path.join(config.DATA_DIR, fn))
    except Exception:
        pass
    print(f"[digest] enviado ({n} agendamento(s)) -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
