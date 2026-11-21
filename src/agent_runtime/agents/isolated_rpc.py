"""JSON-lines RPC between the evaluator's judge (trusted tests) and the child that runs the agent's code (S00-05).

Self-contained on purpose: the harness embeds this file's source into the judge runner and the child bootstrap, which
run with ``python -I`` and import nothing from this package.

Values cross by copy (None, bool, int, float, str, bytes, list, tuple, dict, set, frozenset); any other object crosses
as a handle. The judge may ``import``, read attributes of and call the child's objects. The child may only CALL what the
judge passed to it (e.g. an injected clock); it can never read the judge's attributes. No pickle anywhere: decoding the
child's output must never execute code in the judge.
"""
import base64
import builtins
import json
import math

MAX_DEPTH = 64
MAX_LINE = 8 * 1024 * 1024


class ProtocolError(Exception):
    pass


class RemoteError(Exception):
    """The child failed in a way that has no faithful local equivalent."""


def _builtin_exc_name(cls):
    for k in cls.__mro__:
        if getattr(builtins, k.__name__, None) is k and issubclass(k, BaseException):
            return k.__name__
    return "Exception"


class Endpoint:
    """One side of the connection. ``side`` is "judge" or "child"; ``read_line``/``write_line`` move bytes."""

    def __init__(self, side, read_line, write_line):
        self.side = side
        self.read_line = read_line
        self.write_line = write_line
        self.exported = {}          # handle -> local object handed to the other side
        self.handles = {}           # id(local object) -> handle (objects kept alive in self.exported)
        self.next_id = 1
        self.proxies = {}           # remote handle -> local proxy/class (stable identity)

    # ------------------------------------------------------------------ encoding
    def _export(self, obj):
        h = self.handles.get(id(obj))
        if h is None:
            h = self.next_id
            self.next_id += 1
            self.handles[id(obj)] = h
            self.exported[h] = obj
        return h

    def enc(self, v, depth=0):
        if depth > MAX_DEPTH:
            raise ProtocolError("value nested too deep")
        t = type(v)
        if v is None or t is bool or t is int or t is str:
            return v
        if t is float:
            return v if math.isfinite(v) else {"F": repr(v)}
        if t is list:
            return {"L": [self.enc(x, depth + 1) for x in v]}
        if t is tuple:
            return {"T": [self.enc(x, depth + 1) for x in v]}
        if t is dict:
            return {"D": [[self.enc(k, depth + 1), self.enc(x, depth + 1)] for k, x in v.items()]}
        if t is bytes:
            return {"B": base64.b64encode(v).decode("ascii")}
        if t is set or t is frozenset:
            return {"S" if t is set else "FS": [self.enc(x, depth + 1) for x in v]}
        own = self._own_handle_of_proxy(v)
        if own is not None:
            return own
        h = self._export(v)
        if self.side == "judge":
            return {"P": h}
        if isinstance(v, type) and issubclass(v, BaseException):
            return {"R": h, "k": "exc", "n": v.__name__, "b": _builtin_exc_name(v)}
        return {"R": h, "k": "call" if callable(v) else "obj", "n": getattr(v, "__qualname__", type(v).__name__)}

    def _own_handle_of_proxy(self, v):
        """A proxy of the OTHER side's object goes back as that side's handle."""
        h = getattr(v, "_p2s_handle", None) if type(v) in (RemoteRef, ParentRef) else None
        if h is None:
            for rh, cls in self.proxies.items():
                if cls is v:
                    h = rh
                    break
        if h is None:
            return None
        return {"R": h} if self.side == "judge" else {"P": h}

    def dec(self, v, depth=0):
        if depth > MAX_DEPTH:
            raise ProtocolError("value nested too deep")
        if v is None or type(v) in (bool, int, float, str):
            return v
        if type(v) is not dict or len(v) == 0:
            raise ProtocolError("malformed value")
        if "F" in v:
            return float(v["F"]) if v["F"] in ("inf", "-inf", "nan") else _bad("float")
        if "L" in v:
            return [self.dec(x, depth + 1) for x in v["L"]]
        if "T" in v:
            return tuple(self.dec(x, depth + 1) for x in v["T"])
        if "D" in v:
            return {self.dec(k, depth + 1): self.dec(x, depth + 1) for k, x in v["D"]}
        if "B" in v:
            return base64.b64decode(v["B"].encode("ascii"), validate=True)
        if "S" in v:
            return {self.dec(x, depth + 1) for x in v["S"]}
        if "FS" in v:
            return frozenset(self.dec(x, depth + 1) for x in v["FS"])
        if self.side == "judge":
            if "P" in v:
                return self.exported[_int(v["P"])]
            if "R" in v:
                return self._remote(v)
        else:
            if "R" in v:
                return self.exported[_int(v["R"])]
            if "P" in v:
                h = _int(v["P"])
                return self.proxies.setdefault(h, ParentRef(self, h))
        raise ProtocolError("unknown value tag")

    def _remote(self, v):
        h = _int(v["R"])
        if h in self.proxies:
            return self.proxies[h]
        if v.get("k") == "exc":
            base = getattr(builtins, str(v.get("b")), Exception)
            if not (isinstance(base, type) and issubclass(base, BaseException)):
                base = Exception
            obj = type(str(v.get("n", "RemoteException"))[:100], (base,), {"__module__": "p2s_remote", "_p2s_handle": h})
        else:
            obj = RemoteRef(self, h, str(v.get("n", "object"))[:100])
        self.proxies[h] = obj
        return obj

    # ------------------------------------------------------------------ transport
    def send(self, msg):
        self.write_line(json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\n")

    def recv(self):
        line = self.read_line()
        if not line:
            raise ProtocolError("connection closed")
        if len(line) > MAX_LINE:
            raise ProtocolError("message too large")
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError) as exc:
            raise ProtocolError(f"unreadable message: {type(exc).__name__}") from None
        if type(msg) is not dict:
            raise ProtocolError("message is not an object")
        return msg

    def request(self, msg):
        rid = self.next_id
        self.next_id += 1
        self.send({**msg, "id": rid})
        while True:
            m = self.recv()
            if "re" in m:
                if m["re"] != rid:
                    raise ProtocolError("reply out of order")
                if "ok" in m:
                    return self.dec(m["ok"])
                self._raise(m.get("exc"))
            elif "op" in m:
                self.serve(m)
            else:
                raise ProtocolError("unknown message")

    def _raise(self, exc):
        if type(exc) is not dict:
            raise ProtocolError("malformed exception")
        args = tuple(str(a)[:500] for a in (exc.get("a") or [])[:3])
        if exc.get("c") is not None:
            cls = self.dec(exc["c"]) if self.side == "judge" else None
            if isinstance(cls, type) and issubclass(cls, BaseException):
                raise cls(*args)
        name = str(exc.get("t") or "")
        cls = getattr(builtins, name, None)
        if isinstance(cls, type) and issubclass(cls, Exception):
            raise cls(*args)
        raise RemoteError(f"{name or 'remote error'}: {' '.join(args)}")

    def reply_exc(self, rid, e):
        t = type(e)
        builtin = getattr(builtins, t.__name__, None) is t
        self.send({"re": rid, "exc": {"t": t.__name__ if builtin else _builtin_exc_name(t),
                                      "c": None if builtin or self.side == "judge" else self.enc(t),
                                      "a": [str(a)[:500] for a in e.args[:3]]}})

    def serve(self, m):
        """Handle one request from the other side. The judge only lets the child CALL what it was given."""
        rid = m.get("id")
        try:
            op = m.get("op")
            if self.side == "judge" and op != "call":
                raise PermissionError("the evaluated code may only call what the tests passed to it")
            if op == "import":
                import importlib
                mod = importlib.import_module(str(m["mod"]))
                result = mod
            elif op == "attr":
                name = str(m["name"])
                if name.startswith("__") and name not in ("__name__", "__qualname__", "__doc__"):
                    raise AttributeError(name)
                result = getattr(self.dec(m["obj"]), name)
            elif op == "call":
                fn = self.dec(m["obj"])
                args = [self.dec(a) for a in m.get("a", [])]
                kwargs = {str(k): self.dec(x) for k, x in m.get("k", [])}
                result = fn(*args, **kwargs)
            else:
                raise ProtocolError(f"unknown op {op!r}")
            self.send({"re": rid, "ok": self.enc(result)})
        except ProtocolError:
            raise
        except Exception as e:  # noqa: BLE001 - every failure crosses as an exception, never as a value
            self.reply_exc(rid, e)


