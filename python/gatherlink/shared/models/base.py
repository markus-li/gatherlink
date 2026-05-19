"""
Shared Pydantic model helpers for Gatherlink.

This module provides the common base for control-plane models. It keeps model
conversion explicit: external formats can first be wrapped in a named
``ConversionSource`` and then mapped into canonical Gatherlink models through
per-target field maps.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from pydantic.fields import FieldInfo

type SourceMapKey = type[Any] | str
type FieldMapValue = str | FieldTransform
T = TypeVar("T")
TargetModelT = TypeVar("TargetModelT", bound="GatherlinkBaseModel")


def described(description: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Attach a human-readable description to a conversion function."""

    def wrapper(func: Callable[..., Any]) -> Callable[..., Any]:
        setattr(func, "_description", description)
        return func

    return wrapper


@dataclass(frozen=True)
class FieldTransform:
    """
    Mapping rule for a target field.

    ``transform`` may be a callable or a constant value. When ``source`` is set,
    the source value is resolved first and passed to the callable. When ``source``
    is omitted, a callable is invoked without arguments or the constant is used as-is.
    """

    transform: Callable[..., Any] | Any
    source: str | None = None
    description: str | None = None

    def get_description(self) -> str:
        if self.description:
            return self.description
        if callable(self.transform):
            return getattr(self.transform, "_description", self.transform.__name__)
        return f"static: {self.transform!r}"

    def apply(self, source_obj: Any | None) -> Any:
        if self.source is None:
            return self.transform() if callable(self.transform) else self.transform
        raw = GatherlinkBaseModel.resolve_source_value(source_obj, self.source)
        return self.transform(raw) if callable(self.transform) else self.transform


class ConversionSource(BaseModel):
    """Named dictionary payload used when several input formats map to one model."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    source_format: str = Field(description="Stable name for the external input format.")
    data: dict[str, Any]
    friendly_name: str | None = None

    def get_friendly_name(self) -> str:
        return self.friendly_name or self.source_format


class GatherlinkBaseModel(BaseModel):
    """Base model with explicit import/export and cross-model mapping helpers."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid", validate_assignment=True)

    __field_maps__: ClassVar[dict[SourceMapKey, dict[str, FieldMapValue]]] = {}
    __friendly_name__: ClassVar[str | None] = None

    @classmethod
    def get_friendly_name(cls) -> str:
        return cls.__friendly_name__ or cls.__name__

    @staticmethod
    def _fieldrepr(field_info: FieldInfo | Any) -> str:
        if isinstance(field_info, FieldInfo):
            default = None if field_info.default is None else field_info.default
            if field_info.description:
                return field_info.description
        else:
            default = field_info

        if default is None:
            return "[empty]"
        if isinstance(default, bool):
            return "true" if default else "false"
        if isinstance(default, str):
            return default if default in {"true", "false"} else repr(default)
        return repr(default)

    @staticmethod
    def resolve_source_value(source_obj: Any, path: str, default: Any = None) -> Any:
        """Resolve a dotted path from a Pydantic model, mapping, or plain object."""
        current = source_obj.data if isinstance(source_obj, ConversionSource) else source_obj
        for part in path.split("."):
            if current is None:
                return default
            if isinstance(current, Mapping):
                current = current.get(part, default)
            else:
                current = getattr(current, part, default)
            if current is default:
                return default
        return current

    @classmethod
    def _resolve_field_map(
        cls, source_obj: Any, source_format: str | None = None
    ) -> tuple[SourceMapKey, dict[str, FieldMapValue]]:
        if source_format is not None:
            candidate_keys: list[SourceMapKey] = [source_format]
        elif isinstance(source_obj, ConversionSource):
            candidate_keys = [source_obj.source_format, type(source_obj)]
        else:
            candidate_keys = [type(source_obj)]

        if isinstance(source_obj, Mapping):
            candidate_keys.append(dict)

        for key in candidate_keys:
            if key in cls.__field_maps__:
                return key, cls.__field_maps__[key]

        available = ", ".join(key if isinstance(key, str) else key.__name__ for key in cls.__field_maps__)
        source_name = source_format or type(source_obj).__name__
        raise ValueError(f"No mapping defined from {source_name} to {cls.__name__}. Available: {available}")

    @classmethod
    def from_source(
        cls: type[TargetModelT],
        source_obj: Any,
        *,
        source_format: str | None = None,
        into_instance: TargetModelT | None = None,
    ) -> TargetModelT:
        """Build or update this model from a mapped source object."""
        _, field_map = cls._resolve_field_map(source_obj, source_format)
        init_data: dict[str, Any] = {}

        for target_field, mapping in field_map.items():
            if isinstance(mapping, str):
                value = cls.resolve_source_value(source_obj, mapping)
            elif isinstance(mapping, FieldTransform):
                value = mapping.apply(source_obj)
            else:
                raise TypeError(f"Invalid mapping for field {target_field!r}: {mapping!r}")
            init_data[target_field] = value

        if into_instance is not None:
            for field_name, value in init_data.items():
                setattr(into_instance, field_name, value)
            return into_instance

        return cls(**init_data)

    @classmethod
    def from_mapping(
        cls: type[TargetModelT],
        data: Mapping[str, Any],
        *,
        source_format: str = "dict",
        into_instance: TargetModelT | None = None,
    ) -> TargetModelT:
        """Build or update this model from a named dictionary format."""
        source = ConversionSource(source_format=source_format, data=dict(data))
        return cls.from_source(source, into_instance=into_instance)

    def to_model(self, target_cls: type[TargetModelT]) -> TargetModelT:
        """Convert this model to another Gatherlink model using the target's field map."""
        return target_cls.from_source(self)

    def export_dict(self, *, by_alias: bool = True, exclude_none: bool = True) -> dict[str, Any]:
        """Export a clean dictionary suitable for JSON/YAML serialization."""
        return self.model_dump(mode="json", by_alias=by_alias, exclude_none=exclude_none)

    @classmethod
    def generate_mapping_dict(
        cls,
        *,
        all_fields: bool = False,
        sample_source: Any | None = None,
    ) -> list[dict[str, str]]:
        """Return human-readable mapping metadata for docs, diagnostics, or tests."""
        mappings: list[dict[str, str]] = []
        mapped_targets: set[str] = set()

        for source_key, field_map in cls.__field_maps__.items():
            source_name = source_key if isinstance(source_key, str) else source_key.__name__
            for target_field, mapping in field_map.items():
                mapped_targets.add(target_field)
                source_value = transformed_value = None

                if isinstance(mapping, str):
                    source_field = mapping
                    transformation = "direct"
                    if sample_source is not None:
                        source_value = cls.resolve_source_value(sample_source, mapping)
                        transformed_value = source_value
                elif isinstance(mapping, FieldTransform):
                    source_field = mapping.source or "[constant]"
                    transformation = mapping.get_description()
                    if sample_source is not None:
                        source_value = (
                            cls.resolve_source_value(sample_source, mapping.source) if mapping.source else None
                        )
                        transformed_value = mapping.apply(sample_source)
                else:
                    raise TypeError(f"Unsupported mapping type: {type(mapping)!r}")

                mappings.append(
                    {
                        "source": str(source_name),
                        "target": cls.get_friendly_name(),
                        "source_field": source_field,
                        "target_field": target_field,
                        "transformation": transformation,
                        "source_value": cls._fieldrepr(source_value),
                        "transformed_value": cls._fieldrepr(transformed_value),
                    }
                )

        if all_fields:
            for target_field, field_info in cls.model_fields.items():
                if target_field in mapped_targets:
                    continue
                mappings.append(
                    {
                        "source": "[auto]",
                        "target": cls.get_friendly_name(),
                        "source_field": "[constant]",
                        "target_field": target_field,
                        "transformation": f"static: {cls._fieldrepr(field_info)}",
                        "source_value": "[empty]",
                        "transformed_value": cls._fieldrepr(field_info),
                    }
                )

        return mappings

    @classmethod
    def generate_mapping_text_report(cls, *, all_fields: bool = False, sample_source: Any | None = None) -> str:
        rows = cls.generate_mapping_dict(all_fields=all_fields, sample_source=sample_source)
        if not rows:
            return ""

        source_width = max(len(row["source_field"]) for row in rows) + 2
        target_width = max(len(row["target_field"]) for row in rows) + 2
        lines: list[str] = []
        current_source: str | None = None

        for row in rows:
            if row["source"] != current_source:
                current_source = row["source"]
                lines.append(f"\nMappings from: {current_source} -> {row['target']}")
            lines.append(
                f"  {row['source_field'].ljust(source_width)}-> "
                f"{row['target_field'].ljust(target_width)}"
                f"({row['transformation']})  "
                f"Source: {row['source_value']} -> Target: {row['transformed_value']}"
            )

        return "\n".join(lines)


