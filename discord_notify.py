"""Optional, best-effort Discord notices. Never log webhook credentials."""
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import urllib.request
from companion import secret

LOG = logging.getLogger('tagger')

COLORS = {'info': 0x5865F2, 'success': 0x2ECC71, 'warning': 0xF1C40F,
          'error': 0xE74C3C, 'preview': 0x9B59B6, 'deleted': 0x95A5A6}


def embed(title, description='', kind='info', fields=()):
    # Conservative bounds keep the complete embed below Discord's 6000-char limit.
    return {'title': title[:200], 'description': description[:2500],
            'color': COLORS[kind],
            'fields': [{'name': str(name)[:100], 'value': str(value)[:600] or '—',
                        'inline': inline} for name, value, inline in list(fields)[:4]],
            'footer': {'text': 'Kapowarr Tagger • Comic library companion'},
            'timestamp': datetime.now(timezone.utc).isoformat()}


def processing_embed(report, error, lines):
    if error:
        return embed('❌ Processing paused', error, 'error',
                     [('Next step', 'Automatic retry is scheduled. Check the tagger logs.', False)])
    changed = [r for r in report['files'] if r['status'] in ('written', 'converted')]
    warning = any('attention' in line for line in lines)
    recovered = any('recovered' in line for line in lines)
    title = '⚠️ Library needs attention' if warning else ('✅ Processing recovered' if recovered else '📚 Library updated')
    fields = []
    if changed:
        fields += [('Metadata updated', sum(r['status'] == 'written' for r in changed), True),
                   ('CBR → CBZ', sum(r['status'] == 'converted' for r in changed), True)]
        books = '\n'.join('• ' + Path(r['file']).name.replace('`', '').replace('\n', ' ')[:110] for r in changed[:4])
        if len(changed) > 4:
            books += f"\n… and {len(changed)-4} more"
        fields.append(('Books processed', books, False))
    scan = report.get('scan_request', 'not_requested')
    scan_label = {'accepted': 'Scan queued • import completion not confirmed',
                  'pending_retry': 'Scan needs retry', 'not_requested': 'No scan requested'}.get(scan, scan)
    fields.append(('Kavita', scan_label, False))
    details = '\n'.join(line for line in lines if not line.startswith(('Kapowarr Tagger:', 'written:', 'converted:', '…', 'Kavita scan')))
    preview = report.get('mode') == 'preview'
    if preview:
        title = '🧪 Preview • ' + title
        details = 'No comic files changed.\n' + details
    return embed(title, details, 'preview' if preview else ('warning' if warning else 'success'), fields)


def retention_embed(event, remaining=None):
    status = event['status']
    preview = event.get('mode') == 'preview'
    book = event['file'].replace('`', '').replace('\n', ' ')[:300]
    if status in ('countdown_started', 'three_days'):
        due = int(event['due'])
        title = '⏳ Cleanup countdown started' if status == 'countdown_started' else '⚠️ Cleanup reminder'
        kind = 'info' if status == 'countdown_started' else 'warning'
        fields = [('Time remaining', f'{remaining} days', True),
                  ('Eligible on' if preview else 'Scheduled deletion', f'<t:{due}:f>\n<t:{due}:R>', True),
                  ('Keep this book', 'Add it to your **Keep** reading list to cancel cleanup.', False)]
        if preview:
            title = '🧪 Preview • ' + title
            fields.append(('Preview only', 'Deletion is disabled.', False))
        return embed(title, book, 'preview' if preview else kind, fields)
    if status.startswith('deleted'):
        return embed('🗑️ Book deleted', book, 'deleted', [
            ('History', 'Saved to historical-log.json before deletion.', False)])
    return embed('✅ Reading progress restored', book, 'success', [
        ('Completion', 'Marked read after an exact reimport match.', False),
        ('Original sessions', 'Preserved in the JSON history log.', False)])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

class DiscordNotifier:
    def __init__(self, directory):
        self.path = Path(directory) / 'discord-state.json'
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}

    def send(self, content, *, card=None):
        url = secret('DISCORD_WEBHOOK_URL', required=False)
        if not url:
            return False
        # No mentions, no full filesystem paths, and no exception/response bodies.
        payload = {'embeds': [card or embed('📚 Kapowarr Tagger', content)],
                   'allowed_mentions': {'parse': []}}
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
        if lines and not self.send('\n'.join(lines), card=processing_embed(report, error, lines)):
            return
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(state))
        temp.replace(self.path)
        self.state = state
