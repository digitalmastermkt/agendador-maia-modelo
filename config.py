"""
Configuracao do Agendador (pacote portatil).
As JANELAS de disponibilidade sao editaveis aqui (ou via data/availability.json,
que tem prioridade se existir).

Valores sensiveis (marca, e-mails, telefones, admin, dominio) vem de VARIAVEIS
DE AMBIENTE. Copie .env.example para .env e preencha. Nada de segredo hardcoded.
"""
import os
import json

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "agendar.db")

# ---- Token OAuth (UM perfil: a conta Google do DONO da instancia) ----
# O token e salvo aqui apos o dono conectar a agenda dele em /oauth/start.
TOKEN_PATH = os.path.join(DATA_DIR, "calendar_token.json")

TIMEZONE = os.environ.get("TIMEZONE", "America/Sao_Paulo")

# ---- Marca (lida de env, com defaults genericos) ----
BRAND_NAME = os.environ.get("BRAND_NAME", "Sua Empresa")
BRAND_WHATSAPP = os.environ.get("BRAND_WHATSAPP", "")   # ex: 55DDDNUMERO (so digitos)

# ---- Identidade do evento ----
EVENT_TITLE = os.environ.get("EVENT_TITLE", "Apresentação")
EVENT_DURATION_MIN = int(os.environ.get("EVENT_DURATION_MIN", "40"))  # duracao do slot
BUFFER_MIN = int(os.environ.get("BUFFER_MIN", "20"))     # buffer apos cada evento
SLOT_STEP_MIN = int(os.environ.get("SLOT_STEP_MIN", "60"))  # passo do slot dentro da janela
BOOKING_HORIZON_DAYS = int(os.environ.get("BOOKING_HORIZON_DAYS", "14"))  # dias pra frente
MIN_LEAD_HOURS = int(os.environ.get("MIN_LEAD_HOURS", "3"))  # antecedencia minima

# agenda do dono conectado via OAuth. "primary" = agenda principal da conta.
CALENDAR_ID = os.environ.get("CALENDAR_ID", "primary")

# E-mail do DONO: convidado em TODO evento (fecha a agenda dele tambem).
# Se vazio, nao convida ninguem alem do cliente.
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "").strip()

# ---- Convidados FIXOS da equipe (attendees em TODO agendamento) ----
# Lista de e-mails separados por virgula na env MEET_GUESTS_FIXOS.
# Ex: MEET_GUESTS_FIXOS="fulano@empresa.com,ciclana@empresa.com"
# Deixe vazio pra nao adicionar ninguem alem do cliente (+ OWNER_EMAIL).
MEET_GUESTS_FIXOS = [
    e.strip() for e in os.environ.get("MEET_GUESTS_FIXOS", "").split(",") if e.strip()
]

# ---- Notificacao interna via Telegram (OPCIONAL, desligada por default) ----
# So funciona se voce tiver um bot proprio com um "outbox" (uma pasta onde ele
# le arquivos JSON e envia ao Telegram). Se OUTBOX_DIR estiver vazio, TODA
# notificacao Telegram vira no-op (nao quebra nada).
OUTBOX_DIR = os.environ.get("OUTBOX_DIR", "").strip()  # "" = desligado
try:
    NOTIFY_TELEGRAM_CHAT_ID = int(os.environ.get("NOTIFY_TELEGRAM_CHAT_ID", "0")) or None
except ValueError:
    NOTIFY_TELEGRAM_CHAT_ID = None
CHAT_ID = NOTIFY_TELEGRAM_CHAT_ID  # compat: usado por notify_outbox

# ---- WhatsApp (Evolution API — OPCIONAL) ----
# Se nao configurar, o envio de WhatsApp vira no-op gracioso (o agendamento
# segue normalmente, so sem a mensagem automatica).
EVOLUTION_URL = os.environ.get("EVOLUTION_URL", "http://127.0.0.1:8080")
EVOLUTION_INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "")
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY", "")
WA_DRYRUN_ENV = "AGENDAR_WA_DRYRUN"  # export=1 pra nao enviar de verdade (testes)

# ---- Notificacao interna da EQUIPE por WhatsApp (a cada novo agendamento) ----
# Formato da env TEAM_NOTIFY_WHATSAPP: "Nome:55DDDNUMERO,Outro:55DDDNUMERO".
# Cada novo agendamento dispara, ALEM da confirmacao ao cliente, um aviso interno
# pra estes numeros. Deixe vazio pra desligar a notificacao interna da equipe.
def _parse_team(raw):
    out = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        label, num = item.split(":", 1)
        label, num = label.strip(), num.strip()
        if label and num:
            out[label] = num
    return out

TEAM_NOTIFY_WHATSAPP = _parse_team(os.environ.get("TEAM_NOTIFY_WHATSAPP", ""))

# ---- Admin (login do painel /admin, HTTP Basic) ----
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")  # DEFINA no .env (senha forte)

