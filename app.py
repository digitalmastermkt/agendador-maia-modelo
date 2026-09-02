"""
Agendador AnB — app Flask enxuto (SQLite, footprint minimo).
Parceiros auto-agendam eventos (com Google Meet) na agenda do dono da instancia.
"""
import os
import re
import json
import time
import sqlite3
import datetime
import secrets
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Flask, request, jsonify, redirect, render_template_string,
                   Response, url_for)

import config
import gcal
import slots as slots_mod
import wa
import active_client_hook

app = Flask(__name__, static_folder="static", static_url_path="/static")

# Atras do Caddy (que termina o TLS externo em :443 e faz reverse_proxy pro
# Flask em http://127.0.0.1:5120). O Caddy repassa X-Forwarded-Proto refletindo
# o esquema real da conexao externa (https). ProxyFix faz o Flask honrar esses
# headers, entao request.url volta a ser https:// — essencial pro OAuth do Google
# (oauthlib recusa authorization_response que nao seja https).
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Garante que os logs INFO do OAuth (diagnostico do fetch_token) apareçam no
# logs/app.log — o Werkzeug dev server por padrao nao emitiria INFO do app.logger.
import logging as _logging
_logging.basicConfig(level=_logging.INFO)
app.logger.setLevel(_logging.INFO)

TZ = ZoneInfo(config.TIMEZONE)

