# Agendador — guia de implantacao (handoff)

Guia pratico pra colocar o agendador no ar numa instancia nova. Portugues, passo
a passo. Nada de segredo vem no pacote: tudo sensivel e lido do `.env`.

---

## 1. O que e o sistema

App enxuto em **Flask + SQLite** onde parceiros/clientes **auto-agendam eventos**
(apresentacoes, reunioes, demos) na **Google Agenda do dono**, com:

- **Google Meet automatico** em cada evento (link gerado na hora);
- **pagina publica** de agendamento (escolhe dia e horario livre, informa nome/
  e-mail/WhatsApp) e **pagina de reagendamento/cancelamento** por link unico;
- **painel /admin** (lista, reagenda e cancela);
- **lembretes de WhatsApp opcionais** (1 dia / 1h / 15 min antes) via Evolution API;
- **bloqueio automatico de horarios** cruzando as janelas de disponibilidade com
  os compromissos ja existentes na agenda do dono (freebusy) e com os agendamentos
  ja feitos pelo proprio app.

Falha graciosa por design: sem Google conectado, os agendamentos ficam salvos no
banco; sem WhatsApp configurado, o envio vira no-op; nada disso derruba o fluxo.

---

## 2. Arquitetura dos arquivos

| Arquivo | Papel |
|---|---|
| `app.py` | Rotas Flask + banco SQLite (tabela `bookings`), OAuth, API de slots, reagendamento e cancelamento. |
| `config.py` | Configuracao central: le tudo de variaveis de ambiente; janelas de disponibilidade (`DEFAULT_WINDOWS`); textos das mensagens (`msg_*`). |
| `gcal.py` | Camada Google Calendar: freebusy + criar/mover/apagar evento com Meet (e busca opcional de gravacao no Drive). |
| `slots.py` | Geracao de horarios livres, cruzando janelas + freebusy do dono + agendamentos existentes. |
| `wa.py` | WhatsApp via Evolution API (opcional). Normaliza telefone e envia texto; no-op gracioso se nao configurado. |
| `reminders.py` | Worker de lembretes (rode via cron a cada ~5 min). |
| `digest.py` | Resumo diario opcional dos agendamentos do dia (integracao Telegram opcional). |
| `active_client_hook.py` | Stub no-op. Ponto de extensao pra integrar um motor de atendimento proprio (nao e necessario). |
| `templates_public.html` | Pagina publica de agendamento. |
| `templates_reschedule.html` | Pagina de reagendamento/cancelamento (por link/token). |
| `templates_admin.html` | Painel administrativo. |
| `start.sh` | Sobe o app (carrega o `.env`, setsid). |
| `agendar-anb.service` | Unit systemd (opcional, pra subir no boot). |
| `caddy_route.sh` | Modelo de publicacao num subdominio via Caddy. |
| `.env.example` | Todas as variaveis documentadas (copie pra `.env`). |
| `static/` | Coloque aqui seus logos (`logo.png`, `og.jpg`) — ver `static/README.txt`. |
| `data/availability.example.json` | Exemplo de janelas de disponibilidade por arquivo. |

---

## 3. Setup no Google Cloud (uma vez)

1. Acesse https://console.cloud.google.com e **crie um projeto** (ou use um existente).
2. Em **APIs e Servicos → Biblioteca**, ative a **Google Calendar API**.
   - (Opcional) Ative a **Google Drive API** se quiser usar a busca de gravacoes do Meet.
3. Em **APIs e Servicos → Tela de consentimento OAuth**, configure a tela
   (tipo Externo serve; adicione seu e-mail em "usuarios de teste" enquanto estiver em teste).
4. Em **APIs e Servicos → Credenciais → Criar credenciais → ID do cliente OAuth**:
   - Tipo de aplicativo: **Aplicativo da Web**.
   - **URI de redirecionamento autorizado**: `https://SEU-SUBDOMINIO/oauth/callback`
     (exatamente igual ao `PUBLIC_BASE_URL` que voce vai usar + `/oauth/callback`).
5. Copie o **Client ID** e o **Client Secret** — vao no `.env`.

---

## 4. Preencher o `.env`

```bash
cp .env.example .env
# edite o .env com seus valores
```

