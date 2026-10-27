import io
import os
import unittest
import urllib.error
from unittest.mock import patch, MagicMock
from companion import api, KapowarrAPIError

class APIErrorTests(unittest.TestCase):
    def invoke(self, error, route='volumes/114'):
        opener = MagicMock()
        opener.open.side_effect = error
        with patch.dict(os.environ, {'KAPOWARR_API_KEY': 'SECRET', 'KAPOWARR_URL': 'http://kapowarr:5656'}), patch('urllib.request.build_opener', return_value=opener):
            with self.assertRaises(KapowarrAPIError) as caught:
                api(route)
        message = str(caught.exception)
        self.assertNotIn('SECRET', message)
        self.assertNotIn('api_key', message)
        return message

    def test_http_status_and_route_without_secrets(self):
        for code in [301, 401, 403, 404, 429, 500, 503]:
            error = urllib.error.HTTPError('http://server?api_key=SECRET', code, 'SECRET', {}, io.BytesIO(b'SECRET'))
            msg = self.invoke(error)
            self.assertIn(f'HTTP {code}', msg)
            self.assertIn('GET /api/volumes/114', msg)

    def test_unknown_route_redacted(self):
        msg = self.invoke(urllib.error.URLError('SECRET'), 'volumes?api_key=SECRET')
        self.assertIn('[redacted-route]', msg)

    def test_network_and_json_errors(self):
        for error in [TimeoutError('SECRET'), urllib.error.URLError('SECRET'), ValueError('SECRET')]:
            self.invoke(error)

if __name__ == '__main__': unittest.main()
