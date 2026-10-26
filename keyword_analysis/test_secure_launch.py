"""Fake credentials only. Hidden-input and lifecycle tests never call the Naver API."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import getpass
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from keyword_analysis import secure_launch
from keyword_analysis.trends import KEY_ENV


class TtyBuffer(io.StringIO):
    def isatty(self):
        return True


class SecureLaunchTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, dict(zip(KEY_ENV, ['inherited-id', 'inherited-secret']))))
        self.output = TtyBuffer()
        self.stack.enter_context(patch('sys.stdout', self.output))
        self.stack.enter_context(patch('sys.stdin', TtyBuffer()))
        self.available = self.stack.enter_context(patch.object(secure_launch, 'port_available', return_value=True))
        self.reader = self.stack.enter_context(patch.object(getpass, 'getpass', side_effect=['test-only-id', 'test-only-secret']))
        self.launch = self.stack.enter_context(patch.object(secure_launch, 'launch'))

    def tearDown(self):
        for name in KEY_ENV:
            self.assertNotIn(name, os.environ)
        for secret in ('inherited-id', 'inherited-secret', 'test-only-id', 'test-only-secret'):
            self.assertNotIn(secret, self.output.getvalue())

    def test_hidden_prompts_and_environment_only_same_process_handoff(self):
        captured = []
        def server(**kwargs):
            captured.append(tuple(os.environ[name] for name in KEY_ENV))
            self.assertEqual(kwargs, dict(port=8766, open_browser=True, demo=False, require_fresh=True))
        self.launch.side_effect = server
        self.assertEqual(secure_launch.run(), 0)
        self.assertEqual(captured, [('test-only-id', 'test-only-secret')])
        self.assertEqual(self.reader.call_count, 2)
        self.assertTrue(all('hidden' in c.args[0] for c in self.reader.call_args_list))

    def test_demo_kept_without_input_or_credentials(self):
        self.assertEqual(secure_launch.run(demo=True), 0)
        self.reader.assert_not_called()
        self.launch.assert_called_once_with(port=8766, open_browser=True, demo=True, require_fresh=True)

    def test_missing_console_fails_closed_without_read_or_launch(self):
        with patch('sys.stdin', io.StringIO()):
            self.assertEqual(secure_launch.run(), 1)
        self.reader.assert_not_called(); self.launch.assert_not_called()

    def test_hidden_input_fallback_warning_aborts_instead_of_echo(self):
        import warnings
        def insecure_fallback(prompt):
            warnings.warn('fallback', getpass.GetPassWarning)
            self.fail('Must not continue to echoing fallback')
        self.reader.side_effect = insecure_fallback
        self.assertEqual(secure_launch.run(), 1)
        self.launch.assert_not_called()

    def test_cancellation_at_either_prompt_does_not_start(self):
        for responses in [[KeyboardInterrupt()], ['test-only-id', EOFError()]]:
            self.reader.side_effect = responses
            self.assertEqual(secure_launch.run(), 130)
            self.launch.assert_not_called()

    def test_empty_or_header_injection_rejected_without_launch(self):
        for values in [('', 'test-only-secret'), ('test-only-id', 'bad\nsecret')]:
            self.reader.side_effect = values
            self.assertEqual(secure_launch.run(), 1)
        self.launch.assert_not_called()

    def test_existing_server_rejected_before_secret_input(self):
        self.available.return_value = False
        self.assertEqual(secure_launch.run(), 1)
        self.reader.assert_not_called(); self.launch.assert_not_called()

    def test_launch_failure_redacted_and_environment_removed(self):
        self.launch.side_effect = RuntimeError('test-only-secret')
        self.assertEqual(secure_launch.run(), 1)

    def test_cli_never_echoes_unknown_arguments(self):
        for args in [['--secret', 'test-only-secret'], ['--port', 'test-only-secret']]:
            # argparse error must not repeat even accidental command-line secret values.
            with redirect_stderr(self.output), self.assertRaises(SystemExit) as error:
                secure_launch.main(args)
            self.assertEqual(error.exception.code, 2)
        for name in KEY_ENV:
            os.environ.pop(name, None)  # parser exits before run(), which has no input yet.
        self.reader.assert_not_called(); self.launch.assert_not_called()

    def test_bat_has_no_powershell_policy_override_or_secret_arguments(self):
        path = Path(__file__).resolve().parents[1] / 'start_keyword_analysis_secure.bat'
        text = path.read_text()
        self.assertIn('-B -m keyword_analysis.secure_launch', text)
        for forbidden in ['setx ', 'ExecutionPolicy', 'CLIENT_SECRET', 'CLIENT_ID', '-EncodedCommand']:
            self.assertNotIn(forbidden, text)
        for name in KEY_ENV:
            os.environ.pop(name, None)


if __name__ == '__main__':
    unittest.main()