# ---------------------------------------------------------------- DB
def db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            partner TEXT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            slot_start TEXT NOT NULL,
            slot_end TEXT NOT NULL,
            event_id TEXT,
            meet_link TEXT,
            calendar_ok INTEGER DEFAULT 0,
            is_test INTEGER DEFAULT 0,
            status TEXT DEFAULT 'confirmed',
            confirm_sent INTEGER DEFAULT 0,
            reminded_1d INTEGER DEFAULT 0,
            reminded_1h INTEGER DEFAULT 0,
            reminded_15m INTEGER DEFAULT 0
        )
    """)
    # migracao idempotente: colunas de lembrete pra bancos ja existentes.
    # janelas atuais: 1d / 1h / 15m. reminded_3h/reminded_30m (antigas) podem
    # existir como colunas orfas em bancos legados; nao removemos (SQLite nao
    # dropa coluna facil e nao ha risco em manter). O worker so usa 1d/1h/15m.
    have = {r[1] for r in conn.execute("PRAGMA table_info(bookings)").fetchall()}
    for col in ("confirm_sent", "reminded_1d", "reminded_1h", "reminded_15m"):
        if col not in have:
            conn.execute(f"ALTER TABLE bookings ADD COLUMN {col} INTEGER DEFAULT 0")
    # coluna do token de reagendamento (link unico por agendamento)
    if "reschedule_token" not in have:
        conn.execute("ALTER TABLE bookings ADD COLUMN reschedule_token TEXT")
    # coluna do link da gravacao do Meet (Drive do organizador/dono)
    if "recording_link" not in have:
        conn.execute("ALTER TABLE bookings ADD COLUMN recording_link TEXT")
    conn.commit()
    # backfill: garante token pra agendamentos ativos antigos
    for r in conn.execute(
        "SELECT id FROM bookings WHERE (reschedule_token IS NULL OR reschedule_token='') "
        "AND status='confirmed'").fetchall():
        conn.execute("UPDATE bookings SET reschedule_token=? WHERE id=?",
                     (secrets.token_urlsafe(24), r[0]))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- helpers
def booked_ranges(exclude_id=None):
    """Ranges locais dos agendamentos ativos (pra bloquear slots).
    exclude_id: ignora esse agendamento (usado no reagendamento, pra o proprio
    horario nao bloquear a si mesmo)."""
    conn = db()
    if exclude_id is not None:
        rows = conn.execute(
            "SELECT slot_start, slot_end FROM bookings WHERE status='confirmed' AND id<>?",
            (exclude_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT slot_start, slot_end FROM bookings WHERE status='confirmed'"
        ).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            s = datetime.datetime.fromisoformat(r["slot_start"])
            e = datetime.datetime.fromisoformat(r["slot_end"])
            out.append((s, e))
        except Exception:
            continue
    return out


def notify_outbox(text):
    """Notificacao Telegram OPCIONAL via 'outbox' (pasta onde um bot proprio le
    arquivos JSON e envia). Desligada por default: se config.OUTBOX_DIR estiver
    vazio, vira no-op. Nunca chama a API do Telegram direto."""
    if not getattr(config, "OUTBOX_DIR", ""):
        return
    try:
        os.makedirs(config.OUTBOX_DIR, exist_ok=True)
        mid = f"agendar-{int(time.time()*1000)}"
        payload = {"chat_id": config.CHAT_ID, "text": text}
        path = os.path.join(config.OUTBOX_DIR, f"{mid}.json")
        with open(path, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def check_auth(u, p):
    if not u:
        return False
    # login principal do admin (ADMIN_USER/ADMIN_PASS via env)
    if config.ADMIN_PASS and u == config.ADMIN_USER and p == config.ADMIN_PASS:
        return True
    # logins adicionais com hash (config.ADMIN_USERS)
    from werkzeug.security import check_password_hash
    h = getattr(config, "ADMIN_USERS", {}).get(u)
    if h:
        try:
            return check_password_hash(h, p or "")
        except Exception:
            return False
    return False


def require_admin(f):
    @wraps(f)
    def wrap(*a, **k):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response("Acesso restrito", 401,
                            {"WWW-Authenticate": 'Basic realm="Agendar AnB"'})
        return f(*a, **k)
    return wrap


# ---------------------------------------------------------------- OAuth
_oauth_states = {}


@app.route("/oauth/start")
@require_admin
def oauth_start():
    from google_auth_oauthlib.flow import Flow
    # UM unico perfil OAuth: a conta Google do DONO da instancia.
    scopes, token_path, label = config.OAUTH_SCOPES, config.TOKEN_PATH, "owner"
    env = _load_env()
    client_config = {
        "web": {
            "client_id": env["GOOGLE_CALENDAR_CLIENT_ID"],
            "client_secret": env["GOOGLE_CALENDAR_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [config.OAUTH_REDIRECT],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=scopes,
                                   redirect_uri=config.OAUTH_REDIRECT)
    # redirect_uri EXPLICITO tambem no session (defesa em profundidade: tem que
    # ser byte-a-byte igual ao usado na troca do token no callback).
    flow.redirect_uri = config.OAUTH_REDIRECT
    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent")
    # CAUSA-RAIZ do invalid_grant anterior: o google-auth-oauthlib gera um
    # code_verifier PKCE por instancia de Flow e envia code_challenge=SHA256(cv)
    # no authorization_url. Se o callback criar um Flow NOVO, ele gera OUTRO
    # code_verifier e a troca do token falha a verificacao PKCE do Google
    # (invalid_grant). Precisamos PERSISTIR o code_verifier desta etapa e
    # restaura-lo no callback, associado ao state. Persistimos tambem 'who' pra
    # o callback salvar no token certo e usar o mesmo conjunto de scopes.
    _oauth_states[state] = {"code_verifier": flow.code_verifier,
                            "who": label, "token_path": token_path}
    app.logger.info("[oauth/start] who=%s redirect_uri=%s state=%s pkce=%s",
                    label, flow.redirect_uri, state, bool(flow.code_verifier))
    return redirect(auth_url)


@app.route("/oauth/callback")
def oauth_callback():
    from google_auth_oauthlib.flow import Flow
    state = request.args.get("state", "")
    st = _oauth_states.get(state)
    # UM unico perfil OAuth: a conta Google do DONO da instancia.
    scopes = config.OAUTH_SCOPES
    token_path = st.get("token_path") if isinstance(st, dict) else config.TOKEN_PATH
    token_path = token_path or config.TOKEN_PATH
    who_label = "owner"
    env = _load_env()
    client_config = {
        "web": {
            "client_id": env["GOOGLE_CALENDAR_CLIENT_ID"],
            "client_secret": env["GOOGLE_CALENDAR_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [config.OAUTH_REDIRECT],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=scopes,
                                   redirect_uri=config.OAUTH_REDIRECT, state=state)
    # redirect_uri EXPLICITO (identico byte-a-byte ao usado no /oauth/start).
    flow.redirect_uri = config.OAUTH_REDIRECT

    # PKCE: restaura o code_verifier gerado no /oauth/start pra este mesmo state.
    # Sem isso, o Flow do callback geraria um code_verifier NOVO e a verificacao
    # PKCE do Google falharia (invalid_grant). Desliga a auto-geracao pra garantir
    # que o valor restaurado seja o usado. (st ja foi lido acima pro perfil.)
    if isinstance(st, dict) and st.get("code_verifier"):
        flow.code_verifier = st["code_verifier"]
    flow.autogenerate_code_verifier = False

    # authorization_response = request.url DIRETO. Com o ProxyFix ativo, request.url
    # ja vem https (Caddy repassa X-Forwarded-Proto). Passar a URL crua preserva o
    # authorization code EXATAMENTE como o Google mandou (o code tem '/' e chars
    # sensiveis; reconstruir a mao arriscava corromper). OAUTHLIB_INSECURE_TRANSPORT=1
    # no ambiente ja tolera qualquer residuo de http.
    auth_response = request.url
    if auth_response.startswith("http://"):
        auth_response = "https://" + auth_response[len("http://"):]
    app.logger.info("[oauth/callback] redirect_uri=%s state=%s pkce_restored=%s "
                    "auth_response_scheme=%s",
                    flow.redirect_uri, state,
                    bool(isinstance(st, dict) and st.get("code_verifier")),
                    auth_response.split("://", 1)[0])

    # Compliance hook: loga o corpo CRU da resposta do token endpoint do Google
    # ANTES do oauthlib parsear/levantar. E aqui que vem o error_description real.
    def _log_token_response(resp):
        try:
            app.logger.info("[oauth/callback] token endpoint HTTP %s body=%s",
                            resp.status_code, resp.text[:1000])
        except Exception:
            pass
        return resp
    try:
        flow.oauth2session.register_compliance_hook(
            "access_token_response", _log_token_response)
    except Exception:
        pass

    try:
        flow.fetch_token(authorization_response=auth_response)
    except Exception as e:
        # Captura o CORPO exato da resposta do endpoint de token do Google
        # (error_description: redirect_uri_mismatch / invalid code / etc).
        body = None
        for attr in ("description", "response"):
            v = getattr(e, attr, None)
            if v:
                body = str(v)
                break
        app.logger.error("[oauth/callback] fetch_token FALHOU: %s | detalhe=%s",
                         e, body)
        raise
    # sucesso: descarta o state (uso unico)
    _oauth_states.pop(state, None)
    creds = flow.credentials
    # salva o token da conta do DONO (TOKEN_PATH em data/calendar_token.json)
    with open(token_path, "w") as f:
        f.write(creds.to_json())
    app.logger.info("[oauth/callback] token salvo who=%s path=%s", who_label, token_path)
    notify_outbox("Google Agenda conectado no Agendador. "
                  "Criacao de eventos com Meet ativa.")
    return ("<h2 style='font-family:sans-serif'>Google Agenda conectado com sucesso.</h2>"
            "<p style='font-family:sans-serif'>Pode fechar esta aba. "
            "O agendador ja cria eventos com Meet.</p>")


def _load_env():
    """Le os segredos OAuth do Google. Prioridade: variaveis de ambiente do
    processo (recomendado — via .env carregado pelo start.sh). GOOGLE_CALENDAR_
    CLIENT_ID e GOOGLE_CALENDAR_CLIENT_SECRET sao obrigatorios pro fluxo OAuth."""
    env = {}
    for k in ("GOOGLE_CALENDAR_CLIENT_ID", "GOOGLE_CALENDAR_CLIENT_SECRET"):
        v = os.environ.get(k)
        if v:
            env[k] = v.strip().strip('"').strip("'")
    return env


# ---------------------------------------------------------------- public API
@app.route("/api/slots")
def api_slots():
    data = slots_mod.generate_slots(booked_ranges())
    return jsonify({"days": data, "duration": config.EVENT_DURATION_MIN})


def _clean_email_list(d):
    """Junta d['emails'] (lista, novo) + d['email'] (str, compat).
    Dedup preservando ordem, minusculas. So mantem e-mails com formato valido."""
    raw = []
    lst = d.get("emails")
    if isinstance(lst, (list, tuple)):
        raw.extend(lst)
    if d.get("email"):
        raw.append(d.get("email"))
    out, seen = [], set()
    for e in raw:
        e = (str(e) or "").strip().lower()
        if e and EMAIL_RE.match(e) and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def _clean_phone_list(d):
    """Junta d['phones'] (lista, novo) + d['phone'] (str, compat).
    Normaliza pelo mesmo normalizador do WhatsApp; descarta invalidos; dedup."""
    raw = []
    lst = d.get("phones")
    if isinstance(lst, (list, tuple)):
        raw.extend(lst)
    if d.get("phone"):
        raw.append(d.get("phone"))
    out, seen = [], set()
    for p in raw:
        norm = wa.normalize_phone(p)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out




# --- Funil de campanhas (OPCIONAL) -------------------------------------------
# Se voce integra um painel de campanhas/CRM proprio, um agendamento cujo
# telefone seja lead pode ser marcado como "qualificado" via webhook. Best-effort:
# painel fora do ar nao afeta o agendamento. Desligado por default (sem env).
PAINEL_CAMPANHAS_URL = os.environ.get("PAINEL_CAMPANHAS_URL", "")
PAINEL_CAMPANHAS_TOKEN = os.environ.get("PAINEL_CAMPANHAS_TOKEN", "")


def _funil_marcar_qualificado(phones, when):
    if not (PAINEL_CAMPANHAS_URL and PAINEL_CAMPANHAS_TOKEN and phones):
        return
    import json as _json
    import urllib.request as _rq
    for _tel in phones:
        try:
            req = _rq.Request(
                f"{PAINEL_CAMPANHAS_URL}/webhooks/campanhas/estagio", method="POST",
                data=_json.dumps({"telefone": _tel, "estagio": "qualificado",
                                  "nota": f"Reuniao agendada: {when}"}).encode(),
                headers={"Authorization": f"Bearer {PAINEL_CAMPANHAS_TOKEN}",
                         "Content-Type": "application/json"})
            with _rq.urlopen(req, timeout=6) as resp:
                _json.loads(resp.read() or b"{}")
            app.logger.info(f"[FUNIL] {_tel} -> qualificado ({when})")
            return                       # um telefone casou, missao cumprida
        except Exception as exc:
            app.logger.info(f"[FUNIL] {_tel} nao casou/painel off: {exc}")

@app.route("/api/book", methods=["POST"])
def api_book():
    d = request.get_json(force=True, silent=True) or {}
    name = (d.get("name") or "").strip()
    emails = _clean_email_list(d)
    phones = _clean_phone_list(d)
    email = emails[0] if emails else ""        # compat: 1o e-mail (coluna legada)
    phone = phones[0] if phones else ""         # compat: 1o telefone (coluna legada)
    partner = (d.get("partner") or "").strip()[:60]
    iso_start = (d.get("iso_start") or "").strip()
    is_test = 1 if d.get("test") else 0

    if not name or not emails or not phones or not iso_start:
        return jsonify({"ok": False, "error": "Preencha nome, e-mail, WhatsApp e horario."}), 400

    try:
        start, end = slots_mod.parse_slot(iso_start)
    except Exception:
        return jsonify({"ok": False, "error": "Horario invalido."}), 400

    # re-checa se ainda esta livre (evita corrida)
    now = datetime.datetime.now(TZ)
    if start < now + datetime.timedelta(hours=config.MIN_LEAD_HOURS):
        return jsonify({"ok": False, "error": "Horario indisponivel, escolha outro."}), 409
    for bs, be in booked_ranges():
        if start < be and bs < end:
            return jsonify({"ok": False, "error": "Horario acabou de ser reservado. Escolha outro."}), 409

    # cria evento no Google (se disponivel) — TODOS os e-mails viram convidados
    ev = {"ok": False, "event_id": None, "meet_link": None, "error": "not-attempted"}
    if not is_test:
        ev = gcal.create_event(start, end, emails, name, partner)

    # guarda as listas completas nas colunas legadas (comma-separated), mantendo
    # retrocompat: quem tinha 1 e-mail/1 telefone continua com 1 valor la.
    email_store = ", ".join(emails)
    phone_store = ", ".join(phones)

    token = secrets.token_urlsafe(24)
    conn = db()
    cur = conn.execute("""
        INSERT INTO bookings
        (created_at, partner, name, email, phone, slot_start, slot_end,
         event_id, meet_link, calendar_ok, is_test, status, reschedule_token)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (now.isoformat(), partner, name, email_store, phone_store,
          start.isoformat(), end.isoformat(),
          ev.get("event_id"), ev.get("meet_link"),
          1 if ev.get("ok") else 0, is_test, "confirmed", token))
    conn.commit()
    bid = cur.lastrowid
    conn.close()

    when = start.strftime("%d/%m/%Y as %H:%M")
    if not is_test:
        cal_note = "" if ev.get("ok") else " (evento no Google pendente: reconectar Agenda)"
        # confirmacao ao CLIENTE via WhatsApp — TODOS os telefones informados.
        # (nao quebra o fluxo se algum falhar)
        wa_note = ""
        sent_ok = 0
        for ph in phones:
            res = wa.send_text(ph,
                               config.msg_confirmacao(name, when, ev.get("meet_link"), token),
                               slug=f"confirm-{bid}")
            if res.get("ok"):
                if res.get("dryrun"):
                    sent_ok += 1
                else:
                    sent_ok += 1
                    # hook opcional de atendimento (no-op no pacote padrao).
                    active_client_hook.mark_apresentacao(
                        ph,
                        detalhe=f"{config.EVENT_TITLE} marcada pra {when} (confirmacao).")
        if phones and sent_ok:
            conn2 = db()
            conn2.execute("UPDATE bookings SET confirm_sent=1 WHERE id=?", (bid,))
            conn2.commit()
            conn2.close()
            wa_note = (f" (confirmacao WhatsApp enviada a {sent_ok} de "
                       f"{len(phones)} numero(s))")
        elif phones:
            wa_note = " (confirmacao WhatsApp NAO enviada a nenhum numero)"

        # notificacao interna via Telegram (opcional — ver notify_outbox)
        notify_outbox(
            f"Novo agendamento: parceiro '{partner or 'direto'}' — "
            f"{name} ({email_store}"
            f"{'; tel ' + phone_store if phone_store else ''}) marcou {when}."
            + cal_note + wa_note
        )

        # Funil de campanhas OPCIONAL: lead que marcou reuniao vira "qualificado"
        # no seu painel/CRM (so se PAINEL_CAMPANHAS_* estiver configurado).
        try:
            _funil_marcar_qualificado(phones, when)
        except Exception:
            pass

        # NOTIFICACAO INTERNA DA EQUIPE via WhatsApp (config.TEAM_NOTIFY_WHATSAPP).
        # Falha graciosa por numero.
        team = getattr(config, "TEAM_NOTIFY_WHATSAPP", {}) or {}
        if team:
            team_msg = config.msg_equipe_novo_agendamento(
                name, when, partner, emails, phones, ev.get("meet_link"))
            for _label, _num in team.items():
                wa.send_text(_num, team_msg, slug=f"equipe-{bid}")

    return jsonify({
        "ok": True,
        "id": bid,
        "meet_link": ev.get("meet_link"),
        "calendar_ok": bool(ev.get("ok")),
        "when": when,
    })


