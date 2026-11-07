import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import launcher


class LauncherTests(unittest.TestCase):
    def test_check_and_no_pause_are_noninteractive(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(launcher,'update_game',return_value=[]) as update, patch('builtins.input',side_effect=AssertionError('unexpected prompt')),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(launcher.main(['--game-dir',directory,'--check']),0)
                self.assertTrue(update.call_args.kwargs['check'])

    def test_picker_selection_requires_confirmation_before_install(self):
        with patch.object(launcher,'default_game_dir',return_value=None),patch.object(launcher,'choose_game_dir',return_value=Path('chosen')),patch('builtins.input',return_value='n'),patch.object(launcher,'update_game') as update,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(['--no-pause']),0)
            update.assert_not_called()

    def test_error_prints_message_and_returns_failure_without_traceback(self):
        with patch.object(launcher,'update_game',side_effect=RuntimeError('Close the running game')),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(launcher.main(['--game-dir','chosen','--no-pause']),1)
        self.assertIn('Close the running game',output.getvalue())
        self.assertNotIn('Traceback',output.getvalue())


if __name__=='__main__':unittest.main()
