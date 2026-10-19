"""Small host boundary for completion waiting; never selects a fallback host."""
import os
import desktop_notify as codex


def host_id():
    value=os.environ.get('MODEL_DESK_COMPLETION_HOST','codex')
    if value not in {'codex','dsh'}:
        raise ValueError('未知的完成通知宿主；不会回退到其他聊天')
    return value


def adapter():
    if host_id()=='dsh':
        import dsh_notify
        return dsh_notify
    return codex


def calling_thread(): return adapter().calling_thread()
def read_current(): return adapter().read_current()
def send_current(identity,prompt,call_id): return adapter().send_current(identity,prompt,call_id)
def hidden_options(): return codex.hidden_options()


def child_environment():
    if host_id()=='dsh': return adapter().child_environment()
    return codex.child_environment()


def authorization_context():
    return adapter().authorization_context() if host_id()=='dsh' else {}