# ---------------------------------------------------------------- admin
@app.route("/admin")
@require_admin
def admin():
    conn = db()
    rows = conn.execute(
        "SELECT * FROM bookings ORDER BY slot_start DESC LIMIT 200").fetchall()
    conn.close()
    cal = gcal.calendar_ok()
    return render_template_string(ADMIN_HTML, rows=rows, cal_ok=cal,
                                  reauth_url=url_for("oauth_start"), **_brand_ctx())


@app.route("/admin/delete/<int:bid>", methods=["POST"])
@require_admin
def admin_delete(bid):
    conn = db()
    r = conn.execute("SELECT event_id FROM bookings WHERE id=?", (bid,)).fetchone()
    if r and r["event_id"]:
        gcal.delete_event(r["event_id"])
    conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin"))


@app.route("/admin/recording/<int:bid>", methods=["POST"])
@require_admin
def admin_recording(bid):
    """Busca a gravacao do Meet no Drive do organizador (dono da instancia) e
    associa ao agendamento. Depende do escopo drive.readonly no token (opcional,
    ver config.OAUTH_SCOPES). So funciona apos a reuniao (a gravacao leva alguns
    minutos pra aparecer no Drive)."""
    conn = db()
    r = conn.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
    conn.close()
    if not r:
        return jsonify({"ok": False, "error": "Agendamento nao encontrado."}), 404
    # busca por nome do cliente + apos o inicio da reuniao
    first = (r["name"] or "").split()[0] if r["name"] else ""
    res = gcal.find_recording(query_terms=[first] if first else None,
                              after_iso=r["slot_start"])
    if not res.get("ok"):
        return jsonify({"ok": False, "error": res.get("error")}), 502
    files = res.get("files") or []
    if not files:
        return jsonify({"ok": False, "error": "Nenhuma gravacao encontrada ainda "
                        "(pode levar alguns minutos apos a reuniao)."}), 404
    link = files[0].get("webViewLink")
    conn2 = db()
    conn2.execute("UPDATE bookings SET recording_link=? WHERE id=?", (link, bid))
    conn2.commit()
    conn2.close()
    return jsonify({"ok": True, "recording_link": link,
                    "name": files[0].get("name")})


