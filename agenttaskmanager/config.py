"""Configuração do agente: variáveis de ambiente, fuso horário e conexão com o Trello.

Por que este módulo existe:
    No projeto-base, cada ferramenta criava o próprio ``TrelloClient`` e buscava o
    board ``'DIO'`` (nome fixo no código). Aqui essa lógica fica num único lugar,
    reutilizado por todas as ferramentas de ``tools.py``.
"""

from __future__ import annotations

import os
from datetime import datetime, tzinfo
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from trello import TrelloClient

# O .env fica na raiz do projeto (pasta acima deste pacote). Variáveis já
# definidas no sistema têm prioridade sobre o arquivo (override=False).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=False)

REQUIRED_ENV_VARS = (
    "GOOGLE_API_KEY",
    "TRELLO_API_KEY",
    "TRELLO_API_SECRET",
    "TRELLO_TOKEN",
    "TRELLO_BOARD_NAME",
)

# O projeto-base usava gemini-2.5-flash, que a API já não oferece a contas novas
# (404 "no longer available to new users"). Troque via GEMINI_MODEL se necessário.
DEFAULT_MODEL = "gemini-3.8-flash"


class TaskAgentError(Exception):
    """Erro esperado, com mensagem pronta para ser mostrada ao usuário pelo agente."""


def missing_env_vars() -> list[str]:
    """Retorna os nomes das variáveis obrigatórias ausentes ou vazias."""
    return [name for name in REQUIRED_ENV_VARS if not os.getenv(name, "").strip()]


def validate_env() -> None:
    """Interrompe a inicialização se faltar alguma credencial.

    Chamado em ``agent.py`` antes de criar o agente, para que o erro apareça
    logo ao abrir o ``adk web`` em vez de surgir no meio de uma conversa.
    """
    missing = missing_env_vars()
    if missing:
        raise TaskAgentError(
            "Variáveis de ambiente ausentes: "
            + ", ".join(missing)
            + ". Copie .env.exemplo para .env na raiz do projeto e preencha os valores."
        )


def get_model_name() -> str:
    """Modelo Gemini usado pelo agente (configurável via GEMINI_MODEL)."""
    return os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL


def get_timezone() -> tzinfo:
    """Fuso horário usado para interpretar e exibir datas.

    Padrão: fuso do sistema operacional. Opcionalmente, AGENT_TIMEZONE
    (ex.: ``America/Sao_Paulo``) força um fuso IANA — útil em servidores UTC.
    """
    name = os.getenv("AGENT_TIMEZONE", "").strip()
    if not name:
        return datetime.now().astimezone().tzinfo  # fuso local do sistema
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError ou falta do pacote tzdata no Windows
        raise TaskAgentError(
            f"AGENT_TIMEZONE='{name}' inválido ou indisponível. "
            "Use um nome IANA (ex.: America/Sao_Paulo); no Windows instale 'tzdata'."
        ) from exc


@lru_cache(maxsize=1)
def get_trello_client() -> TrelloClient:
    """Cria (uma única vez) o cliente autenticado do Trello.

    Entrada: credenciais do .env. Saída: ``TrelloClient`` reaproveitado por
    todas as ferramentas durante a execução do agente.
    """
    validate_env()
    return TrelloClient(
        api_key=os.environ["TRELLO_API_KEY"],
        api_secret=os.environ["TRELLO_API_SECRET"],
        token=os.environ["TRELLO_TOKEN"],
    )


def get_board():
    """Retorna o board configurado em TRELLO_BOARD_NAME (comparação sem diferenciar maiúsculas).

    Busca a cada chamada (não usa cache) para refletir boards renomeados ou
    recriados sem precisar reiniciar o agente.
    """
    board_name = os.getenv("TRELLO_BOARD_NAME", "").strip()
    boards = get_trello_client().list_boards(board_filter="open")
    for board in boards:
        if board.name.strip().lower() == board_name.lower():
            return board
    available = ", ".join(sorted(b.name for b in boards)) or "nenhum"
    raise TaskAgentError(
        f"Board '{board_name}' não encontrado no Trello. Boards abertos disponíveis: {available}."
    )
