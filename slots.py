"""
Geracao de slots livres: cruza as janelas de disponibilidade com o freebusy
do Google Agenda e com os agendamentos ja salvos no SQLite.
Tudo em fuso America/Sao_Paulo.
"""
import datetime
from zoneinfo import ZoneInfo

import config
import gcal

TZ = ZoneInfo(config.TIMEZONE)
UTC = ZoneInfo("UTC")


def _overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def generate_slots(booked_local_ranges):
    """Retorna dict {date_str: [ {time, iso_start} ... ]} com slots livres.
    booked_local_ranges: lista de (start_dt, end_dt) tz-aware locais dos
    agendamentos salvos no SQLite (pra bloquear mesmo sem Google)."""
    windows = config.load_windows()
    now = datetime.datetime.now(TZ)
    horizon = now + datetime.timedelta(days=config.BOOKING_HORIZON_DAYS)
    min_start = now + datetime.timedelta(hours=config.MIN_LEAD_HOURS)

    tmin = now.astimezone(UTC).isoformat()
    tmax = horizon.astimezone(UTC).isoformat()

    # Freebusy da agenda do DONO (conta conectada via OAuth). Um compromisso na
    # agenda dele zera o slot — desde que o token tenha o escopo calendar.readonly
    # (ver config.OAUTH_SCOPES). Falha graciosa: sem Google, get_busy devolve [].
    busy_raw = list(gcal.get_busy(tmin, tmax))
    busy = []
    for b in busy_raw:
        try:
            bs = datetime.datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(TZ)
            be = datetime.datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(TZ)
            busy.append((bs, be))
        except Exception:
            continue
    busy.extend(booked_local_ranges)

    out = {}
    dur = datetime.timedelta(minutes=config.EVENT_DURATION_MIN)
    buf = datetime.timedelta(minutes=config.BUFFER_MIN)
    step = config.SLOT_STEP_MIN

    day = now.date()
    for _ in range(config.BOOKING_HORIZON_DAYS + 1):
        wd = day.weekday()
        for win in windows.get(wd, []):
            h1, m1 = map(int, win[0].split(":"))
            h2, m2 = map(int, win[1].split(":"))
            cur = datetime.datetime(day.year, day.month, day.day, h1, m1, tzinfo=TZ)
            win_end = datetime.datetime(day.year, day.month, day.day, h2, m2, tzinfo=TZ)
            while cur + dur <= win_end:
                slot_start = cur
                slot_end = cur + dur
                # bloqueio: passado / antecedencia minima
                if slot_start >= min_start and slot_start <= horizon:
                    blocked = False
                    for bs, be in busy:
                        # inclui buffer apos o slot pra nao colar
                        if _overlaps(slot_start, slot_end + buf, bs, be):
                            blocked = True
                            break
                    if not blocked:
                        ds = slot_start.strftime("%Y-%m-%d")
                        out.setdefault(ds, []).append({
                            "time": slot_start.strftime("%H:%M"),
                            "iso_start": slot_start.isoformat(),
                        })
                cur += datetime.timedelta(minutes=step)
        day += datetime.timedelta(days=1)

    # ordena
    for k in out:
        out[k].sort(key=lambda x: x["time"])
    return dict(sorted(out.items()))


def parse_slot(iso_start):
    """iso_start -> (start_dt, end_dt) tz-aware locais."""
    start = datetime.datetime.fromisoformat(iso_start)
    if start.tzinfo is None:
        start = start.replace(tzinfo=TZ)
    end = start + datetime.timedelta(minutes=config.EVENT_DURATION_MIN)
    return start, end