# Logins adicionais opcionais, com hash werkzeug (pbkdf2:sha256).
# Formato da env ADMIN_USERS: "usuario1:<hash>,usuario2:<hash>".
# Gere um hash com:
#   from werkzeug.security import generate_password_hash
#   generate_password_hash("<senha>", method="pbkdf2:sha256")
def _parse_admin_users(raw):
    out = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        user, h = item.split(":", 1)
        user, h = user.strip(), h.strip()
        if user and h:
            out[user] = h
    return out

ADMIN_USERS = _parse_admin_users(os.environ.get("ADMIN_USERS", ""))

# ---- URL publica (pra montar links de reagendamento nas mensagens) ----
# Ex: https://agendar.seudominio.com
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# ---- OAuth (conta Google do DONO da instancia) ----
OAUTH_REDIRECT = f"{PUBLIC_BASE_URL}/oauth/callback"

# ESCOPOS OAUTH (importante):
#   calendar.events   -> cria/move eventos com Google Meet.
#   calendar.readonly -> le o freebusy da agenda do dono, pra que os compromissos
#                        PESSOAIS dele bloqueiem os horarios automaticamente. SEM
#                        este escopo, a agenda NAO se bloqueia sozinha (so os
#                        agendamentos feitos por este app bloqueiam).
OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]
# OPCIONAL: descomente pra permitir ler as gravacoes do Meet no Drive do dono
# (usado pela busca de gravacao no /admin). Nao faz parte do nucleo de agendamento.
#   "https://www.googleapis.com/auth/drive.readonly",

# ---- JANELAS DE DISPONIBILIDADE (config simples e editavel) ----
# Dia da semana: 0=segunda ... 6=domingo. Horarios em HH:MM (fuso TIMEZONE).
# Cada janela vira varios slots de SLOT_STEP_MIN em SLOT_STEP_MIN.
# Para editar: mude aqui OU crie data/availability.json com o mesmo formato.
DEFAULT_WINDOWS = {
    0: [["09:00", "11:00"], ["14:00", "16:00"]],  # segunda
    1: [["09:00", "11:00"], ["14:00", "16:00"]],  # terca
    2: [["09:00", "11:00"], ["14:00", "16:00"]],  # quarta
    3: [["09:00", "11:00"], ["14:00", "16:00"]],  # quinta
    4: [["09:00", "11:00"]],                       # sexta
    5: [],                                          # sabado
    6: [],                                          # domingo
}


def load_windows():
    """availability.json (se existir) tem prioridade sobre DEFAULT_WINDOWS."""
    override = os.path.join(DATA_DIR, "availability.json")
    if os.path.exists(override):
        try:
            with open(override) as f:
                raw = json.load(f)
            return {int(k): v for k, v in raw.items()}
        except Exception:
            pass
    return DEFAULT_WINDOWS


# ---- Mensagens WhatsApp ao cliente (tom acolhedor; marca = BRAND_NAME) ----
def reschedule_url(token):
    return f"{PUBLIC_BASE_URL.rstrip('/')}/reagendar/{token}"


def msg_confirmacao(name, when_str, meet_link, reschedule_token=None):
    first = (name or "").split()[0].title() if name else ""
    ola = f"Olá, {first}! " if first else "Olá! "
    corpo = (
        f"{ola}Aqui é da *{BRAND_NAME}* \U0001F49A\n\n"
        f"Sua *{EVENT_TITLE}* está confirmada!\n\n"
        f"\U0001F4C5 *Quando:* {when_str}\n"
    )
    if meet_link:
        corpo += f"\U0001F517 *Link da chamada (Google Meet):*\n{meet_link}\n\n"
    else:
        corpo += "\U0001F517 O link da chamada chega em seguida.\n\n"
    corpo += (
        "É só entrar no horário pelo link. Vai ser rápido e direto ao ponto.\n\n"
    )
    if reschedule_token:
        corpo += (f"\U0001F504 *Precisa mudar o horário?* Reagende aqui:\n"
                  f"{reschedule_url(reschedule_token)}\n\n")
    corpo += "Qualquer dúvida, é só responder por aqui. Até lá! ✨"
    return corpo


def msg_reagendado(name, old_when, new_when, meet_link, reschedule_token=None):
    """Aviso ao cliente de que o horário foi movido."""
    first = (name or "").split()[0].title() if name else ""
    ola = f"Olá, {first}! " if first else "Olá! "
    corpo = (
        f"{ola}Aqui é da *{BRAND_NAME}* \U0001F49A\n\n"
        f"Sua *{EVENT_TITLE}* foi *reagendada* com sucesso.\n\n"
        f"❌ Antes: {old_when}\n"
        f"✅ *Agora:* {new_when}\n"
    )
    if meet_link:
        corpo += f"\U0001F517 *Link da chamada (Google Meet):*\n{meet_link}\n\n"
    else:
        corpo += "\U0001F517 O link da chamada segue o mesmo enviado antes.\n\n"
    if reschedule_token:
        corpo += (f"\U0001F504 Precisa mudar de novo? "
                  f"{reschedule_url(reschedule_token)}\n\n")
    corpo += f"Te espero lá! — {BRAND_NAME} \U0001F49A"
    return corpo


