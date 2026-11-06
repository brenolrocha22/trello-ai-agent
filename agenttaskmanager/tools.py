"""Ferramentas (tools) que o agente Gemini chama para gerenciar tarefas no Trello.

Como o ADK usa estas funções:
    Cada função pública abaixo é registrada em ``agent.py``. O ADK lê o nome,
    a assinatura (tipos dos parâmetros) e a docstring para descrevê-la ao
    Gemini, que decide quando chamá-la e com quais argumentos. Por isso as
    docstrings das ferramentas são escritas para o modelo, não só para humanos.

Contrato de retorno:
    Toda ferramenta devolve um ``dict`` com ``resultado`` = ``"sucesso"`` ou
    ``"erro"`` e uma ``mensagem``. Nenhuma exceção escapa: erros viram uma
    resposta que o agente consegue explicar ao usuário.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, time, timedelta

from requests.exceptions import RequestException
from trello.exceptions import ResourceUnavailable, Unauthorized

from .config import TaskAgentError, get_board, get_timezone

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Status e listas do board
# --------------------------------------------------------------------------

# Nome exibido de cada lista, na ordem em que aparecem no board.
STATUS_LISTS = {
    "a_fazer": "A fazer",
    "em_andamento": "Em andamento",
    "concluido": "Concluído",
}

# Expressões (já normalizadas: minúsculas e sem acento) reconhecidas para cada
# status. Cobrem o nome das listas em PT/EN e o jeito natural de relatar progresso.
STATUS_ALIASES = {
    "a_fazer": [
        "a fazer", "afazer", "fazer", "por fazer", "to do", "todo", "pendente",
        "pendentes", "nao iniciado", "nao iniciada", "nao comecei", "backlog",
    ],
    "em_andamento": [
        "em andamento", "andamento", "em progresso", "progresso", "in progress",
        "doing", "fazendo", "comecei", "iniciei", "iniciado", "iniciada",
        "trabalhando", "em execucao", "executando",
    ],
    "concluido": [
        "concluido", "concluida", "concluidos", "concluidas", "conclui", "done",
        "feito", "feita", "feitos", "feitas", "finalizado", "finalizada",
        "finalizados", "finalizadas", "finalizei", "terminei", "terminado",
        "terminada", "terminados", "terminadas", "pronto", "pronta", "prontos",
        "prontas", "completo", "completa", "completei", "entregue", "entreguei",
    ],
}

ALL_STATUS_WORDS = {"todas", "todos", "tudo", "all", "geral", ""}


def normalize_text(text: str) -> str:
    """Minúsculas, sem acentos e com espaços simples: 'Concluído ' -> 'concluido'."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    no_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", no_accents).strip().lower()


def resolve_status(text: str) -> str | None:
    """Converte texto livre em uma chave de status (``a_fazer``, ``em_andamento``, ``concluido``).

    Primeiro tenta correspondência exata; depois procura a expressão dentro da
    frase ("já terminei" -> concluido). Expressões maiores são testadas antes,
    para que "não comecei" (a fazer) vença "comecei" (em andamento).
    Retorna ``None`` se nada for reconhecido.
    """
    normalized = normalize_text(text)
    if not normalized:
        return None
    for key, aliases in STATUS_ALIASES.items():
        if normalized in aliases:
            return key
    candidates = sorted(
        ((alias, key) for key, aliases in STATUS_ALIASES.items() for alias in aliases),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
    for alias, key in candidates:
        if re.search(rf"\b{re.escape(alias)}\b", normalized):
            return key
    return None


def _status_lists(board) -> dict:
    """Mapeia cada status para a lista correspondente do board, criando as que faltarem.

    Aceita listas já existentes com nomes alternativos (ex.: "To Do", "Done").
    Saída: ``{"a_fazer": List, "em_andamento": List, "concluido": List}``.
    """
    found = {}
    for trello_list in board.list_lists(list_filter="open"):
        name = normalize_text(trello_list.name)
        for key, aliases in STATUS_ALIASES.items():
            if key not in found and name in aliases:  # nome exato, sem busca parcial
                found[key] = trello_list
    for key, display_name in STATUS_LISTS.items():
        if key not in found:
            logger.info("Criando lista ausente '%s' no board.", display_name)
            found[key] = board.add_list(display_name, pos="bottom")
    return found


# --------------------------------------------------------------------------
# Datas e fuso horário
# --------------------------------------------------------------------------

# Horário usado quando o usuário informa só o dia. Meio-dia local fica a
# ±12h da meia-noite, então o dia exibido não muda mesmo que o Trello
# (navegador) e o agente estejam em fusos um pouco diferentes.
DEFAULT_DUE_TIME = time(12, 0)

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y")
_DATETIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M")

_WEEKDAYS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
             "sexta-feira", "sábado", "domingo"]


