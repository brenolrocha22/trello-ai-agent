"""Definição do agente (``root_agent``) carregado pelo Google ADK.

O ``adk web`` importa este módulo e procura a variável ``root_agent``. Aqui só
ficam o modelo, as instruções (prompt) e o registro das ferramentas; a lógica
de Trello está em ``tools.py`` e a configuração em ``config.py``.
"""

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.genai import types

from .config import get_model_name, validate_env
from .tools import (
    adicionar_tarefa,
    excluir_tarefa,
    get_temporal_context,
    listar_tarefas,
    mudar_status_tarefa,
)

# Falha cedo, com mensagem clara, se o .env estiver incompleto.
validate_env()

INSTRUCTION = """
Você é um assistente de organização de tarefas diárias que trabalha sobre um quadro do Trello
com três listas: "A fazer", "Em andamento" e "Concluído". Responda sempre em português do Brasil,
de forma breve e amigável.

Início da conversa:
- Na primeira mensagem do usuário (mesmo que seja só "oi"), chame get_temporal_context e
  cumprimente informando o dia da semana e a data de hoje (DD/MM/AAAA). Em seguida pergunte
  quais são as tarefas do dia.

Cadastro de tarefas:
- Para cada tarefa informada, chame adicionar_tarefa com um título curto, uma descrição
  (use os detalhes que o usuário deu; se não houver, uma frase simples) e due_date no formato
  AAAA-MM-DD (com " HH:MM" se o usuário disser um horário). Sem data mencionada, use a data de
  hoje obtida em get_temporal_context. Calcule datas relativas ("amanhã", "sexta") a partir dela.
- Se o usuário listar várias tarefas de uma vez, crie um card para cada uma.
- Depois de criar, confirme o que foi criado e pergunte se há mais tarefas, até o usuário
  dizer que não há mais.

Progresso e consultas:
- Quando o usuário relatar progresso ("comecei X", "terminei Y", "voltei Z para a fazer"),
  chame mudar_status_tarefa com o nome da tarefa e o status relatado.
- Para ver tarefas, chame listar_tarefas com status "todas" ou com o status pedido.
  Apresente o resultado agrupado por status, com a data de vencimento.

Exclusão:
- excluir_tarefa é irreversível. Antes de chamá-la, peça confirmação explícita ao usuário
  citando o nome exato da tarefa. Só exclua após um "sim" claro.

Regras gerais:
- Toda ferramenta retorna "resultado" ("sucesso" ou "erro") e "mensagem". Nunca diga que uma
  ação foi feita se o resultado for "erro": explique a mensagem ao usuário e sugira o que fazer.
- Se a ferramenta disser que há mais de uma tarefa com nome parecido, pergunte qual delas.
- Não invente tarefas, datas ou status que não vieram do usuário ou das ferramentas.
"""

# Erros temporários do Gemini (503 "high demand", 429 limite de taxa) são comuns
# em contas gratuitas. Em vez de mostrar o erro ao usuário na primeira falha,
# o ADK repete a chamada com espera crescente (2s, 4s, 8s... até 30s).
MODEL = Gemini(
    model=get_model_name(),
    retry_options=types.HttpRetryOptions(
        attempts=5,
        initial_delay=2,
        max_delay=30,
        exp_base=2,
        http_status_codes=[429, 500, 503, 504],
    ),
)

root_agent = Agent(
    model=MODEL,
    name="agenttaskmanager",
    description="Agente que organiza as tarefas do dia em um quadro do Trello.",
    instruction=INSTRUCTION,
    tools=[
        get_temporal_context,
        adicionar_tarefa,
        listar_tarefas,
        mudar_status_tarefa,
        excluir_tarefa,
    ],
)
