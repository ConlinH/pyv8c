"""``window.crypto``: ``getRandomValues`` fills the caller's array in place,
``randomUUID`` is secure-context only, as in Chrome, and ``subtle`` is backed by the
standard library where it can be: digests, HMAC, raw key handling and PBKDF2 are real,
while AES, RSA and the encapsulations reject with ``NotSupportedError`` rather than
answering something that only looks like a result. ``supports`` reports the same set,
which is the question a page is meant to ask.
"""

import hashlib
import hmac as hmac_module
import os
import uuid

from pyv8c import pyv8

from ...idl import interface
from ...foundation.arguments import (MISSING, dictionary, integer, missing_error,
                                     sequence)
from ..page_errors import PageTypeError, dom_exception
from ..page_errors import js_error_value


#: The digest names the standard library backs, and the HMAC hash names as well.
_HASHES = {"SHA-1": "sha1", "SHA-256": "sha256", "SHA-384": "sha384", "SHA-512": "sha512"}

#: One ``getRandomValues`` call may ask for at most this many bytes.
_RANDOM_QUOTA = 65536
#: The symmetric algorithms whose keys are raw bytes.
_RAW_ALGORITHMS = ("HMAC", "AES-CBC", "AES-CTR", "AES-GCM", "AES-KW")
#: The derivation algorithms whose key *is* its material, such as a password.
_PASSWORD_ALGORITHMS = ("PBKDF2",)
#: What each algorithm lets a key be used for; a wrong one is a SyntaxError in Chrome.
_VALID_USAGES = {
    "HMAC": ("sign", "verify"),
    "AES-CBC": ("encrypt", "decrypt", "wrapKey", "unwrapKey"),
    "AES-CTR": ("encrypt", "decrypt", "wrapKey", "unwrapKey"),
    "AES-GCM": ("encrypt", "decrypt", "wrapKey", "unwrapKey"),
    "AES-KW": ("wrapKey", "unwrapKey"),
    "PBKDF2": ("deriveBits", "deriveKey"),
}


def _options(algorithm):
    """An ``AlgorithmIdentifier`` as a plain mapping."""
    if isinstance(algorithm, dict):
        return algorithm
    return dictionary(algorithm)


def _name_of(algorithm):
    """The algorithm name, upper-cased, from a string or a dictionary."""
    if isinstance(algorithm, str):
        return algorithm.upper()
    return str(_options(algorithm).get("name", "")).upper()


def _usages(values):
    if values is MISSING or values is None:
        return ()
    from ...foundation.arguments import is_nullish

    if is_nullish(values):
        return ()
    if isinstance(values, (list, tuple)):
        return tuple(str(usage) for usage in values)
    return tuple(str(usage) for usage in sequence(values, str))


