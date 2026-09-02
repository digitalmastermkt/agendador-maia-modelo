"""
Camada Google Calendar: freebusy + criar evento com Meet.
Isolada pra falhar graciosamente se o token estiver revogado.
"""
import os
import json
import datetime
import uuid

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

import config


class CalendarUnavailable(Exception):
    """Token ausente/revogado ou erro de auth. Fluxo segue sem Google."""
    pass


def _creds():
    if not os.path.exists(config.TOKEN_PATH):
        raise CalendarUnavailable("token ausente")
    try:
        creds = Credentials.from_authorized_user_file(config.TOKEN_PATH)
    except Exception as e:
        raise CalendarUnavailable(f"token invalido: {e}")
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(config.TOKEN_PATH, "w") as f:
                    f.write(creds.to_json())
            except Exception as e:
                raise CalendarUnavailable(f"refresh falhou: {e}")
        else:
            raise CalendarUnavailable("sem refresh token valido")
    return creds


def _service():
    return build("calendar", "v3", credentials=_creds(), cache_discovery=False)


def calendar_ok():
    """True se o Google Calendar esta acessivel agora."""
    try:
        svc = _service()
        svc.calendars().get(calendarId=config.CALENDAR_ID).execute()
        return True
    except Exception:
        return False


def get_busy(time_min_iso, time_max_iso):
    """Retorna lista de blocos ocupados [{start,end}] em ISO UTC.
    Se o calendario estiver indisponivel, devolve [] (assume tudo livre)."""
    try:
        svc = _service()
        body = {
            "timeMin": time_min_iso,
            "timeMax": time_max_iso,
            "timeZone": config.TIMEZONE,
            "items": [{"id": config.CALENDAR_ID}],
        }
        fb = svc.freebusy().query(body=body).execute()
        return fb["calendars"][config.CALENDAR_ID]["busy"]
    except CalendarUnavailable:
        return []
    except Exception:
        return []


def create_event(start_dt, end_dt, guest_email, guest_name, partner):
    """Cria evento com Meet e convida o(s) parceiro(s).
    guest_email pode ser uma string (1 e-mail) OU uma lista de e-mails —
    TODOS entram como convidados e recebem o convite do Meet.
    Retorna dict {ok, event_id, meet_link, html_link, error}."""
    try:
        svc = _service()
    except CalendarUnavailable as e:
        return {"ok": False, "error": str(e), "meet_link": None,
                "event_id": None, "html_link": None}

    # normaliza pra lista de e-mails (dedup preservando ordem, minusculas)
    if isinstance(guest_email, (list, tuple, set)):
        raw_emails = list(guest_email)
    else:
        raw_emails = [guest_email]
    guest_emails = []
    seen = set()
    for e in raw_emails:
        e = (e or "").strip().lower()
        if e and e not in seen:
            seen.add(e)
            guest_emails.append(e)
    primary_email = guest_emails[0] if guest_emails else ""

    emails_desc = ", ".join(guest_emails) if guest_emails else primary_email
    desc = (f"{config.EVENT_TITLE}.\n"
            f"Agendado por: {guest_name} ({emails_desc})\n"
            f"Origem/parceiro: {partner or 'direto'}")
    # convidados: TODOS os e-mails informados + o DONO (fecha a agenda dele).
    # O organizador e a conta conectada via OAuth (o dono da instancia).
    attendees = []
    for i, e in enumerate(guest_emails):
        attendees.append({"email": e,
                          "displayName": guest_name if i == 0 else e})
    owner = getattr(config, "OWNER_EMAIL", "").strip()
    if owner and owner.lower() not in seen:
        seen.add(owner.lower())
        attendees.append({"email": owner})
    # convidados FIXOS da equipe: entram em TODO evento (dedup pelo 'seen').
    # Assim eles recebem o convite e conseguem abrir o Meet direto.
    for fg in getattr(config, "MEET_GUESTS_FIXOS", []):
        fg = (fg or "").strip()
        if fg and fg.lower() not in seen:
            seen.add(fg.lower())
            attendees.append({"email": fg})
    # Titulo com o NOME do cliente pra identificar a gravacao do Meet.
    # Ex: "Apresentacao — Fulano".
    _summary = f"{config.EVENT_TITLE} — {guest_name}" if guest_name else config.EVENT_TITLE
    body = {
        "summary": _summary,
        "description": desc,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": config.TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": config.TIMEZONE},
        "attendees": attendees,
        "conferenceData": {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "reminders": {"useDefault": True},
    }
    try:
        ev = svc.events().insert(
            calendarId=config.CALENDAR_ID,
            body=body,
            conferenceDataVersion=1,
            sendUpdates="all",
        ).execute()
        meet = None
        for ep in ev.get("conferenceData", {}).get("entryPoints", []):
            if ep.get("entryPointType") == "video":
                meet = ep.get("uri")
                break
        return {"ok": True, "event_id": ev.get("id"),
                "meet_link": meet, "html_link": ev.get("htmlLink"),
                "error": None}
    except Exception as e:
        return {"ok": False, "error": str(e), "meet_link": None,
                "event_id": None, "html_link": None}