# ---------------------------------------------------------------- reagendamento (core)
def do_reschedule(bid, iso_start, actor="admin"):
    """Move um agendamento pra novo horario. Transacional e idempotente.
    Retorna (ok: bool, payload: dict). payload traz 'error' em caso de falha
    com 'code' (not-found, invalid, too-soon, conflict, moved-to-same).
    actor in {'admin','client'} so muda a nota da notificacao interna."""
    # valida novo horario
    try:
        start, end = slots_mod.parse_slot(iso_start)
    except Exception:
        return False, {"code": "invalid", "error": "Horario invalido."}

    conn = db()
    try:
        # SERIALIZED: pega lock de escrita ja no inicio pra evitar corrida
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        r = conn.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
        if not r:
            conn.execute("ROLLBACK")
            return False, {"code": "not-found", "error": "Agendamento nao encontrado."}
        if r["status"] != "confirmed":
            conn.execute("ROLLBACK")
            return False, {"code": "not-found",
                           "error": "Este agendamento nao esta ativo (cancelado?)."}

        old_start_dt = datetime.datetime.fromisoformat(r["slot_start"])
        if old_start_dt.tzinfo is None:
            old_start_dt = old_start_dt.replace(tzinfo=TZ)

        # mesmo horario? idempotente: nada muda
        if r["slot_start"] == start.isoformat():
            conn.execute("ROLLBACK")
            return False, {"code": "moved-to-same",
                           "error": "Este ja e o horario atual do agendamento."}

        now = datetime.datetime.now(TZ)
        if start < now + datetime.timedelta(hours=config.MIN_LEAD_HOURS):
            conn.execute("ROLLBACK")
            return False, {"code": "too-soon",
                           "error": "Escolha um horario com mais antecedencia."}

        # conflito: algum OUTRO agendamento ativo ocupa o novo slot?
        clash = conn.execute(
            "SELECT slot_start, slot_end FROM bookings "
            "WHERE status='confirmed' AND id<>?", (bid,)).fetchall()
        for c in clash:
            try:
                cs = datetime.datetime.fromisoformat(c["slot_start"])
                ce = datetime.datetime.fromisoformat(c["slot_end"])
            except Exception:
                continue
            if start < ce and cs < end:
                conn.execute("ROLLBACK")
                return False, {"code": "conflict",
                               "error": "Esse horario acabou de ser ocupado. Escolha outro."}

        # atualiza banco (libera slot antigo implicitamente / bloqueia novo);
        # reseta flags de lembrete pro novo horario.
        conn.execute("""
            UPDATE bookings
            SET slot_start=?, slot_end=?,
                reminded_1d=0, reminded_1h=0, reminded_15m=0
            WHERE id=?
        """, (start.isoformat(), end.isoformat(), bid))
        conn.execute("COMMIT")
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        conn.close()
        return False, {"code": "error", "error": f"Falha interna: {e}"}
    finally:
        try:
            conn.isolation_level = ""  # volta ao default
        except Exception:
            pass

    # ---- pos-commit: efeitos externos (Google, WhatsApp, outbox) ----
    # move evento no Google se conectado e se ha event_id; senao segue no banco.
    meet_link = r["meet_link"]
    gcal_note = ""
    if r["event_id"]:
        mv = gcal.move_event(r["event_id"], start, end)
        if mv.get("ok"):
            gcal_note = " (Google Agenda atualizado)"
            if mv.get("meet_link"):
                meet_link = mv.get("meet_link")
                conn2 = db()
                conn2.execute("UPDATE bookings SET meet_link=?, calendar_ok=1 WHERE id=?",
                              (meet_link, bid))
                conn2.commit()
                conn2.close()
        else:
            gcal_note = f" (Google Agenda NAO atualizado: {mv.get('error')})"
    else:
        gcal_note = " (sem evento no Google: sera criado quando reconectar)"

    old_when = old_start_dt.strftime("%d/%m/%Y as %H:%M")
    new_when = start.strftime("%d/%m/%Y as %H:%M")

    # avisa o cliente por WhatsApp (gracioso) — nunca em agendamento de teste
    wa_note = ""
    if r["phone"] and not r["is_test"]:
        res = wa.send_text(
            r["phone"],
            config.msg_reagendado(r["name"], old_when, new_when, meet_link,
                                  r["reschedule_token"]),
            slug=f"reagendo-{bid}")
        if res.get("ok"):
            if not res.get("dryrun"):
                # hook opcional de atendimento (no-op no pacote padrao).
                active_client_hook.mark_apresentacao(
                    r["phone"],
                    detalhe=f"{config.EVENT_TITLE} reagendada pra {new_when}.")
            wa_note = " [WA dry-run]" if res.get("dryrun") else " (cliente avisado por WhatsApp)"
        else:
            wa_note = f" (WhatsApp ao cliente NAO enviado: {res.get('error')})"

    # notificacao interna
    if not r["is_test"]:
        origem = "pelo proprio cliente" if actor == "client" else "no painel admin"
        notify_outbox(
            f"Reagendamento ({origem}): {r['name']} "
            f"({r['email']}) mudou de {old_when} para {new_when}."
            + gcal_note + wa_note)

    return True, {"id": bid, "old_when": old_when, "new_when": new_when,
                  "meet_link": meet_link, "when": new_when}


