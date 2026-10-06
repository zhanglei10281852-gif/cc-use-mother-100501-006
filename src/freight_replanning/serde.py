"""数据类与 JSON 之间的通用转换，支撑文件持久化与 API 输出。"""

from __future__ import annotations

import types
from dataclasses import MISSING, fields, is_dataclass
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints


def to_jsonable(value: Any) -> Any:
    """把数据类、枚举、元组等递归转成 JSON 可序列化结构。"""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    return value


def _convert(annotation: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return _convert(args[0], value)
        return value
    if origin in (list, tuple):
        args = get_args(annotation)
        item_type = args[0] if args and args[0] is not Ellipsis else Any
        items = [_convert(item_type, item) for item in value]
        return tuple(items) if origin is tuple else items
    if origin is dict:
        args = get_args(annotation)
        value_type = args[1] if len(args) == 2 else Any
        return {key: _convert(value_type, item) for key, item in value.items()}
    if isinstance(annotation, type):
        if issubclass(annotation, Enum):
            return annotation(value)
        if is_dataclass(annotation):
            return from_jsonable(annotation, value)
    return value


def from_jsonable(cls: type, data: dict) -> Any:
    """按类型标注重建数据类实例，缺失字段回退到默认值。"""
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name in data:
            kwargs[f.name] = _convert(hints[f.name], data[f.name])
        elif f.default is MISSING and f.default_factory is MISSING:
            raise ValueError(f"{cls.__name__} 缺少字段 {f.name}")
    return cls(**kwargs)