def move_event(event_id, start_dt, end_dt):
    """Move (reagenda) um evento existente pra novo horario.
    Retorna dict {ok, event_id, meet_link, html_link, error}.
    Se o calendario estiver indisponivel, devolve ok=False com error legivel
    (o fluxo de reagendamento segue mesmo assim, so no banco)."""
    try:
        svc = _service()
    except CalendarUnavailable as e:
        return {"ok": False, "error": str(e), "meet_link": None,
                "event_id": event_id, "html_link": None}
    try:
        patch = {
            "start": {"dateTime": start_dt.isoformat(), "timeZone": config.TIMEZONE},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": config.TIMEZONE},
        }
        ev = svc.events().patch(
            calendarId=config.CALENDAR_ID,
            eventId=event_id,
            body=patch,
            sendUpdates="all",
        ).execute()
        meet = None
        for ep in ev.get("conferenceData", {}).get("entryPoints", []):
            if ep.get("entryPointType") == "video":
                meet = ep.get("uri")
                break
        return {"ok": True, "event_id": ev.get("id"),
                "meet_link": meet, "html_link": ev.get("htmlLink"),
                "error": None}
    except Exception as e:
        return {"ok": False, "error": str(e), "meet_link": None,
                "event_id": event_id, "html_link": None}


def delete_event(event_id):
    try:
        svc = _service()
        svc.events().delete(calendarId=config.CALENDAR_ID,
                            eventId=event_id, sendUpdates="all").execute()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- Drive (gravacoes Meet)
def _drive_service():
    return build("drive", "v3", credentials=_creds(), cache_discovery=False)


def find_recording(query_terms=None, after_iso=None):
    """Procura uma gravacao do Meet no Drive do organizador (dono da instancia), pasta
    'Meet Recordings'. As gravacoes sao mp4 nomeados com o titulo da reuniao
    + data/hora. Retorna dict {ok, files:[{id,name,webViewLink,createdTime}], error}.

    query_terms: lista de strings pra casar no nome do arquivo (ex: titulo do
                 evento, nome do cliente). Opcional.
    after_iso:   ISO8601 (RFC3339); so retorna arquivos criados depois disso.
                 Util pra pegar a gravacao logo apos a reuniao. Opcional.

    Precisa do scope drive.readonly no token. Falha graciosa se indisponivel.
    """
    try:
        svc = _drive_service()
    except CalendarUnavailable as e:
        return {"ok": False, "files": [], "error": str(e)}
    try:
        # 1) acha a pasta 'Meet Recordings'
        folder_q = ("name = 'Meet Recordings' and "
                    "mimeType = 'application/vnd.google-apps.folder' and trashed = false")
        fr = svc.files().list(q=folder_q, spaces="drive",
                              fields="files(id,name)", pageSize=5).execute()
        folders = fr.get("files", [])
        # 2) monta a query dos videos
        q_parts = ["mimeType = 'video/mp4'", "trashed = false"]
        if folders:
            parents = " or ".join(f"'{f['id']}' in parents" for f in folders)
            q_parts.append(f"({parents})")
        if after_iso:
            q_parts.append(f"createdTime > '{after_iso}'")
        for t in (query_terms or []):
            t = str(t).replace("'", " ").strip()
            if t:
                q_parts.append(f"name contains '{t}'")
        q = " and ".join(q_parts)
        res = svc.files().list(
            q=q, spaces="drive", orderBy="createdTime desc", pageSize=10,
            fields="files(id,name,webViewLink,createdTime)").execute()
        return {"ok": True, "files": res.get("files", []), "error": None}
    except Exception as e:
        return {"ok": False, "files": [], "error": str(e)}