@app.route("/admin/reschedule/<int:bid>", methods=["POST"])
@require_admin
def admin_reschedule(bid):
    iso_start = ""
    if request.is_json:
        iso_start = ((request.get_json(silent=True) or {}).get("iso_start") or "").strip()
    if not iso_start:
        iso_start = (request.form.get("iso_start") or "").strip()
    if not iso_start:
        return jsonify({"ok": False, "error": "Informe o novo horario."}), 400
    ok, payload = do_reschedule(bid, iso_start, actor="admin")
    status = 200 if ok else (409 if payload.get("code") in ("conflict", "too-soon") else 400)
    return jsonify({"ok": ok, **payload}), status


@app.route("/api/slots/<int:bid>")
def api_slots_for_booking(bid):
    """Slots livres considerando que o proprio agendamento 'bid' libera o seu slot.
    Usado tanto pelo admin quanto pela pagina do cliente (via token, ver rota)."""
    data = slots_mod.generate_slots(booked_ranges(exclude_id=bid))
    return jsonify({"days": data, "duration": config.EVENT_DURATION_MIN})


# ---------------------------------------------------------------- reagendamento (cliente)
def _booking_by_token(token):
    conn = db()
    r = conn.execute(
        "SELECT * FROM bookings WHERE reschedule_token=?", (token,)).fetchone()
    conn.close()
    return r


