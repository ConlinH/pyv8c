"""The script and Trusted Types directives in a document's policy container."""


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

    def allows_inline(self, nonce=''):
        for policy in self.policies:
            source = policy.get('script-src-elem', policy.get('script-src', policy.get('default-src')))
            if source is None:
                continue
            if nonce and f"'nonce-{nonce}'" in source:
                continue
            if "'unsafe-inline'" in source and not any(
                    item.startswith(("'nonce-", "'sha256-", "'sha384-", "'sha512-")) for item in source):
                continue
            return False
        return True