def msg_lembrete(name, when_str, meet_link, janela, reschedule_token=None):
    """Lembrete ao CLIENTE. janela in {'1d','1h','15m'}.
    O link do Meet (meet_link) so vai na janela '15m'; nas janelas '1d' e '1h'
    o worker passa meet_link vazio/None de proposito."""
    first = (name or "").split()[0].title() if name else ""
    ola = f"Oi, {first}! " if first else "Oi! "
    heads = {
        "1d": f"Passando pra lembrar: *amanhã* você tem a sua {EVENT_TITLE} \U0001F680",
        "1h": f"Falta *1 hora* pra sua {EVENT_TITLE} \U0001F550",
        "15m": f"Já já começa! Sua {EVENT_TITLE} é em *15 minutos* ⏰",
    }
    corpo = f"{ola}{heads.get(janela, 'Lembrete da sua ' + EVENT_TITLE)}\n\n"
    corpo += f"\U0001F4C5 *Horário:* {when_str}\n"
    if meet_link:
        corpo += f"\U0001F517 *Entre pelo Meet:*\n{meet_link}\n\n"
    else:
        corpo += "\U0001F517 O link da chamada foi enviado na confirmação.\n\n"
    # o lembrete de 15m nao oferece reagendar (tarde demais)
    if reschedule_token and janela != "15m":
        corpo += (f"\U0001F504 Precisa remarcar? {reschedule_url(reschedule_token)}\n\n")
    corpo += f"Te espero lá! — {BRAND_NAME} \U0001F49A"
    return corpo


def msg_lembrete_equipe(name, when_str, janela, meet_link=None):
    """Lembrete interno curto pra equipe. janela in {'1d','1h','15m'}.
    O link do Meet so entra na janela '15m' (quando meet_link e passado)."""
    heads = {
        "1d": f"Lembrete: *amanhã* tem {EVENT_TITLE} \U0001F680",
        "1h": f"Lembrete: falta *1 hora* pra {EVENT_TITLE} \U0001F550",
        "15m": f"Lembrete: {EVENT_TITLE} em *15 minutos* ⏰",
    }
    corpo = f"{heads.get(janela, 'Lembrete de ' + EVENT_TITLE)}\n\n"
    corpo += f"\U0001F464 *Cliente:* {name or '—'}\n"
    corpo += f"\U0001F4C5 *Quando:* {when_str}\n"
    if meet_link:
        corpo += f"\U0001F517 *Meet:* {meet_link}\n"
    return corpo


def msg_cancelado_cliente(name, when_str):
    """Confirmacao curta ao CLIENTE de que o agendamento dele foi cancelado."""
    first = (name or "").split()[0].title() if name else ""
    ola = f"Olá, {first}! " if first else "Olá! "
    corpo = (
        f"{ola}Aqui é da *{BRAND_NAME}* \U0001F49A\n\n"
        f"Sua *{EVENT_TITLE}* de {when_str} foi *cancelada* com sucesso.\n\n"
        "Se mudar de ideia, é só falar com a gente por aqui que remarcamos "
        "num instante. Até breve! \U0001F49A"
    )
    return corpo


def msg_equipe_cancelamento(name, when_str, partner, emails, phones):
    """Aviso interno curto pra equipe quando o CLIENTE cancela o proprio
    agendamento pela pagina publica."""
    emails_str = ", ".join(emails) if emails else "—"
    phones_str = ", ".join(phones) if phones else "—"
    corpo = (
        f"❌ *Agendamento CANCELADO pelo cliente — {EVENT_TITLE}*\n\n"
        f"\U0001F464 *Cliente:* {name}\n"
        f"\U0001F4C5 *Era:* {when_str}\n"
        f"\U0001F91D *Origem/parceiro:* {partner or 'direto'}\n"
        f"\U0001F4E7 *E-mail(s):* {emails_str}\n"
        f"\U0001F4F1 *WhatsApp:* {phones_str}\n"
    )
    return corpo


def msg_equipe_novo_agendamento(name, when_str, partner, emails, phones,
                                meet_link):
    """Aviso interno curto pra equipe a cada novo agendamento.
    emails/phones sao listas (podem ter 1+ itens)."""
    emails_str = ", ".join(emails) if emails else "—"
    phones_str = ", ".join(phones) if phones else "—"
    corpo = (
        f"\U0001F5D3 *Novo agendamento — {EVENT_TITLE}*\n\n"
        f"\U0001F464 *Cliente:* {name}\n"
        f"\U0001F4C5 *Quando:* {when_str}\n"
        f"\U0001F91D *Origem/parceiro:* {partner or 'direto'}\n"
        f"\U0001F4E7 *E-mail(s):* {emails_str}\n"
        f"\U0001F4F1 *WhatsApp:* {phones_str}\n"
    )
    if meet_link:
        corpo += f"\U0001F517 *Meet:* {meet_link}\n"
    return corpo
