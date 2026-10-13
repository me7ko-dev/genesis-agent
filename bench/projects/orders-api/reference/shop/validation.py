import re

from flask import request

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(status)
        self.status = status
        self.body = body


def fail(errors):
    raise ApiError(400, {"errors": errors})


def not_found(what):
    raise ApiError(404, {"error": f"няма {what}"})


def conflict(message):
    raise ApiError(409, {"error": message})


def json_object():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        fail({"body": "тялото трябва да е JSON обект"})
    return body


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def check_name(body, errors):
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        errors["name"] = "задължително, непразен текст"
        return None
    return name.strip()


def check_price(body, errors):
    price = body.get("price_cents")
    if not is_int(price) or price <= 0:
        errors["price_cents"] = "цяло число над 0"
        return None
    return price


def pagination():
    errors = {}
    values = []
    for key, default, top in (("page", 1, None), ("per_page", 20, 100)):
        raw = request.args.get(key)
        try:
            v = default if raw is None else int(raw)
        except ValueError:
            v = 0
        if v < 1 or (top is not None and v > top):
            errors[key] = f"цяло число от 1{f' до {top}' if top else ''}"
        values.append(v)
    if errors:
        fail(errors)
    return values[0], values[1]