def parse_due_date(text: str, now: datetime | None = None) -> datetime:
    """Converte a data informada em um ``datetime`` com fuso horário explícito.

    Correção do bug do projeto-base: lá a string "2026-11-06" ia direto para o
    Trello, que a interpretava como 00:00 UTC — ou seja, 21:00 do dia 05 no
    Brasil (UTC-3). Aqui a data vira um instante local com offset
    (ex.: ``2026-11-06T12:00:00-03:00``), e o Trello grava o dia correto.

    Aceita: vazio/"hoje", "amanhã", AAAA-MM-DD, DD/MM/AAAA, com ou sem HH:MM.
    """
    tz = get_timezone()
    now = now or datetime.now(tz)
    raw = normalize_text(text)

    if raw in ("", "hoje", "today"):
        return datetime.combine(now.date(), DEFAULT_DUE_TIME, tzinfo=tz)
    if raw in ("amanha", "tomorrow"):
        return datetime.combine(now.date() + timedelta(days=1), DEFAULT_DUE_TIME, tzinfo=tz)

    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    for fmt in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt).date()
            return datetime.combine(parsed, DEFAULT_DUE_TIME, tzinfo=tz)
        except ValueError:
            pass
    raise TaskAgentError(
        f"Data '{text}' não reconhecida. Use AAAA-MM-DD (ex.: 2026-11-06), DD/MM/AAAA, 'hoje' ou 'amanhã'."
    )


def format_due_date(raw_due: str | None) -> str | None:
    """Converte o ``due`` retornado pelo Trello (UTC, ex.: '2026-11-06T15:00:00.000Z')
    para o horário local, no formato 'AAAA-MM-DD HH:MM'. Retorna ``None`` sem data."""
    if not raw_due:
        return None
    parsed = datetime.fromisoformat(raw_due.replace("Z", "+00:00"))
    return parsed.astimezone(get_timezone()).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------
# Busca de cards e tratamento de erros (compartilhados pelas ferramentas)
# --------------------------------------------------------------------------

def _find_card(board, task_name: str):
    """Localiza um card aberto pelo nome, em qualquer lista do board.

    Ordem: nome exato (ignorando acentos/maiúsculas); se não houver, nome que
    contenha o texto buscado. Mais de um candidato gera erro pedindo ao
    usuário que especifique, para nunca mover/excluir o card errado.
    """
    target = normalize_text(task_name)
    if not target:
        raise TaskAgentError("Informe o nome da tarefa.")
    cards = board.open_cards()
    exact = [c for c in cards if normalize_text(c.name) == target]
    matches = exact or [c for c in cards if target in normalize_text(c.name)]
    if not matches:
        raise TaskAgentError(f"Tarefa '{task_name}' não encontrada no board.")
    if len(matches) > 1:
        names = "; ".join(f"'{c.name}'" for c in matches)
        raise TaskAgentError(f"Mais de uma tarefa corresponde a '{task_name}': {names}. Especifique qual.")
    return matches[0]


def _ok(message: str, **data) -> dict:
    return {"resultado": "sucesso", "mensagem": message, **data}


def _error(exc: Exception) -> dict:
    """Traduz exceções em uma resposta amigável, registrando o detalhe técnico no log."""
    if isinstance(exc, TaskAgentError):
        message = str(exc)
    elif isinstance(exc, Unauthorized):
        message = "Trello recusou as credenciais (401). Verifique TRELLO_API_KEY e TRELLO_TOKEN."
    elif isinstance(exc, ResourceUnavailable):
        message = f"Recurso indisponível no Trello: {exc}"
    elif isinstance(exc, RequestException):
        message = "Falha de conexão com o Trello. Verifique sua internet e tente novamente."
    else:
        logger.exception("Erro inesperado em ferramenta do agente")
        message = f"Erro inesperado: {exc}"
    return {"resultado": "erro", "mensagem": message}


# --------------------------------------------------------------------------
# Ferramentas registradas no agente
# --------------------------------------------------------------------------

def get_temporal_context() -> dict:
    """Retorna a data e a hora atuais no fuso do usuário.

    Use no início da conversa para cumprimentar o usuário informando a data
    de hoje e sempre que precisar saber que dia é hoje (ex.: para calcular "amanhã").
    """
    try:
        now = datetime.now(get_timezone())
        return _ok(
            "Contexto temporal obtido.",
            data=now.strftime("%Y-%m-%d"),
            data_formatada=now.strftime("%d/%m/%Y"),
            dia_da_semana=_WEEKDAYS[now.weekday()],
            hora=now.strftime("%H:%M"),
            fuso_utc=now.strftime("%z"),
        )
    except Exception as exc:
        return _error(exc)


