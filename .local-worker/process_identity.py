"""Bind a task child PID without storing its command line or credentials."""
import hashlib
import json
import math
import re


def command_digest(arguments):
    return hashlib.sha256(json.dumps(arguments, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def capture(pid):
    try:
        import psutil
        process = psutil.Process(pid)
        return {'pid': process.pid, 'created_at': process.create_time(),
                'command_sha256': command_digest(process.cmdline())}
    except (ImportError, OSError, ValueError):
        return None
    except Exception:
        # AccessDenied/NoSuchProcess are both absent evidence, never proof of death.
        return None


def well_formed(identity):
    """Shape check only: could this identity ever serve as match evidence?

    A valid pid alone is not an identity.  Missing fields, wrong types,
    non-finite or non-positive creation times and malformed digests all leave
    an identity unable to prove anything, alive or gone.
    """
    return (isinstance(identity, dict) and type(identity.get('pid')) is int
            and identity['pid'] > 0
            and type(identity.get('created_at')) in (int, float)
            and math.isfinite(identity['created_at']) and identity['created_at'] > 0
            and isinstance(identity.get('command_sha256'), str)
            and re.fullmatch(r'[0-9a-f]{64}', identity['command_sha256']) is not None)


def matches(identity, pid, created, arguments):
    if (not isinstance(identity, dict) or type(identity.get('pid')) is not int
            or identity['pid'] != pid or type(identity.get('created_at')) not in (int, float)
            or not math.isfinite(identity['created_at']) or identity['created_at'] <= 0
            or not isinstance(identity.get('command_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}',identity['command_sha256'])):
        return None
    return (abs(identity['created_at'] - created) < 0.001
            and identity['command_sha256'] == command_digest(arguments))
