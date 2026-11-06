"""Teste de integração contra o Trello REAL (board de TRELLO_BOARD_NAME).

Só executa com credenciais válidas no .env e opt-in explícito, porque cria,
move e exclui um card de teste no seu board:

    PowerShell:  $env:RUN_TRELLO_INTEGRATION="1"; python -m unittest tests.test_integration -v
    Bash:        RUN_TRELLO_INTEGRATION=1 python -m unittest tests.test_integration -v

O card criado tem um nome único e é excluído ao final, mesmo se algo falhar.
"""

import os
import unittest
import uuid
from datetime import datetime

try:
    # Importar o pacote carrega o .env e valida as credenciais (agent.py).
    from agenttaskmanager import config, tools
    _configured = True
except Exception:  # .env ausente/incompleto: o teste é pulado, não quebra a suíte
    _configured = False

RUN = (
    os.getenv("RUN_TRELLO_INTEGRATION") == "1"
    and _configured
    and os.getenv("TRELLO_TOKEN") != "fake-for-tests"  # valor fictício dos testes unitários
)


@unittest.skipUnless(RUN, "Defina RUN_TRELLO_INTEGRATION=1 e configure o .env para rodar contra o Trello real.")
class TrelloIntegrationTest(unittest.TestCase):
    def test_full_lifecycle(self):
        name = f"[teste-agente] {uuid.uuid4().hex[:8]}"
        today = datetime.now(config.get_timezone()).strftime("%Y-%m-%d")
        created = False
        try:
            # 1. Criação, com data de hoje
            result = tools.adicionar_tarefa(name, "Card criado pelo teste de integração", today)
            self.assertEqual(result["resultado"], "sucesso", result)
            created = True

            # 2. Listagem geral + consistência da data lida de volta do Trello
            listed = tools.listar_tarefas("todas")
            card = next(t for t in listed["tarefas"] if t["nome"] == name)
            self.assertTrue(card["vencimento"].startswith(today), f"Data mudou: {card['vencimento']} != {today}")

            # 3. Filtro por status
            todo = tools.listar_tarefas("a fazer")
            self.assertIn(name, [t["nome"] for t in todo["tarefas"]])

            # 4. Movimentação entre listas (linguagem natural)
            self.assertEqual(tools.mudar_status_tarefa(name, "comecei")["resultado"], "sucesso")
            self.assertIn(name, [t["nome"] for t in tools.listar_tarefas("em andamento")["tarefas"]])
            self.assertEqual(tools.mudar_status_tarefa(name, "concluído")["resultado"], "sucesso")
            self.assertIn(name, [t["nome"] for t in tools.listar_tarefas("done")["tarefas"]])

            # 5. Exclusão
            self.assertEqual(tools.excluir_tarefa(name)["resultado"], "sucesso")
            created = False
            all_names = [t["nome"] for t in tools.listar_tarefas()["tarefas"]]
            self.assertNotIn(name, all_names)

            # 6. Erro tratado: tarefa inexistente
            self.assertEqual(tools.excluir_tarefa(name)["resultado"], "erro")
        finally:
            if created:
                tools.excluir_tarefa(name)


if __name__ == "__main__":
    unittest.main()
