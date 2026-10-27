"""Optional, best-effort Discord notices. Never log webhook credentials."""
import hashlib
import json
import logging
from pathlib import Path
import urllib.request
from companion import secret

LOG = logging.getLogger('tagger')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

class DiscordNotifier:
    def __init__(self, directory):
        self.path = Path(directory) / 'discord-state.json'
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}

    def send(self, content):
        url = secret('DISCORD_WEBHOOK_URL', required=False)
        if not url:
            return False
        # No mentions, no full filesystem paths, and no exception/response bodies.
        payload = {'content': content[:1900], 'allowed_mentions': {'parse': []}}
        request = urllib.request.Request(url, data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json', 'User-Agent': 'kapowarr-tagger'}, method='POST')
        with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
            if response.status not in (200, 204):
                raise ValueError('Unexpected Discord status')
        return True

    def update(self, report=None, error=None):
        """Delivery problems never interrupt tagging. Dedupe survives restarts."""
        try:
            self._update(report, error)
        except Exception as exc:
            # HTTPError strings contain the secret URL; only expose the type.
            if hasattr(exc, 'close'):
                exc.close()
            LOG.warning('Discord notification failed (%s); tagging continues', type(exc).__name__)

    def _update(self, report, error):
        state = dict(self.state)
        lines = []
        if error:
            if state.get('error') == error:
                return
            lines = ['Kapowarr Tagger: processing failed', error, 'Automatic retry is scheduled.']
            state['error'] = error
        else:
            if state.pop('error', None):
                lines.append('Kapowarr Tagger: processing recovered')
            rows = report['files']
            changed = [r for r in rows if r['status'] in ('written', 'converted')]
            attention = sorted(hashlib.sha256((r['file'] + r['status']).encode()).hexdigest()
                               for r in rows if r['status'] in ('review', 'unmapped'))
            new_attention = len(set(attention) - set(state.get('attention', [])))
            state['attention'] = attention
            if changed:
                lines.append('Kapowarr Tagger: changes completed (' + report['mode'] + ')')
                for row in changed[:8]:
                    lines.append(row['status'] + ': ' + Path(row['file']).name[:140])
                if len(changed) > 8:
                    lines.append('… and ' + str(len(changed) - 8) + ' more files')
            if new_attention:
                lines.append(f'{new_attention} new items need attention; see report.json (review/unmapped).')
            connection = report.get('kavita_connection', {}).get('status', 'unknown')
            scan = report.get('scan_request', 'not_requested')
            problem = connection if connection != 'connected' else ('scan_retry' if scan == 'pending_retry' else '')
            if problem and problem != state.get('kavita_problem'):
                lines.append('Kavita needs attention: ' + problem)
            elif not problem and state.get('kavita_problem'):
                lines.append('Kavita connection/scan retry recovered.')
            state['kavita_problem'] = problem
            if scan == 'accepted':
                lines.append('Kavita scan request accepted; import completion is not confirmed.')
            elif changed:
                lines.append('Kavita scan: ' + scan)
        if lines and not self.send('\n'.join(lines)):
            return
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(state))
        temp.replace(self.path)
        self.state = state