@app.route("/reagendar/<token>")
def reschedule_page(token):
    r = _booking_by_token(token)
    if not r:
        return render_template_string(RESCHEDULE_HTML, invalid=True,
                                      bid=None, token=token, cur_when="",
                                      name="", cancelled=False, **_brand_ctx()), 404
    cancelled = (r["status"] != "confirmed")
    cur_dt = datetime.datetime.fromisoformat(r["slot_start"])
    cur_when = cur_dt.strftime("%d/%m/%Y as %H:%M")
    return render_template_string(
        RESCHEDULE_HTML, invalid=False, bid=r["id"], token=token,
        cur_when=cur_when, name=(r["name"] or "").split()[0].title(),
        cancelled=cancelled, **_brand_ctx())


@app.route("/api/reagendar/<token>/slots")
def api_reschedule_slots(token):
    r = _booking_by_token(token)
    if not r or r["status"] != "confirmed":
        return jsonify({"days": {}, "duration": config.EVENT_DURATION_MIN}), 404
    data = slots_mod.generate_slots(booked_ranges(exclude_id=r["id"]))
    return jsonify({"days": data, "duration": config.EVENT_DURATION_MIN})


@app.route("/api/reagendar/<token>", methods=["POST"])
def api_reschedule_do(token):
    r = _booking_by_token(token)
    if not r:
        return jsonify({"ok": False, "error": "Link invalido."}), 404
    d = request.get_json(force=True, silent=True) or {}
    iso_start = (d.get("iso_start") or "").strip()
    if not iso_start:
        return jsonify({"ok": False, "error": "Escolha um horario."}), 400
    ok, payload = do_reschedule(r["id"], iso_start, actor="client")
    status = 200 if ok else (409 if payload.get("code") in ("conflict", "too-soon") else 400)
    return jsonify({"ok": ok, **payload}), status


