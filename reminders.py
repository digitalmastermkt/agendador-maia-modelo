#!/usr/bin/env python3
"""
Lembretes WhatsApp do Agendador (OPCIONAL — depende do WhatsApp configurado).
Varre a tabela bookings, calcula quais agendamentos estao dentro de cada janela
de lembrete (1 dia antes, 1h antes, 15 min antes) e ainda nao receberam aquele
lembrete, envia via Evolution API e MARCA no banco (reminded_1d/1h/15m).

Cada lembrete vai para:
  - o CLIENTE (todos os telefones informados no agendamento; coluna 'phone' e
    comma-separated) usando a mensagem config.msg_lembrete;
  - a EQUIPE (config.TEAM_NOTIFY_WHATSAPP, se configurada) usando a mensagem
    curta config.msg_lembrete_equipe.
Tudo via wa.send_text. Falha por numero e graciosa: um numero que falhar nao
trava os outros nem impede a marcacao. Sem WhatsApp configurado, wa.send_text
vira no-op e os lembretes sao apenas marcados (nao enviados).

LINK DO MEET:
  - so a janela '15m' reenvia o meet_link (cliente e equipe);
  - '1d' e '1h' NAO enviam o link.

Idempotencia: a coluna reminded_<janela> so e marcada=1 DEPOIS de tentar enviar
a todos os destinatarios daquela janela, garantindo nao-duplicacao no proximo
tick do cron. Roda via cron a cada 5 min.

Fuso: usa config.TIMEZONE (America/Sao_Paulo), o mesmo do app.

DRY-RUN: export AGENDAR_WA_DRYRUN=1 pra nao enviar de verdade. Em dry-run NAO
marca as colunas, pra poder testar de novo.
"""
import os
import sys
import sqlite3
import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import wa
import active_client_hook

TZ = ZoneInfo(config.TIMEZONE)

# janela: (nome_coluna, minutos_antes, tolerancia_minutos)
# tolerancia >= intervalo do cron (5min) pra nao perder a janela.
WINDOWS = [
    ("reminded_1d", 24 * 60, 6),   # ~1 dia antes
    ("reminded_1h", 60, 6),        # ~1h antes
    ("reminded_15m", 15, 6),       # ~15 min antes
]

JANELA_TAG = {"reminded_1d": "1d", "reminded_1h": "1h", "reminded_15m": "15m"}


def db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def migrate(conn):
    """Migracao idempotente das colunas de idempotencia dos lembretes.
    - Adiciona reminded_1h / reminded_15m se faltarem (sem apagar nada).
    - Backfill anti-duplicacao para agendamentos ja existentes: quem ja recebeu o
      antigo reminded_3h (>= ~1h antes ja era coberto) tem reminded_1h pre-setado;
      quem ja recebeu reminded_30m tem reminded_15m pre-setado. Assim um booking
      em andamento nao leva um 'ja ja comeca' duplicado ao virar a regra nova.
      As colunas antigas reminded_3h/reminded_30m ficam orfas (ok, nao usadas).
    """
    have = {r[1] for r in conn.execute("PRAGMA table_info(bookings)").fetchall()}
    added = []
    for col in ("reminded_1h", "reminded_15m"):
        if col not in have:
            conn.execute(f"ALTER TABLE bookings ADD COLUMN {col} INTEGER DEFAULT 0")
            added.append(col)
    if added:
        conn.commit()
        # backfill so nas colunas recem-criadas, e so se a coluna antiga existir.
        have2 = {r[1] for r in conn.execute("PRAGMA table_info(bookings)").fetchall()}
        if "reminded_1h" in added and "reminded_3h" in have2:
            conn.execute(
                "UPDATE bookings SET reminded_1h=1 "
                "WHERE reminded_3h=1 AND (reminded_1h IS NULL OR reminded_1h=0)")
        if "reminded_15m" in added and "reminded_30m" in have2:
            conn.execute(
                "UPDATE bookings SET reminded_15m=1 "
                "WHERE reminded_30m=1 AND (reminded_15m IS NULL OR reminded_15m=0)")
        conn.commit()
        print(f"[reminders] migracao: colunas adicionadas {added} (backfill aplicado)")