class ExpirableModel(GatherlinkBaseModel):
    """Model with simple monotonic-enough wall-clock expiry metadata."""

    _creation_date: datetime = PrivateAttr(default_factory=lambda: datetime.now(UTC))
    _expiry_seconds: int = PrivateAttr(default=900)

    def __init__(self, **data: Any) -> None:
        expiry_seconds = data.pop("expiry_seconds", 900)
        super().__init__(**data)
        self._expiry_seconds = expiry_seconds

    def is_expired(self) -> bool:
        return (datetime.now(UTC) - self._creation_date).total_seconds() > self._expiry_seconds


class ListLikeModel[T](GatherlinkBaseModel):
    """Pydantic model wrapper that behaves like its first list field."""

    _list_field_name: str = PrivateAttr()

    def __init__(self, **data: Any) -> None:
        list_field_name = data.pop("_list_field_name", None)
        if not list_field_name:
            field_names = list(self.__class__.model_fields)
            if not field_names:
                raise ValueError(f"{self.__class__.__name__} must define at least one field.")
            list_field_name = field_names[0]
        super().__init__(**data)
        self._list_field_name = list_field_name

    def __getitem__(self, index: int | slice) -> T | ListLikeModel[T]:
        """Return one item or a sliced model from the wrapped list field."""
        list_field = getattr(self, self._list_field_name)
        if isinstance(index, slice):
            return self.__class__(
                **{
                    self._list_field_name: list_field[index],
                    **self.model_dump(exclude={self._list_field_name}),
                }
            )
        return list_field[index]

    def __len__(self) -> int:
        """Return the length of the wrapped list field."""
        return len(getattr(self, self._list_field_name))

    def __iter__(self) -> Iterator[T]:
        """Iterate over the wrapped list field."""
        return iter(getattr(self, self._list_field_name))


class ExpirableListLikeModel[T](ListLikeModel[T], ExpirableModel):
    """List-like model with expiry metadata."""


class GenericListResponse[T](ExpirableListLikeModel[T]):
    """Generic list response base that uses the subclass' first field as the list."""

    _detected_list_field_name: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Detect the first declared field on concrete list response subclasses."""
        super().__init_subclass__(**kwargs)
        field_names = list(getattr(cls, "__annotations__", {}))
        if field_names:
            cls._detected_list_field_name = field_names[0]

    def __init__(self, **data: Any) -> None:
        super().__init__(_list_field_name=self._detected_list_field_name, **data)