Obrigatorios: `GOOGLE_CALENDAR_CLIENT_ID`, `GOOGLE_CALENDAR_CLIENT_SECRET`,
`PUBLIC_BASE_URL`, `ADMIN_PASS`. Marca: `BRAND_NAME`, `EVENT_TITLE`. O resto tem
default ou e opcional (WhatsApp, Telegram, equipe). Ver comentarios no `.env.example`.

---

## 5. Configurar marca e janelas

- **Marca:** `BRAND_NAME`, `EVENT_TITLE`, `BRAND_WHATSAPP` no `.env`; logos em `static/`
  (`logo.png`, `og.jpg` — ver `static/README.txt`).
- **Janelas de disponibilidade:** edite `config.py → DEFAULT_WINDOWS`
  (dia da semana `0=segunda .. 6=domingo`, horarios `HH:MM`). Alternativa sem tocar
  no codigo: crie `data/availability.json` (copie de `data/availability.example.json`);
  se existir, tem prioridade. Outros parametros (duracao, buffer, passo, horizonte,
  antecedencia minima) tambem saem do `.env`/`config.py`.

---

## 6. Subir o app

Dependencias Python (imports reais do codigo):

```bash
python3 -m venv venv
./venv/bin/pip install flask google-auth google-auth-oauthlib google-api-python-client requests
```

Subir (carrega o `.env` e roda na porta `PORT`, default 5120):

```bash
bash start.sh
```

Systemd (opcional, pra subir no boot): edite `agendar-anb.service` trocando
`__USER__` e `__INSTALL_DIR__`, depois:

```bash
sudo cp agendar-anb.service /etc/systemd/system/agendador.service
sudo systemctl daemon-reload
sudo systemctl enable --now agendador
```

Publicacao num subdominio com HTTPS: use `caddy_route.sh` como referencia
(ajuste `SUBDOMINIO`, `INSTALL_DIR`, `PORT`) ou cole o bloco no seu Caddyfile.
O `PUBLIC_BASE_URL` do `.env` precisa bater com o subdominio publicado.

Lembretes (opcional): agende `reminders.py` no cron, ex. a cada 5 min:

```
*/5 * * * * cd /opt/agendador && ./venv/bin/python3 reminders.py >> logs/reminders.log 2>&1
```

Resumo diario (opcional, so com integracao Telegram): agende `digest.py` de manha.

---

## 7. Conectar a Google Agenda do dono

1. Acesse `https://SEU-SUBDOMINIO/admin` (login `ADMIN_USER`/`ADMIN_PASS`).
2. Clique em **"Reconectar agora"** (ou acesse `/oauth/start` direto).
3. **Faca login na conta Google do DONO** (a agenda que vai receber os eventos) e autorize.
4. O token e salvo em `data/calendar_token.json`. A partir dai, cada novo
   agendamento cria **evento + Google Meet** automaticamente.

Enquanto nao conectar, os agendamentos ficam salvos no banco (sem evento/Meet) e o
painel mostra o aviso de reconexao.

---

## 8. NOTA IMPORTANTE — escopo `calendar.readonly` (freebusy)

O `config.OAUTH_SCOPES` **ja vem com dois escopos**:

```python
OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",    # cria/move eventos + Meet
    "https://www.googleapis.com/auth/calendar.readonly",  # le o freebusy da agenda
]
```

O `calendar.readonly` e o que permite ao app **ler os compromissos pessoais do
dono** e **bloquear automaticamente** os horarios ocupados na pagina de
agendamento. **Se voce reduzir os escopos so pra `calendar.events`, a agenda
pessoal do dono deixa de bloquear** — o app so vai evitar conflito com os
agendamentos que ele mesmo criou, e nao com os compromissos que ja estavam la.
Mantenha os dois escopos.

(Se depois de conectar voce mudar os escopos, precisa refazer o `/oauth/start`
pra o novo token carregar as permissoes atualizadas.)

---

## 9. Como funciona no dia a dia

- Link publico: `https://SEU-SUBDOMINIO/` (com `?p=parceiro` pra marcar a origem).
- O cliente escolhe um horario livre, informa dados e recebe confirmacao na hora
  (e o convite do Meet por e-mail).
- Cada agendamento tem um link unico de reagendamento/cancelamento, enviado na
  confirmacao e nos lembretes.
- O dono acompanha tudo em `/admin`.
