"""Testes unitários das ferramentas, sem acesso à rede.

O Trello é substituído por objetos falsos (FakeBoard/FakeList/FakeCard) e o
fuso horário é fixado em UTC-3, para reproduzir o bug de "um dia a menos"
independentemente do fuso da máquina que roda os testes.

Executar:  python -m unittest discover -s tests -v
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# Valores fictícios só para permitir importar o pacote (agent.py valida o .env).
# Nenhum teste deste arquivo faz chamadas reais: get_board é sempre substituído.
for _name in ("GOOGLE_API_KEY", "TRELLO_API_KEY", "TRELLO_API_SECRET", "TRELLO_TOKEN", "TRELLO_BOARD_NAME"):
    os.environ.setdefault(_name, "fake-for-tests")

from requests.exceptions import ConnectionError as RequestsConnectionError  # noqa: E402

from agenttaskmanager import config, tools  # noqa: E402

BRT = timezone(timedelta(hours=-3))  # America/Sao_Paulo sem horário de verão


# ----------------------------------------------------------------- fakes ---

class FakeCard:
    def __init__(self, board, name, desc="", due=None, list_id=None):
        self._board = board
        self.id = f"card-{len(board.cards) + 1}"
        self.name, self.desc, self.due, self.idList = name, desc, due, list_id
        self.url = f"https://trello.com/c/{self.id}"

    def change_list(self, list_id):
        self.idList = list_id

    def delete(self):
        self._board.cards.remove(self)


class FakeList:
    def __init__(self, board, name):
        self._board = board
        self.id = f"list-{name}"
        self.name = name

    def add_card(self, name, desc=None, due=None):
        card = FakeCard(self._board, name, desc, due, self.id)
        self._board.cards.append(card)
        return card


class FakeBoard:
    def __init__(self, list_names=("A fazer", "Em andamento", "Concluído")):
        self.cards = []
        self.lists = [FakeList(self, n) for n in list_names]

    def list_lists(self, list_filter="open"):
        return list(self.lists)

    def add_list(self, name, pos=None):
        new = FakeList(self, name)
        self.lists.append(new)
        return new

    def open_cards(self):
        return list(self.cards)

    def list_named(self, name):
        return next(l for l in self.lists if l.name == name)


class ToolTestCase(unittest.TestCase):
    """Base: board falso + fuso UTC-3 em todas as ferramentas."""

    def setUp(self):
        self.board = FakeBoard()
        patches = [
            patch.object(tools, "get_board", return_value=self.board),
            patch.object(tools, "get_timezone", return_value=BRT),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def add(self, name, list_name="A fazer", due=None):
        return self.board.list_named(list_name).add_card(name, "", due)


# ----------------------------------------------------------- status/texto ---

class StatusResolutionTest(unittest.TestCase):
    def test_canonical_names_with_and_without_accents(self):
        for text, expected in [
            ("A fazer", "a_fazer"), ("Em andamento", "em_andamento"),
            ("Concluído", "concluido"), ("concluido", "concluido"), ("CONCLUÍDA", "concluido"),
        ]:
            self.assertEqual(tools.resolve_status(text), expected, text)

    def test_english_list_names(self):
        self.assertEqual(tools.resolve_status("To Do"), "a_fazer")
        self.assertEqual(tools.resolve_status("Doing"), "em_andamento")
        self.assertEqual(tools.resolve_status("DONE"), "concluido")

    def test_natural_expressions(self):
        for text, expected in [
            ("já terminei", "concluido"), ("finalizei agora", "concluido"), ("está pronto", "concluido"),
            ("comecei hoje cedo", "em_andamento"), ("estou fazendo", "em_andamento"),
            ("ainda não comecei", "a_fazer"), ("pendente", "a_fazer"),
        ]:
            self.assertEqual(tools.resolve_status(text), expected, text)

    def test_unknown_or_empty(self):
        self.assertIsNone(tools.resolve_status("banana"))
        self.assertIsNone(tools.resolve_status(""))


# ------------------------------------------------------------------ datas ---

class DateHandlingTest(unittest.TestCase):
    def setUp(self):
        p = patch.object(tools, "get_timezone", return_value=BRT)
        p.start()
        self.addCleanup(p.stop)
        self.now = datetime(2026, 11, 6, 23, 30, tzinfo=BRT)  # tarde da noite: já é dia 07 em UTC

    def test_iso_date_becomes_local_noon_with_offset(self):
        due = tools.parse_due_date("2026-11-06", self.now)
        self.assertEqual(due.isoformat(), "2026-11-06T12:00:00-03:00")

    def test_regression_one_day_less(self):
        """O instante enviado ao Trello, convertido para UTC, continua no mesmo dia.
        No projeto-base, '2026-11-06' virava 2026-11-06T00:00Z = 05/11 21:00 no Brasil."""
        due = tools.parse_due_date("2026-11-06", self.now)
        self.assertEqual(due.astimezone(timezone.utc).date().isoformat(), "2026-11-06")
        trello_echo = due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.assertEqual(tools.format_due_date(trello_echo), "2026-11-06 12:00")

    def test_brazilian_format_and_explicit_time(self):
        self.assertEqual(tools.parse_due_date("06/11/2026", self.now).isoformat(), "2026-11-06T12:00:00-03:00")
        self.assertEqual(tools.parse_due_date("2026-11-06 15:30", self.now).isoformat(), "2026-11-06T15:30:00-03:00")

    def test_relative_dates_use_local_day(self):
        self.assertEqual(tools.parse_due_date("", self.now).date().isoformat(), "2026-11-06")
        self.assertEqual(tools.parse_due_date("hoje", self.now).date().isoformat(), "2026-11-06")
        self.assertEqual(tools.parse_due_date("amanhã", self.now).date().isoformat(), "2026-11-07")

    def test_invalid_date(self):
        with self.assertRaises(config.TaskAgentError):
            tools.parse_due_date("32/13/2026", self.now)

    def test_format_due_date_empty(self):
        self.assertIsNone(tools.format_due_date(None))
        self.assertIsNone(tools.format_due_date(""))


# ------------------------------------------------------------- ferramentas ---

class TemporalContextTest(ToolTestCase):
    def test_returns_local_date_and_weekday(self):
        result = tools.get_temporal_context()
        self.assertEqual(result["resultado"], "sucesso")
        self.assertRegex(result["data"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertIn(result["dia_da_semana"], tools._WEEKDAYS)
        self.assertEqual(result["fuso_utc"], "-0300")


class AddTaskTest(ToolTestCase):
    def test_creates_card_in_todo_with_offset_due(self):
        result = tools.adicionar_tarefa("Estudar ADK", "Ler a documentação", "2026-11-06")
        self.assertEqual(result["resultado"], "sucesso", result)
        card = self.board.cards[0]
        self.assertEqual(card.idList, self.board.list_named("A fazer").id)
        self.assertEqual(card.due, "2026-11-06T12:00:00-03:00")
        self.assertEqual(card.desc, "Ler a documentação")

    def test_missing_lists_are_created(self):
        self.board = FakeBoard(list_names=())
        with patch.object(tools, "get_board", return_value=self.board):
            result = tools.adicionar_tarefa("Tarefa", "", "2026-11-06")
        self.assertEqual(result["resultado"], "sucesso", result)
        self.assertEqual([l.name for l in self.board.lists], ["A fazer", "Em andamento", "Concluído"])

    def test_reuses_english_lists(self):
        self.board = FakeBoard(list_names=("To Do", "Doing", "Done"))
        with patch.object(tools, "get_board", return_value=self.board):
            tools.adicionar_tarefa("Tarefa", "", "")
        self.assertEqual(len(self.board.lists), 3)
        self.assertEqual(self.board.cards[0].idList, self.board.list_named("To Do").id)

    def test_empty_name_is_error_and_creates_nothing(self):
        result = tools.adicionar_tarefa("   ", "desc", "2026-11-06")
        self.assertEqual(result["resultado"], "erro")
        self.assertEqual(self.board.cards, [])

    def test_invalid_date_is_error_and_creates_nothing(self):
        result = tools.adicionar_tarefa("Tarefa", "", "dia 40")
        self.assertEqual(result["resultado"], "erro")
        self.assertIn("não reconhecida", result["mensagem"])
        self.assertEqual(self.board.cards, [])


class ListTasksTest(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.add("Ler e-mails", "A fazer", "2026-11-06T15:00:00.000Z")
        self.add("Relatório", "Em andamento")
        self.add("Reunião", "Concluído")

    def test_list_all(self):
        result = tools.listar_tarefas()
        self.assertEqual(result["total"], 3)
        first = next(t for t in result["tarefas"] if t["nome"] == "Ler e-mails")
        self.assertEqual(first["vencimento"], "2026-11-06 12:00")  # UTC -> local

    def test_filter_by_status_variations(self):
        for text in ("concluído", "Concluido", "done", "feitas"):
            result = tools.listar_tarefas(text)
            self.assertEqual([t["nome"] for t in result["tarefas"]], ["Reunião"], text)
        self.assertEqual([t["nome"] for t in tools.listar_tarefas("em andamento")["tarefas"]], ["Relatório"])

    def test_invalid_status_is_error_not_everything(self):
        result = tools.listar_tarefas("banana")
        self.assertEqual(result["resultado"], "erro")


class ChangeStatusTest(ToolTestCase):
    def test_moves_with_natural_language(self):
        card = self.add("Escrever relatório")
        result = tools.mudar_status_tarefa("escrever relatorio", "comecei")
        self.assertEqual(result["resultado"], "sucesso", result)
        self.assertEqual(card.idList, self.board.list_named("Em andamento").id)
        tools.mudar_status_tarefa("Escrever relatório", "já terminei")
        self.assertEqual(card.idList, self.board.list_named("Concluído").id)

    def test_partial_name_unique_match(self):
        card = self.add("Pagar conta de luz")
        tools.mudar_status_tarefa("conta de luz", "concluído")
        self.assertEqual(card.idList, self.board.list_named("Concluído").id)

    def test_already_in_destination(self):
        self.add("Tarefa X", "Concluído")
        result = tools.mudar_status_tarefa("Tarefa X", "done")
        self.assertEqual(result["resultado"], "sucesso")
        self.assertIn("já está", result["mensagem"])

    def test_not_found_ambiguous_and_invalid_status(self):
        self.add("Ligar para cliente A")
        self.add("Ligar para cliente B")
        self.assertIn("não encontrada", tools.mudar_status_tarefa("Inexistente", "done")["mensagem"])
        self.assertIn("Mais de uma", tools.mudar_status_tarefa("ligar", "done")["mensagem"])
        self.assertEqual(tools.mudar_status_tarefa("Ligar para cliente A", "talvez")["resultado"], "erro")


class DeleteTaskTest(ToolTestCase):
    def test_deletes_card(self):
        self.add("Apagar isto")
        result = tools.excluir_tarefa("apagar isto")
        self.assertEqual(result["resultado"], "sucesso")
        self.assertEqual(self.board.cards, [])

    def test_delete_not_found_and_ambiguous_keep_cards(self):
        self.add("Estudar Python")
        self.add("Estudar Gemini")
        self.assertEqual(tools.excluir_tarefa("Inexistente")["resultado"], "erro")
        self.assertEqual(tools.excluir_tarefa("estudar")["resultado"], "erro")
        self.assertEqual(len(self.board.cards), 2)


class ErrorHandlingTest(ToolTestCase):
    def test_tools_never_raise(self):
        failures = [
            config.TaskAgentError("Board 'X' não encontrado"),
            RequestsConnectionError("offline"),
            RuntimeError("falha inesperada"),
        ]
        calls = [
            lambda: tools.adicionar_tarefa("T", "", ""),
            lambda: tools.listar_tarefas(),
            lambda: tools.mudar_status_tarefa("T", "done"),
            lambda: tools.excluir_tarefa("T"),
        ]
        # logger.exception é silenciado só para não poluir a saída do teste.
        with patch.object(tools.logger, "exception"):
            for exc in failures:
                with patch.object(tools, "get_board", side_effect=exc):
                    for call in calls:
                        result = call()
                        self.assertEqual(result["resultado"], "erro", (exc, result))
                        self.assertTrue(result["mensagem"])

    def test_connection_error_message(self):
        with patch.object(tools, "get_board", side_effect=RequestsConnectionError("x")):
            self.assertIn("conexão", tools.listar_tarefas()["mensagem"])


class ConfigTest(unittest.TestCase):
    def test_validate_env_lists_missing_vars(self):
        with patch.dict(os.environ, {"TRELLO_TOKEN": "", "TRELLO_BOARD_NAME": "  "}):
            with self.assertRaises(config.TaskAgentError) as ctx:
                config.validate_env()
        self.assertIn("TRELLO_TOKEN", str(ctx.exception))
        self.assertIn("TRELLO_BOARD_NAME", str(ctx.exception))

    def test_no_credentials_hardcoded(self):
        """Garante que o código-fonte não contém credenciais: só lê de os.environ/os.getenv."""
        import pathlib
        src = pathlib.Path(tools.__file__).parent
        for path in src.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"(api_key|token|api_secret)\s*=\s*['\"][A-Za-z0-9]{20,}", path.name)


if __name__ == "__main__":
    unittest.main()