def _int(v):
    if type(v) is not int:
        raise ProtocolError("handle is not an integer")
    return v


def _bad(what):
    raise ProtocolError(f"malformed {what}")


class RemoteRef:
    """Judge-side proxy of a child object: attribute reads and calls go to the child."""

    __slots__ = ("_p2s_ep", "_p2s_handle", "_p2s_name")

    def __init__(self, ep, h, name):
        object.__setattr__(self, "_p2s_ep", ep)
        object.__setattr__(self, "_p2s_handle", h)
        object.__setattr__(self, "_p2s_name", name)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self._p2s_ep.request({"op": "attr", "obj": {"R": self._p2s_handle}, "name": name})

    def __call__(self, *args, **kwargs):
        ep = self._p2s_ep
        return ep.request({"op": "call", "obj": {"R": self._p2s_handle}, "a": [ep.enc(a) for a in args],
                           "k": [[k, ep.enc(v)] for k, v in kwargs.items()]})

    def __repr__(self):
        return f"<remote {self._p2s_name}>"


class ParentRef:
    """Child-side proxy of something the tests passed in: it can only be called."""

    __slots__ = ("_p2s_ep", "_p2s_handle")

    def __init__(self, ep, h):
        self._p2s_ep = ep
        self._p2s_handle = h

    def __call__(self, *args, **kwargs):
        ep = self._p2s_ep
        return ep.request({"op": "call", "obj": {"P": self._p2s_handle}, "a": [ep.enc(a) for a in args],
                           "k": [[k, ep.enc(v)] for k, v in kwargs.items()]})

    def __repr__(self):
        return "<judge callable>"
