"""Compact result transport; only a matching server acknowledgement permits omission."""

import time


class ResultTransport:
    def __init__(self, identity, refresh_s=60.0, full_identity=False):
        self.identity = identity
        self.refresh_s = refresh_s
        self.full_identity = full_identity
        self.registered_at = None

    def send(self, session, url, result, headers):
        now = time.monotonic()
        full = self.full_identity or self.registered_at is None or now - self.registered_at >= self.refresh_s
        body = {k: v for k, v in result.items() if k != 'model_identity'}
        body['model_run_id'] = self.identity['run_id']
        if full:
            body['model_identity'] = self.identity
        response = session.post(url, json=body, headers=headers, timeout=(2, 3))
        if response.status_code == 409 and response.json().get('code') == 'model_identity_required':
            self.registered_at = None
            body['model_identity'] = self.identity
            response = session.post(url, json=body, headers=headers, timeout=(2, 3))
            full = True
        response.raise_for_status()
        reply = response.json()
        if reply.get('ok') is not True or reply.get('model_run_id') != self.identity['run_id']:
            self.registered_at = None  # old servers never authorize omission
        elif full:
            self.registered_at = now
        return response
