"""Формирование и проверка подписи Prodamus, порт класса Hmac.php."""

import hashlib
import hmac
import json
import re

__all__ = ["HmacHelper", "flatten_form", "parse_bracket_form"]


class HmacHelper:
    @staticmethod
    def create(data, key, algo="sha256"):
        """Возвращает подпись данных в виде hex-строки в нижнем регистре."""
        payload = {k: v for k, v in data.items() if k != "signature"}
        normalized = _sort_recursive(_stringify_recursive(payload))
        raw = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        raw = raw.replace("/", "\\/")
        digest = hmac.new(
            key.encode("utf-8"), raw.encode("utf-8"), getattr(hashlib, algo)
        )
        return digest.hexdigest()

    @staticmethod
    def verify(data, key, sign, algo="sha256"):
        """Сравнивает подпись данных с полученной подписью."""
        expected = HmacHelper.create(data, key, algo)
        return hmac.compare_digest(expected, str(sign).lower())


def _stringify_recursive(value):
    """Приводит скаляры к строкам по правилам PHP-функции strval()."""
    if isinstance(value, dict):
        return {str(k): _stringify_recursive(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stringify_recursive(item) for item in value]
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else ""
    return str(value)


def _sort_recursive(value):
    if isinstance(value, dict):
        return {key: _sort_recursive(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sort_recursive(item) for item in value]
    return value


def flatten_form(data, prefix=""):
    """Разворачивает вложенную структуру в плоские ключи ``products[0][name]``.

    Обратная операция к :func:`parse_bracket_form`. Ссылка на оплату должна
    передавать ровно то дерево, от которого посчитана подпись, поэтому строку
    запроса собираем из тех же данных, а не переписываем поля руками.
    """
    items = []

    for key, value in data.items() if isinstance(data, dict) else enumerate(data):
        full_key = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, (dict, list, tuple)):
            items.extend(flatten_form(value, full_key))
        else:
            items.append((full_key, "" if value is None else str(value)))

    return items


_KEY_PATTERN = re.compile(r"^([^\[]+)((?:\[[^\]]*\])+)$")
_SEGMENT_PATTERN = re.compile(r"\[([^\]]*)\]")


def parse_bracket_form(flat):
    """Разбирает плоские ключи вида ``products[0][name]`` во вложенную структуру.

    Webhook приходит как ``application/x-www-form-urlencoded``, и подпись
    считается от того же дерева, которое на своей стороне собирает PHP
    в ``$_POST``.
    """
    result = {}

    for key, value in flat.items():
        match = _KEY_PATTERN.match(key)
        if not match:
            result[key] = value
            continue

        root = match.group(1)
        segments = _SEGMENT_PATTERN.findall(match.group(2))

        if root not in result or not isinstance(result[root], (dict, list)):
            result[root] = [] if _is_index(segments[0]) else {}

        current = result[root]

        for i, segment in enumerate(segments):
            is_last = i == len(segments) - 1
            container = _is_index(segments[i + 1]) if not is_last else None

            if isinstance(current, list):
                index = len(current) if segment == "" else int(segment)
                while len(current) <= index:
                    current.append(None)
                if is_last:
                    current[index] = value
                    break
                if not isinstance(current[index], (dict, list)):
                    current[index] = [] if container else {}
                current = current[index]
            else:
                if is_last:
                    current[segment] = value
                    break
                if not isinstance(current.get(segment), (dict, list)):
                    current[segment] = [] if container else {}
                current = current[segment]

    return result


def _is_index(segment):
    return segment == "" or segment.isdigit()