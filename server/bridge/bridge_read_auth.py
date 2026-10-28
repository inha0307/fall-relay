"""V2 read boundary: only the authenticated web proxy may read legacy APIs."""

import hmac
import json


class BridgeReadAuth:
    def __init__(self, app, secret):
        self.app, self.secret = app, secret

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if (
            scope['type'] == 'http'
            and scope['method'] == 'GET'
            and path.startswith('/api/')
            and not path.startswith(('/api/alerts', '/api/edge_event/'))
        ):
            headers = dict(scope.get('headers', []))
            supplied = headers.get(b'x-write-token', b'').decode(errors='replace')
            if not self.secret or not hmac.compare_digest(supplied, self.secret):
                body = json.dumps({'error': 'authenticated web proxy required'}).encode()
                await send(
                    {'type': 'http.response.start', 'status': 403, 'headers': [(b'content-type', b'application/json')]}
                )
                await send({'type': 'http.response.body', 'body': body})
                return
        await self.app(scope, receive, send)