@app.route("/api/cancelar/<token>", methods=["POST"])
def api_cancel_do(token):
    """Cancelamento pelo PROPRIO CLIENTE, via link publico (mesmo token do
    reagendamento). Mesmo padrao destrutivo do /admin/delete: apaga o evento no
    Google Calendar (se houver) e marca status='cancelled'. Idempotente:
    reenviar em algo ja cancelado devolve 409 sem estourar."""
    r = _booking_by_token(token)
    if not r:
        return jsonify({"ok": False, "error": "Link invalido."}), 404
    if r["status"] != "confirmed":
        return jsonify({"ok": False,
                        "error": "Este agendamento ja foi cancelado."}), 409

    bid = r["id"]
    # apaga o evento no Google Calendar (gracioso: nunca trava o cancelamento)
    if r["event_id"]:
        try:
            gcal.delete_event(r["event_id"])
        except Exception as e:
            app.logger.warning("[cancelar] delete_event falhou bid=%s: %s", bid, e)

    conn = db()
    conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
    conn.commit()
    conn.close()

    when = datetime.datetime.fromisoformat(r["slot_start"]).strftime("%d/%m/%Y as %H:%M")

    # reconstroi listas (colunas legadas guardam comma-separated)
    emails = [e.strip() for e in (r["email"] or "").split(",") if e.strip()]
    phones = [p.strip() for p in (r["phone"] or "").split(",") if p.strip()]

    if not r["is_test"]:
        # avisa a EQUIPE (config.TEAM_NOTIFY_WHATSAPP) por WhatsApp — falha graciosa por numero
        team = getattr(config, "TEAM_NOTIFY_WHATSAPP", {}) or {}
        if team:
            team_msg = config.msg_equipe_cancelamento(
                r["name"], when, r["partner"], emails, phones)
            for _label, _num in team.items():
                try:
                    wa.send_text(_num, team_msg, slug=f"cancel-equipe-{bid}")
                except Exception as e:
                    app.logger.warning(
                        "[cancelar] aviso equipe %s falhou: %s", _label, e)

        # confirmacao curta de cancelamento pro proprio CLIENTE (gracioso)
        for ph in phones:
            try:
                wa.send_text(ph, config.msg_cancelado_cliente(r["name"], when),
                             slug=f"cancel-cliente-{bid}")
            except Exception as e:
                app.logger.warning("[cancelar] aviso cliente falhou: %s", e)

        # notificacao interna via Telegram (mesmo canal do reagendamento)
        notify_outbox(
            f"Cancelamento (pelo proprio cliente): {r['name']} "
            f"({r['email']}) cancelou o agendamento de {when}.")

    return jsonify({"ok": True})