def adicionar_tarefa(nome_da_task: str, descricao_da_task: str, due_date: str = "") -> dict:
    """Cria um card na lista "A fazer" do Trello.

    Args:
        nome_da_task: Título curto da tarefa.
        descricao_da_task: Descrição/detalhes da tarefa (pode ser vazia).
        due_date: Data de entrega no formato AAAA-MM-DD, opcionalmente com HH:MM
            (ex.: "2026-11-06" ou "2026-11-06 15:30"). Vazio = hoje.

    Returns:
        dict com resultado, mensagem e os dados do card criado (id, nome, vencimento, url).
    """
    try:
        if not nome_da_task or not nome_da_task.strip():
            raise TaskAgentError("O nome da tarefa não pode ser vazio.")
        due = parse_due_date(due_date)
        board = get_board()
        todo_list = _status_lists(board)["a_fazer"]
        card = todo_list.add_card(
            name=nome_da_task.strip(),
            desc=(descricao_da_task or "").strip(),
            due=due.isoformat(),  # ISO 8601 com offset: o Trello converte para UTC corretamente
        )
        return _ok(
            f"Tarefa '{card.name}' criada em '{todo_list.name}' para {due.strftime('%d/%m/%Y %H:%M')}.",
            id=card.id,
            nome=card.name,
            vencimento=due.strftime("%Y-%m-%d %H:%M"),
            status=todo_list.name,
            url=getattr(card, "url", None),
        )
    except Exception as exc:
        return _error(exc)


def listar_tarefas(status: str = "todas") -> dict:
    """Lista as tarefas do board, todas ou filtradas por status.

    Args:
        status: "todas" (padrão), ou um status em linguagem natural:
            "a fazer"/"pendentes", "em andamento"/"fazendo", "concluído"/"feitas".

    Returns:
        dict com resultado, mensagem, total e a lista de tarefas
        (nome, descrição, vencimento local, status e id).
    """
    try:
        if normalize_text(status) in ALL_STATUS_WORDS:
            wanted_key = None
        else:
            wanted_key = resolve_status(status)
            if wanted_key is None:
                raise TaskAgentError(
                    f"Status '{status}' não reconhecido. Use 'a fazer', 'em andamento', 'concluído' ou 'todas'."
                )

        board = get_board()
        lists = _status_lists(board)
        selected = {key: lst for key, lst in lists.items() if wanted_key in (None, key)}
        list_names = {lst.id: lst.name for lst in selected.values()}

        tasks = [
            {
                "nome": card.name,
                "descricao": card.desc,
                "vencimento": format_due_date(card.due),
                "status": list_names[card.idList],
                "id": card.id,
            }
            for card in board.open_cards()
            if card.idList in list_names
        ]
        label = "todas as listas" if wanted_key is None else f"'{STATUS_LISTS[wanted_key]}'"
        return _ok(f"{len(tasks)} tarefa(s) em {label}.", total=len(tasks), tarefas=tasks)
    except Exception as exc:
        return _error(exc)


def mudar_status_tarefa(nome_da_task: str, novo_status: str) -> dict:
    """Move uma tarefa para outra lista conforme o progresso relatado pelo usuário.

    Args:
        nome_da_task: Nome (ou parte única do nome) da tarefa.
        novo_status: Novo status em linguagem natural, ex.: "a fazer",
            "em andamento", "comecei", "concluído", "terminei", "done".

    Returns:
        dict com resultado, mensagem, status de origem e de destino.
    """
    try:
        target_key = resolve_status(novo_status)
        if target_key is None:
            raise TaskAgentError(
                f"Status '{novo_status}' não reconhecido. Use 'a fazer', 'em andamento' ou 'concluído'."
            )
        board = get_board()
        lists = _status_lists(board)
        card = _find_card(board, nome_da_task)
        destination = lists[target_key]
        origin_name = next((l.name for l in lists.values() if l.id == card.idList), "outra lista")

        if card.idList == destination.id:
            return _ok(f"'{card.name}' já está em '{destination.name}'.", de=origin_name, para=destination.name)

        card.change_list(destination.id)
        return _ok(f"'{card.name}': {origin_name} → {destination.name}.", de=origin_name, para=destination.name)
    except Exception as exc:
        return _error(exc)


def excluir_tarefa(nome_da_task: str) -> dict:
    """Exclui definitivamente um card do Trello. Ação irreversível.

    Só chame depois que o usuário confirmar explicitamente a exclusão.

    Args:
        nome_da_task: Nome (ou parte única do nome) da tarefa a excluir.

    Returns:
        dict com resultado, mensagem e o nome da tarefa excluída.
    """
    try:
        board = get_board()
        card = _find_card(board, nome_da_task)
        name = card.name
        card.delete()
        return _ok(f"Tarefa '{name}' excluída.", nome=name)
    except Exception as exc:
        return _error(exc)
