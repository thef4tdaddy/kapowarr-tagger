import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from discord_notify import DiscordNotifier, retention_embed, processing_embed, COLORS

class DiscordTests(unittest.TestCase):
    def report(self, rows=(), scan='not_requested'):
        return {'mode':'write','files':list(rows),'scan_request':scan,
                'kavita_connection':{'status':'connected'}}

    def test_error_dedup_restart_and_recovery(self):
        with tempfile.TemporaryDirectory() as d, patch.object(DiscordNotifier,'send',return_value=True) as send:
            n=DiscordNotifier(d)
            n.update(error='HTTP 401')
            DiscordNotifier(d).update(error='HTTP 401')
            self.assertEqual(send.call_count,1)
            n.update(report=self.report())
            self.assertIn('recovered',send.call_args.args[0])
            n.update(report=self.report())
            self.assertEqual(send.call_count,2)

    def test_changes_attention_and_scan(self):
        with tempfile.TemporaryDirectory() as d, patch.object(DiscordNotifier,'send',return_value=True) as send:
            n=DiscordNotifier(d)
            rows=[{'file':'/private/comics/book.cbz','status':'written'},
                  {'file':'/private/comics/review.cbz','status':'review'}]
            n.update(report=self.report(rows,'accepted'))
            message=send.call_args.args[0]
            self.assertIn('book.cbz',message)
            self.assertNotIn('/private',message)
            self.assertIn('1 new items',message)
            self.assertIn('not confirmed',message)
            n.update(report=self.report(rows[1:]))
            self.assertEqual(send.call_count,1)

    def test_failure_isolated_and_retried(self):
        with tempfile.TemporaryDirectory() as d, patch.object(DiscordNotifier,'send',side_effect=ValueError('secret-url')):
            n=DiscordNotifier(d)
            with self.assertLogs('tagger',level='WARNING') as logs:
                n.update(error='HTTP 401')
            self.assertNotIn('secret-url',' '.join(logs.output))
            self.assertNotIn('error',n.state)

    def test_mentions_and_length(self):
        with tempfile.TemporaryDirectory() as d, patch('discord_notify.secret',return_value='https://discord.com/api/webhooks/test'), patch('discord_notify.urllib.request.build_opener') as opener:
            response=MagicMock(); response.__enter__.return_value.status=204
            opener.return_value.open.return_value=response
            DiscordNotifier(d).send('@everyone'+'x'*3000)
            request=opener.return_value.open.call_args.args[0]
            payload=json.loads(request.data)
            self.assertEqual(payload['allowed_mentions'],{'parse':[]})
            self.assertNotIn('content', payload)
            self.assertLessEqual(len(payload['embeds'][0]['description']),2500)

    def test_countdown_cards_have_date_keep_action_and_preview_label(self):
        event = {'file': 'Green Lantern.cbz', 'status': 'three_days', 'mode': 'write', 'due': 1800000000}
        card = retention_embed(event, 3)
        self.assertEqual(card['color'], COLORS['warning'])
        self.assertIn('<t:1800000000:f>', json.dumps(card))
        self.assertIn('Keep', json.dumps(card))
        event['mode'] = 'preview'
        card = retention_embed(event, 3)
        self.assertIn('Preview', card['title'])
        self.assertIn('Deletion is disabled', json.dumps(card))
    def test_processing_card_is_bounded_and_keeps_scan_caveat(self):
        rows = [{'file':'/private/'+'x'*2000+'.cbz','status':'converted'} for _ in range(100)]
        card = processing_embed(self.report(rows, 'accepted'), None, [])
        self.assertNotIn('/private/', json.dumps(card))
        self.assertIn('not confirmed', json.dumps(card))
        self.assertLess(len(json.dumps(card)),6000)
    def test_error_card_is_red(self):
        self.assertEqual(processing_embed(None, 'HTTP 401', [])['color'], COLORS['error'])

if __name__=='__main__': unittest.main()