# ---------------------------------------------------------------- health
@app.route("/health")
def health():
    return jsonify({"ok": True, "calendar": gcal.calendar_ok()})


# ---------------------------------------------------------------- public page
def _brand_ctx():
    """Contexto de marca pros templates publicos (lido do config/env).
    brand_wa_link = link wa.me se BRAND_WHATSAPP configurado, senao ''."""
    wa_digits = (getattr(config, "BRAND_WHATSAPP", "") or "").strip()
    return {
        "BRAND_NAME": getattr(config, "BRAND_NAME", "Sua Empresa"),
        "EVENT_TITLE": getattr(config, "EVENT_TITLE", "Apresentação"),
        "PUBLIC_BASE_URL": getattr(config, "PUBLIC_BASE_URL", ""),
        "BRAND_WHATSAPP": wa_digits,
        "BRAND_WA_LINK": (f"https://wa.me/{wa_digits}" if wa_digits else ""),
    }


@app.route("/")
def index():
    partner = request.args.get("p", "").strip()[:60]
    return render_template_string(PUBLIC_HTML, partner=partner, **_brand_ctx())


# templates carregados de arquivo pra manter app.py enxuto
with open(os.path.join(os.path.dirname(__file__), "templates_public.html")) as _f:
    PUBLIC_HTML = _f.read()
with open(os.path.join(os.path.dirname(__file__), "templates_admin.html")) as _f:
    ADMIN_HTML = _f.read()
with open(os.path.join(os.path.dirname(__file__), "templates_reschedule.html")) as _f:
    RESCHEDULE_HTML = _f.read()


init_db()

if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5120
    app.run(host="127.0.0.1", port=port, threaded=True)
