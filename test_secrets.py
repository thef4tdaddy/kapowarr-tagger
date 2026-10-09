import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from companion import secret

class SecretTests(unittest.TestCase):
    def test_direct_environment(self):
        for name in ('KAPOWARR_API_KEY','KAVITA_API_KEY','WEBHOOK_TOKEN'):
            with patch.dict(os.environ,{name:' test-value '},clear=True):
                self.assertEqual(secret(name),'test-value')
    def test_file_precedence(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'key';p.write_text(' file-value\n')
            with patch.dict(os.environ,{'KAVITA_API_KEY':'env-value','KAVITA_API_KEY_FILE':str(p)},clear=True):
                self.assertEqual(secret('KAVITA_API_KEY'),'file-value')
                p.unlink()
                with self.assertRaises(FileNotFoundError): secret('KAVITA_API_KEY')
    def test_missing_and_optional(self):
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(ValueError): secret('KAPOWARR_API_KEY')
            self.assertEqual(secret('WEBHOOK_TOKEN',required=False),'')