def _bytes_of(value, method, position):
    """The bytes behind a ``BufferSource``: a view, an ArrayBuffer or raw bytes."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, pyv8.JSObject):
        kind = value._buffer_kind()
        if kind in ('buffer', 'nontransferable', 'view', 'detached'):
            return value._bytes()
        if kind == 'shared-view':
            raise PageTypeError(f"Failed to execute '{method}' on 'SubtleCrypto': "
                                "The provided ArrayBufferView value must not be shared.")
    raise PageTypeError(f"Failed to execute '{method}' on 'SubtleCrypto': parameter"
                        f" {position} is not of type 'BufferSource'.")


def _checked_usages(name, usages):
    """The usages, or the ``SyntaxError`` Chrome raises for a set it forbids."""
    allowed = _VALID_USAGES.get(name, ())
    if not usages:
        raise dom_exception("SyntaxError", "Usages cannot be empty when creating a key.")
    for usage in usages:
        if usage not in allowed:
            raise dom_exception("SyntaxError", f"Unsupported key usage for a {name} key:"
                                               f" '{usage}'")
    return usages


def _unsupported(method, name):
    return dom_exception("NotSupportedError", f"Unsupported operation: '{name}' is not"
                                               f" implemented for '{method}'.")


def _hmac_algorithm(options):
    """The ``HmacKeyAlgorithm`` a key reports: its hash and its length in bits."""
    raw = options.get("hash")
    hash_name = _name_of(raw) if raw is not None else "SHA-256"
    length = int(options.get("length", 0) or 0)
    return {"name": "HMAC", "hash": {"name": hash_name}, "length": length}


@interface('CryptoKey', None)
class CryptoKey:
    """One key, with the algorithm it was made for and what it may be used for."""

    def __init__(self, key_type="secret", extractable=True, algorithm=None, usages=(),
                 material=b"", **_):
        self._type = str(key_type)
        self._extractable = bool(extractable)
        self._algorithm = dict(algorithm or {})
        self._usages = tuple(str(usage) for usage in usages)
        self._material = bytes(material)

    def get_type(self): return self._type
    def get_extractable(self): return self._extractable
    def get_algorithm(self): return dict(self._algorithm)
    def get_usages(self): return list(self._usages)


@interface('SubtleCrypto', None)
class SubtleCrypto:
    """The cryptographic operations this build really performs."""

    __idl_overrides__ = {
        "interface": {
            "length": 0
        }
    }

    def __init__(self, services=None, **_):
        self._services = services

    def _settle(self, value=None, error=None):
        realm = getattr(self._services, "realm_services", None)
        if realm is None:
            if error is not None:
                raise error
            return value
        promise, resolve, reject = realm.make_promise()
        if error is None:
            resolve(value)
        else:
            reject(js_error_value(error))
        return promise

    def _key_for(self, key, operation):
        """The key, when it is one of ours and is allowed *operation*."""
        if not isinstance(key, CryptoKey):
            return None, _unsupported(operation, "key")
        if operation and key._usages and operation not in key._usages:
            return None, dom_exception(
                "InvalidAccessError",
                f"The key does not support the '{operation}' operation.")
        return key, None

    # -- digests ---------------------------------------------------------------
    def fn_digest(self, algorithm=MISSING, data=MISSING, *_extra):
        if algorithm is MISSING:
            raise missing_error('SubtleCrypto', 'digest', 2, 0, constructor=False)
        if data is MISSING:
            raise missing_error('SubtleCrypto', 'digest', 2, 1, constructor=False)
        name = _name_of(algorithm)
        if name not in _HASHES:
            return self._settle(error=dom_exception(
                "NotSupportedError", f"Unrecognized algorithm name '{name}'."))
        return self._settle(hashlib.new(_HASHES[name], _bytes_of(data, 'digest', 2)).digest())

    # -- keys ------------------------------------------------------------------
    def fn_generateKey(self, algorithm=MISSING, extractable=False, usages=MISSING, *_extra):
        if algorithm is MISSING:
            raise missing_error('SubtleCrypto', 'generateKey', 3, 0, constructor=False)
        options = _options(algorithm)
        name = str(options.get("name", "")).upper()
        granted = _usages(usages)
        if name in _VALID_USAGES:
            _checked_usages(name, granted)
        if name == "HMAC":
            length = int(options.get("length", 0) or 0)
            if length <= 0 or length % 8:
                return self._settle(error=dom_exception(
                    "OperationError",
                    "HmacKeyGenParams: length: The length must be a multiple of 8"))
            algorithm_view = _hmac_algorithm(options)
            return self._settle(CryptoKey("secret", bool(extractable), algorithm_view,
                                          granted, os.urandom(length // 8)))
        if name in _RAW_ALGORITHMS:
            length = int(options.get("length", 0) or 0)
            if length not in (128, 192, 256):
                return self._settle(error=dom_exception(
                    "OperationError", f"{name}: The length must be 128, 192 or 256"))
            return self._settle(CryptoKey("secret", bool(extractable),
                                          {"name": name, "length": length}, granted,
                                          os.urandom(length // 8)))
        return self._settle(error=_unsupported('generateKey', name))

    def fn_importKey(self, format=MISSING, key_data=MISSING, algorithm=MISSING,
                     extractable=False, usages=MISSING, *_extra):
        for position, value in enumerate((format, key_data, algorithm)):
            if value is MISSING:
                raise missing_error('SubtleCrypto', 'importKey', 5, position,
                                    constructor=False)
        kind = str(format).lower()
        options = _options(algorithm)
        name = str(options.get("name", "")).upper()
        granted = _usages(usages)
        if kind != "raw" or name not in _RAW_ALGORITHMS + _PASSWORD_ALGORITHMS:
            return self._settle(error=_unsupported('importKey', f"{kind}/{name}"))
        if name in _VALID_USAGES:
            _checked_usages(name, granted)
        material = _bytes_of(key_data, 'importKey', 2)
        if name in _PASSWORD_ALGORITHMS:
            return self._settle(CryptoKey("secret", bool(extractable), {"name": name},
                                          granted, material))
        if name == "HMAC":
            view = _hmac_algorithm(options)
            view["length"] = len(material) * 8
            return self._settle(CryptoKey("secret", bool(extractable), view, granted,
                                          material))
        return self._settle(CryptoKey("secret", bool(extractable),
                                      {"name": name, "length": len(material) * 8},
                                      granted, material))

    def fn_exportKey(self, format=MISSING, key=MISSING, *_extra):
        if format is MISSING:
            raise missing_error('SubtleCrypto', 'exportKey', 2, 0, constructor=False)
        if key is MISSING:
            raise missing_error('SubtleCrypto', 'exportKey', 2, 1, constructor=False)
        kind = str(format).lower()
        if not isinstance(key, CryptoKey):
            raise PageTypeError("Failed to execute 'exportKey' on 'SubtleCrypto':"
                              " parameter 2 is not of type 'CryptoKey'.")
        if kind != "raw":
            return self._settle(error=_unsupported('exportKey', kind))
        if not key._extractable:
            return self._settle(error=dom_exception(
                "InvalidAccessError", "The key is not extractable."))
        return self._settle(key._material)

    # -- signatures ------------------------------------------------------------
    def fn_sign(self, algorithm=MISSING, key=MISSING, data=MISSING, *_extra):
        for position, value in enumerate((algorithm, key, data)):
            if value is MISSING:
                raise missing_error('SubtleCrypto', 'sign', 3, position, constructor=False)
        name = _name_of(algorithm)
        key, error = self._key_for(key, "sign")
        if error is not None:
            return self._settle(error=error)
        view = key.get_algorithm()
        if name == "HMAC" and str(view.get("name", "")).upper() == "HMAC":
            digest = _HASHES.get(str(view.get("hash", {}).get("name", "")).upper())
            if digest is None:
                return self._settle(error=_unsupported('sign', name))
            material = hmac_module.new(key._material, _bytes_of(data, 'sign', 3), digest)
            return self._settle(material.digest())
        return self._settle(error=_unsupported('sign', name))

    def fn_verify(self, algorithm=MISSING, key=MISSING, signature=MISSING, data=MISSING,
                  *_extra):
        for position, value in enumerate((algorithm, key, signature, data)):
            if value is MISSING:
                raise missing_error('SubtleCrypto', 'verify', 4, position, constructor=False)
        name = _name_of(algorithm)
        key, error = self._key_for(key, "verify")
        if error is not None:
            return self._settle(error=error)
        view = key.get_algorithm()
        if name == "HMAC" and str(view.get("name", "")).upper() == "HMAC":
            digest = _HASHES.get(str(view.get("hash", {}).get("name", "")).upper())
            if digest is None:
                return self._settle(error=_unsupported('verify', name))
            expected = hmac_module.new(key._material, _bytes_of(data, 'verify', 4),
                                       digest).digest()
            given = _bytes_of(signature, 'verify', 3)
            return self._settle(hmac_module.compare_digest(expected, given))
        return self._settle(error=_unsupported('verify', name))

    # -- derivation ------------------------------------------------------------
    def fn_deriveBits(self, algorithm=MISSING, base_key=MISSING, length=MISSING, *_extra):
        for position, value in enumerate((algorithm, base_key, length)):
            if value is MISSING:
                raise missing_error('SubtleCrypto', 'deriveBits', 3, position,
                                    constructor=False)
        options = _options(algorithm)
        name = str(options.get("name", "")).upper()
        base_key, error = self._key_for(base_key, "deriveBits")
        if error is not None:
            return self._settle(error=error)
        if (name != "PBKDF2"
                or str(base_key.get_algorithm().get("name", "")).upper() != "PBKDF2"):
            return self._settle(error=_unsupported('deriveBits', name))
        bits = int(integer(length, signed=True))
        if bits <= 0 or bits % 8:
            return self._settle(error=dom_exception(
                "OperationError", "Pbkdf2Params: The length must be a multiple of 8"))
        digest = _HASHES.get(_name_of(options.get("hash")))
        if digest is None:
            return self._settle(error=_unsupported('deriveBits', name))
        salt = _bytes_of(options.get("salt"), 'deriveBits', 1)
        rounds = int(integer(options.get("iterations"), signed=True))
        derived = hashlib.pbkdf2_hmac(digest, base_key._material, salt, rounds, bits // 8)
        return self._settle(derived)

    def fn_deriveKey(self, algorithm=MISSING, base_key=MISSING, derived_key_type=MISSING,
                     extractable=False, usages=MISSING, *_extra):
        for position, value in enumerate((algorithm, base_key, derived_key_type)):
            if value is MISSING:
                raise missing_error('SubtleCrypto', 'deriveKey', 5, position,
                                    constructor=False)
        target = _options(derived_key_type)
        name = str(target.get("name", "")).upper()
        bits = 256 if name == "HMAC" else int(target.get("length", 0) or 0)
        if name not in _RAW_ALGORITHMS or bits not in (128, 192, 256, 384, 512):
            return self._settle(error=_unsupported('deriveKey', name))
        options = _options(algorithm)
        base_key, error = self._key_for(base_key, "deriveBits")
        if error is not None:
            return self._settle(error=error)
        digest = _HASHES.get(_name_of(options.get("hash")))
        if (str(options.get("name", "")).upper() != "PBKDF2" or digest is None
                or str(base_key.get_algorithm().get("name", "")).upper() != "PBKDF2"):
            return self._settle(error=_unsupported('deriveKey', str(options.get("name"))))
        salt = _bytes_of(options.get("salt"), 'deriveKey', 1)
        rounds = int(integer(options.get("iterations"), signed=True))
        material = hashlib.pbkdf2_hmac(digest, base_key._material, salt, rounds, bits // 8)
        if name in _VALID_USAGES:
            _checked_usages(name, _usages(usages))
        view = (_hmac_algorithm(target) if name == "HMAC"
                else {"name": name, "length": bits})
        if name == "HMAC":
            view["length"] = bits
        return self._settle(CryptoKey("secret", bool(extractable), view, _usages(usages),
                                      material))

    # -- what is not backed here ------------------------------------------------
    def fn_encrypt(self, algorithm=MISSING, key=MISSING, data=MISSING, *_extra):
        return self._settle(error=_unsupported('encrypt', _name_of(algorithm)))

    def fn_decrypt(self, algorithm=MISSING, key=MISSING, data=MISSING, *_extra):
        return self._settle(error=_unsupported('decrypt', _name_of(algorithm)))

    def fn_wrapKey(self, format=MISSING, key=MISSING, wrapping_key=MISSING,
                   wrap_algorithm=MISSING, *_extra):
        return self._settle(error=_unsupported('wrapKey', _name_of(wrap_algorithm)))

    def fn_unwrapKey(self, format=MISSING, wrapped_key=MISSING, unwrapping_key=MISSING,
                     unwrap_algorithm=MISSING, *_extra):
        return self._settle(error=_unsupported('unwrapKey', _name_of(unwrap_algorithm)))

    def fn_getPublicKey(self, key=MISSING, *_extra):
        return self._settle(error=_unsupported('getPublicKey', "asymmetric"))

    def fn_encapsulateBits(self, algorithm=MISSING, *_extra):
        return self._settle(error=_unsupported('encapsulateBits', _name_of(algorithm)))

    def fn_decapsulateBits(self, algorithm=MISSING, *_extra):
        return self._settle(error=_unsupported('decapsulateBits', _name_of(algorithm)))

    def fn_encapsulateKey(self, algorithm=MISSING, *_extra):
        return self._settle(error=_unsupported('encapsulateKey', _name_of(algorithm)))

    def fn_decapsulateKey(self, algorithm=MISSING, *_extra):
        return self._settle(error=_unsupported('decapsulateKey', _name_of(algorithm)))

    def fn_supports(self, algorithm=MISSING, *_extra):
        """Whether this build performs the operation, which is what the method asks."""
        if algorithm is MISSING:
            raise missing_error('SubtleCrypto', 'supports', 1, 0, constructor=False)
        name = _name_of(algorithm)
        return self._settle(name in _HASHES or name in ("HMAC", "PBKDF2"))


@interface('Crypto', None)
class Crypto:
    def __init__(self, secure=False, services=None, **_):
        self._secure = bool(secure)
        self._services = services
        self._subtle = None

    def get_subtle(self):
        """Chrome hands out a SubtleCrypto only where the origin is trustworthy."""
        if not self._secure:
            return pyv8.Undefined
        if self._subtle is None:
            self._subtle = SubtleCrypto(services=self._services)
        return self._subtle

    def fn_randomUUID(self, *_extra):
        return str(uuid.uuid4()) if self._secure else pyv8.Undefined

    def fn_getRandomValues(self, array=MISSING, *_extra):
        if array is MISSING:
            raise missing_error('Crypto', 'getRandomValues', 1, 0, constructor=False)
        info = array._array_buffer_view_info() if isinstance(array, pyv8.JSObject) else None
        if info is None:
            raise PageTypeError("Failed to execute 'getRandomValues' on 'Crypto': parameter 1 "
                                "is not of type 'ArrayBufferView'.")
        if array._buffer_kind() == 'shared-view':
            raise PageTypeError("Failed to execute 'getRandomValues' on 'Crypto': "
                                "The provided ArrayBufferView value must not be shared.")
        kind, length = info
        if kind not in ('Int8Array', 'Uint8Array', 'Uint8ClampedArray', 'Int16Array',
                        'Uint16Array', 'Int32Array', 'Uint32Array', 'BigInt64Array',
                        'BigUint64Array'):
            raise dom_exception(
                'TypeMismatchError',
                "Failed to execute 'getRandomValues' on 'Crypto': The provided "
                f"ArrayBufferView is of type '{kind.removesuffix('Array')}', "
                "which is not an integer array type.")
        if length > _RANDOM_QUOTA:
            # Chrome caps one call at the entropy it is willing to hand out.
            raise dom_exception(
                "QuotaExceededError",
                "Failed to execute 'getRandomValues' on 'Crypto': The ArrayBufferView's byte"
                f" length ({length}) exceeds the number of bytes of entropy available via"
                f" this API ({_RANDOM_QUOTA}).")
        if length:
            data = os.urandom(length)
            array._write_buffer_bytes(data)
        return array

__all__ = ["Crypto", "CryptoKey", "SubtleCrypto"]