def parse_phones(raw):
    """A coluna 'phone' guarda os telefones do cliente separados por virgula.
    Devolve lista normalizada (55DDDNUMERO), sem duplicatas e sem invalidos."""
    out, seen = [], set()
    for part in (raw or "").split(","):
        n = wa.normalize_phone(part)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def run():
    dryrun = os.environ.get(config.WA_DRYRUN_ENV) == "1"
    now = datetime.datetime.now(TZ)
    conn = db()
    migrate(conn)

    team = getattr(config, "TEAM_NOTIFY_WHATSAPP", {}) or {}

    rows = conn.execute(
        "SELECT * FROM bookings WHERE status='confirmed' AND is_test=0"
    ).fetchall()

    sent = 0
    for r in rows:
        client_phones = parse_phones(r["phone"])
        try:
            start = datetime.datetime.fromisoformat(r["slot_start"])
        except Exception:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=TZ)
        # ja passou? nada a fazer
        if start <= now:
            continue

        minutes_until = (start - now).total_seconds() / 60.0

        for col, offset, tol in WINDOWS:
            if r[col]:
                continue  # ja enviado
            # dispara quando estamos DENTRO da janela [offset-tol, offset]
            if not (offset - tol <= minutes_until <= offset + 0.01):
                continue

            tag = JANELA_TAG[col]
            when = start.strftime("%d/%m/%Y as %H:%M")
            tok = r["reschedule_token"] if "reschedule_token" in r.keys() else None
            # link do Meet SO na janela de 15 minutos
            meet_for_window = r["meet_link"] if tag == "15m" else None

            attempted = False  # tentamos enviar a pelo menos 1 destinatario?
            any_dryrun = False

            # ---- CLIENTE (todos os telefones) ----
            msg_cli = config.msg_lembrete(r["name"], when, meet_for_window, tag, tok)
            client_delivered = 0
            for ph in client_phones:
                attempted = True
                res = wa.send_text(ph, msg_cli, slug=f"lembrete-{tag}-{r['id']}")
                if res.get("dryrun"):
                    any_dryrun = True
                    print(f"[reminders] DRYRUN {col} booking={r['id']} cliente={res.get('number')}")
                elif res.get("ok"):
                    client_delivered += 1
                    print(f"[reminders] enviado {col} booking={r['id']} cliente -> {res['number']}")
                else:
                    print(f"[reminders] FALHA {col} booking={r['id']} cliente {res.get('number')}: {res.get('error')}")

            # ---- EQUIPE (config.TEAM_NOTIFY_WHATSAPP) ----
            msg_eq = config.msg_lembrete_equipe(r["name"], when, tag, meet_for_window)
            for _label, _num in team.items():
                attempted = True
                res = wa.send_text(_num, msg_eq, slug=f"lembrete-{tag}-equipe-{r['id']}")
                if res.get("dryrun"):
                    any_dryrun = True
                    print(f"[reminders] DRYRUN {col} booking={r['id']} equipe={_label}")
                elif res.get("ok"):
                    print(f"[reminders] enviado {col} booking={r['id']} equipe={_label} -> {res['number']}")
                else:
                    print(f"[reminders] FALHA {col} booking={r['id']} equipe={_label} {res.get('number')}: {res.get('error')}")

            # marca a coluna DEPOIS de tentar todos os destinatarios (nao em dryrun).
            if attempted and not any_dryrun:
                conn.execute(f"UPDATE bookings SET {col}=1 WHERE id=?", (r["id"],))
                conn.commit()
                sent += 1
                # hook opcional de atendimento (no-op no pacote padrao).
                if client_delivered:
                    active_client_hook.mark_apresentacao(
                        client_phones[0],
                        detalhe=(f"{config.EVENT_TITLE} marcada pra {when} "
                                 f"(lembrete {tag})."))
            elif any_dryrun:
                print(f"[reminders] DRYRUN {col} booking={r['id']} (nao marca)")
            elif not attempted:
                print(f"[reminders] {col} booking={r['id']} sem destinatarios (marca pra nao repetir)")
                conn.execute(f"UPDATE bookings SET {col}=1 WHERE id=?", (r["id"],))
                conn.commit()
    conn.close()
    if sent == 0 and not dryrun:
        # silencioso em operacao normal (cron)
        pass
    return sent


if __name__ == "__main__":
    try:
        n = run()
        sys.exit(0)
    except Exception as e:
        print(f"[reminders] ERRO: {e}", file=sys.stderr)
        sys.exit(1)
