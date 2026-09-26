from unittest.mock import patch, MagicMock
from urllib.request import Request
from django.test import SimpleTestCase
from .http_client import SafeHttpError
from .program_network import approved_source_url, fetch_source_url, source_proxy, SourceRedirect, NAS_PROXY


class ProgramNetworkTests(SimpleTestCase):
    def test_proxy_destination_is_fixed_by_deployment(self):
        with patch.dict('os.environ', {'PROGRAM_SOURCE_PROXY': 'http://unreviewed.example:7890'}):
            with self.assertRaises(SafeHttpError):
                source_proxy()

    def test_source_and_redirect_allowlist(self):
        for url in ['http://www.youtube.com/', 'https://127.0.0.1/',
                    'https://www.youtube.com.evil.test/', 'https://user@www.youtube.com/',
                    'https://www.youtube.com:8443/']:
            with self.subTest(url=url), self.assertRaises(SafeHttpError):
                approved_source_url(url)
        with self.assertRaises(SafeHttpError):
            SourceRedirect().redirect_request(Request('https://www.dwarkesh.com/feed'), None, 302, '', {}, 'https://127.0.0.1/')

    @patch('intelligence.program_network.direct_fetch')
    def test_default_uses_existing_public_network_checks(self, direct):
        with patch.dict('os.environ', {'PROGRAM_SOURCE_PROXY': ''}):
            fetch_source_url('https://www.dwarkesh.com/feed')
        direct.assert_called_once()

    @patch('intelligence.program_network.build_opener')
    def test_proxy_keeps_size_limit_and_uses_fixed_https_host(self, opener):
        response = MagicMock(status=200)
        response.geturl.return_value = 'https://www.dwarkesh.com/feed'
        response.read.return_value = b'12345'
        opener.return_value.open.return_value.__enter__.return_value = response
        with patch.dict('os.environ', {'PROGRAM_SOURCE_PROXY': NAS_PROXY}):
            with self.assertRaises(SafeHttpError):
                fetch_source_url('https://www.dwarkesh.com/feed', max_bytes=4)
        response.read.assert_called_once_with(5)
