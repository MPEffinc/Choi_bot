import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

import choi_bot as bot
from bot.settings import Settings, load_settings, validate_settings
from tests.fakes import FakeClient, FakeTree, FakeProvider
from bot.llm.router import LLMRouter, task_policies

ROOT = Path(__file__).resolve().parents[1]


class ContractTests(unittest.TestCase):
    def test_baseline_commands_and_prompts(self):
        baseline = json.loads((ROOT / 'tests/fixtures/legacy_contract.json').read_text())
        tree = ast.parse((ROOT / 'choi_bot.py').read_text())
        commands = []
        for node in tree.body:
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == 'command':
                    commands.append({'function': node.name, 'args': ast.dump(node.args),
                                     'decorators': [ast.dump(d) for d in node.decorator_list]})
        self.assertEqual(commands, baseline['commands'])
        self.assertEqual(len(commands), 20)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == 'generate_content_timeout']
        self.assertCountEqual([ast.dump(n.args[0]) for n in calls], baseline['prompts'])
        self.assertEqual(len(calls), 10)
        self.assertTrue(all(any(k.arg == 'task_type' for k in n.keywords) for n in calls))
        templates = [ast.dump(n.value) for n in ast.walk(tree) if isinstance(n, ast.Assign)
                     and any(isinstance(x, ast.Name) and x.id in ('CHARACTER_PROMPT', 'prompt', 'final_prompt')
                             for x in n.targets)]
        normalized = [value.replace("Name(id='source_text', ctx=Load())", "Attribute(value=Name(id='self', ctx=Load()), attr='message', ctx=Load())")
                      .replace("Name(id='target_lang', ctx=Load())", "Attribute(value=Name(id='self', ctx=Load()), attr='target_lang', ctx=Load())")
                      for value in templates]
        self.assertCountEqual(normalized, baseline['templates'])
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse(any(n.name.startswith('google') for n in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or '').startswith('google'))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotEqual(node.func.attr, 'generate_content')

    def test_import_has_no_runtime_side_effects(self):
        code = '''
import os, socket, sys
from unittest.mock import patch
import discord
from discord.ext import commands
with patch.object(discord.Client, '__init__', side_effect=AssertionError('client on import')), \\
     patch.object(socket.socket, 'connect', side_effect=AssertionError('network')):
    import choi_bot
    assert choi_bot.client is None and choi_bot.llm_router is None
    assert 'google.genai' not in sys.modules
    assert not os.listdir('.')
'''
        with tempfile.TemporaryDirectory() as directory:
            env = {'PATH': os.environ['PATH'], 'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1'}
            result = subprocess.run([sys.executable, '-B', '-c', code], cwd=directory,
                                    env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_settings_validation_and_environment_precedence(self):
        for values, error in [({}, 'API KEY'), ({'GOOGLE_API_KEY1': 'fake'}, 'TOKEN'),
                              ({'GOOGLE_API_KEY1': 'fake', 'DISCORD_BOT_TOKEN': ''}, 'TOKEN')]:
            settings = Settings.from_mapping(values)
            with self.assertRaisesRegex(ValueError, error):
                validate_settings(settings)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'GOOGLE_API_KEY2': 'environment'}, clear=True):
            path = Path(directory) / 'test.env'
            path.write_text('GOOGLE_API_KEY2=file\nGOOGLE_API_KEY6=six\nDISCORD_BOT_TOKEN=fake\n')
            settings = load_settings(path)
            self.assertEqual(settings.api_keys, ('environment', 'six'))
            validate_settings(settings)
            self.assertNotIn('environment', repr(settings))

    def test_initialization_registers_all_commands_without_connecting(self):
        router = LLMRouter({'gemini': FakeProvider()}, task_policies(bot.MODEL))
        with tempfile.TemporaryDirectory() as directory, patch.object(bot, 'LOG_FOLDER', directory):
            client = bot.initialize_runtime(Settings(('fake',), 'fake'), router=router,
                                            client_factory=FakeClient, tree_factory=FakeTree)
        self.assertEqual(len(bot.tree.commands), 20)
        self.assertEqual(set(client.events), {'on_ready', 'on_message'})
        self.assertTrue(client.intents.message_content and client.intents.members and client.intents.presences)
        self.assertEqual(bot.tree.on_error, bot.on_application_command_error)
        role = next(c for c in bot.tree.commands if c.name == '알림')
        self.assertIsNotNone(role._params['role'].autocomplete)
        config = next(c for c in bot.tree.commands if c.name == 'config')
        self.assertTrue(config.checks)

    def test_invalid_settings_precede_client_creation(self):
        factory = Mock()
        with self.assertRaises(ValueError):
            bot.initialize_runtime(Settings((), None), client_factory=factory)
        factory.assert_not_called()

    def test_main_preserves_entrypoint(self):
        # User explicitly approved this model constant change during Phase 1A.
        self.assertEqual(bot.MODEL, 'gemini-3.5-flash-lite')
        settings = Settings(('fake',), 'fake-token')
        client = Mock()
        with patch.object(bot, 'load_settings', return_value=settings), patch.object(bot, 'initialize_runtime', return_value=client) as init:
            bot.main()
        init.assert_called_once_with(settings)
        client.run.assert_called_once_with('fake-token')
        self.assertIn('CMD ["python", "-u", "choi_bot.py"]', (ROOT / 'Dockerfile').read_text())
