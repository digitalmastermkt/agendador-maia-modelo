#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
active_client_hook.py — STUB no-op (pacote portatil).

No sistema original, este modulo registrava o numero do cliente na "allowlist"
de um motor de atendimento (SDR) proprio, pra que a resposta do cliente no
WhatsApp fosse tratada. Esse motor NAO faz parte deste pacote de agendamento.

Aqui o hook e um no-op: nunca faz nada e nunca lanca excecao. Existe apenas pra
satisfazer o `import active_client_hook` do app.py/reminders.py sem quebrar.

Se voce tiver um motor de atendimento proprio, implemente mark_apresentacao()
aqui pra integrar (registrar o telefone/contexto). Caso contrario, deixe como
esta — o agendamento funciona 100% sem isso.
"""


def mark_apresentacao(phone_raw, detalhe=""):
    """No-op. Retorna sempre False. Nunca lanca."""
    return False
