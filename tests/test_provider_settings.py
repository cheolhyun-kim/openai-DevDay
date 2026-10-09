import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from devday.providers import OpenAIProvider, _openai_api_key


class LocalKeyTests(unittest.TestCase):
    def test_local_file_key_reaches_sdk_and_overrides_previous_terminal_key(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'settings.py'
            path.write_text('OPENAI_API_KEY = " local-test-key "\n', encoding='utf-8')
            with patch('devday.providers.LOCAL_SETTINGS', path), \
                    patch.dict(os.environ, {'OPENAI_API_KEY': 'old-terminal-key'}), \
                    patch('openai.OpenAI') as client:
                OpenAIProvider()
                client.assert_called_once_with(api_key='local-test-key', timeout=180, max_retries=0)

    def test_empty_or_missing_file_falls_back_to_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'settings.py'
            with patch('devday.providers.LOCAL_SETTINGS', path), \
                    patch.dict(os.environ, {'OPENAI_API_KEY': 'environment-test-key'}):
                self.assertEqual(_openai_api_key(), 'environment-test-key')
                path.write_text('OPENAI_API_KEY = ""\n', encoding='utf-8')
                self.assertEqual(_openai_api_key(), 'environment-test-key')

    def test_settings_error_does_not_expose_file_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'settings.py'
            path.write_text('raise RuntimeError("test-secret-do-not-print")\n', encoding='utf-8')
            with patch('devday.providers.LOCAL_SETTINGS', path):
                with self.assertRaises(RuntimeError) as error:
                    _openai_api_key()
                self.assertNotIn('test-secret-do-not-print', str(error.exception))
                self.assertTrue(error.exception.__suppress_context__)

    def test_missing_key_explains_local_file_option(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'settings.py'
            with patch('devday.providers.LOCAL_SETTINGS', path), \
                    patch.dict(os.environ, {'OPENAI_API_KEY': ''}):
                with self.assertRaisesRegex(RuntimeError, 'settings.py'):
                    _openai_api_key()


if __name__ == '__main__':
    unittest.main()
