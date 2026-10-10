import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch
from service import check_kavita

class Response(io.BytesIO):
    status=200

class KavitaTests(unittest.TestCase):
    def test_authenticated_get(self):
        with patch.dict(os.environ,{'KAVITA_URL':'http://kavita:5000','KAVITA_API_KEY':'private-key'},clear=True), patch('service.urllib.request.build_opener') as build:
            build.return_value.open.return_value=Response(b'[{"id":1}]')
            self.assertEqual(check_kavita(),{'status':'connected','accessible_libraries':1})
            request=build.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(),'GET')
            self.assertEqual(request.full_url,'http://kavita:5000/api/Library/libraries')
            self.assertEqual(request.get_header('X-api-key'),'private-key')
            self.assertIsNone(request.data)
    def test_auth_failure_redacted(self):
        with patch.dict(os.environ,{'KAVITA_URL':'http://kavita:5000','KAVITA_API_KEY':'private-key'},clear=True), patch('service.urllib.request.build_opener') as build:
            build.return_value.open.side_effect=urllib.error.HTTPError('secret-url',401,'private-key',{},None)
            self.assertEqual(check_kavita(),{'status':'authentication_failed','http_status':401})
    def test_missing_config(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(check_kavita()['status'],'configuration_error')
    def test_wrong_service(self):
        with patch.dict(os.environ,{'KAVITA_URL':'http://kavita:5000','KAVITA_API_KEY':'private-key'},clear=True), patch('service.urllib.request.build_opener') as build:
            build.return_value.open.return_value=Response(b'{}')
            self.assertEqual(check_kavita()['status'],'invalid_response')
