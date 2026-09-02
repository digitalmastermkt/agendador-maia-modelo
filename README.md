# Agendador (pacote portatil)

App enxuto (Flask + SQLite) para que parceiros/clientes auto-agendem eventos
na Google Agenda do dono, com Google Meet automatico e lembretes de WhatsApp
opcionais.

**Comece pelo guia completo: [README-HANDOFF.md](./README-HANDOFF.md).**

Resumo rapido:
1. Crie uma credencial OAuth (Web) no Google Cloud com o Google Calendar API ativo.
2. `cp .env.example .env` e preencha (Client ID/Secret, dominio, marca, admin).
3. Coloque seus logos em `static/` (ver `static/README.txt`).
4. Instale as dependencias e suba com `bash start.sh` (systemd opcional).
5. Conecte a agenda do dono em `/oauth/start` (ou botao "Reconectar" no `/admin`).

Nada de segredo vem preenchido: tudo sensivel e lido de variaveis de ambiente.
