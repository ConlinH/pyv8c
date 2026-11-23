"""The script and Trusted Types directives in a document's policy container."""

import base64
import hashlib
import re


_HASH_SOURCE = re.compile(r"'(sha256|sha384|sha512)-([a-zA-Z0-9+/_-]+={0,2})'", re.I)


class ContentPolicy:
    def __init__(self, inherited=None):
        self.policies = list(inherited.policies) if inherited is not None else []

    def add(self, text):
        directives = {}
        for directive in str(text).split(';'):
            parts = directive.strip().split()
            if parts and parts[0].lower() not in directives:
                directives[parts[0].lower()] = tuple(parts[1:])
        if directives:
            self.policies.append(directives)

    @property
    def require_trusted_types(self):
        return any("'script'" in policy.get('require-trusted-types-for', ())
                   for policy in self.policies)

    @property
    def allows_eval(self):
        return all("'unsafe-eval'" in source for policy in self.policies
                   if (source := policy.get('script-src', policy.get('default-src'))) is not None)

    def allows_policy(self, name, exists=False):
        for policy in self.policies:
            allowed = policy.get('trusted-types')
            if allowed is None:
                continue
            if name not in allowed and '*' not in allowed:
                return False
            if exists and "'allow-duplicates'" not in allowed:
                return False
        return True

    def allows_inline(self, nonce='', source_text=None):
        digests = {}
        encoded = None
        for policy in self.policies:
            source = policy.get('script-src-elem', policy.get('script-src', policy.get('default-src')))
            if source is None:
                continue
            if nonce and f"'nonce-{nonce}'" in source:
                continue
            hashes = [match for item in source if (match := _HASH_SOURCE.fullmatch(item))]
            matched = False
            if source_text is not None:
                for match in hashes:
                    algorithm = match[1].lower()
                    if algorithm not in digests:
                        if encoded is None:
                            try:
                                encoded = source_text.encode('utf-8')
                            except UnicodeEncodeError:
                                encoded = source_text.encode('utf-16-le', 'surrogatepass').decode(
                                    'utf-16-le', 'replace').encode('utf-8')
                        digests[algorithm] = base64.b64encode(
                            hashlib.new(algorithm, encoded).digest()).decode('ascii').rstrip('=')
                    expected = match[2].replace('-', '+').replace('_', '/').rstrip('=')
                    if digests[algorithm] == expected:
                        matched = True
                        break
            if matched:
                continue
            if "'unsafe-inline'" in source and not any(
                    item.startswith("'nonce-") for item in source) and not hashes:
                continue
            return False
        return True
