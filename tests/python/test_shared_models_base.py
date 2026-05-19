from __future__ import annotations

from typing import ClassVar

from gatherlink.shared.models import (
    ConversionSource,
    FieldTransform,
    GatherlinkBaseModel,
    GenericListResponse,
    described,
)
from pydantic import BaseModel, Field


class ExternalPeer(BaseModel):
    name: str
    endpoint: str


@described("upper-case value")
def uppercase(value: str) -> str:
    return value.upper()


class CanonicalPeer(GatherlinkBaseModel):
    node: str
    endpoint: str
    role: str = "client"

    __field_maps__: ClassVar = {
        ExternalPeer: {
            "node": FieldTransform(uppercase, source="name"),
            "endpoint": "endpoint",
            "role": FieldTransform("relay"),
        },
        "minimal-client": {
            "node": "node.name",
            "endpoint": "peer.endpoint",
            "role": FieldTransform("client"),
        },
        "legacy-client": {
            "node": "hostname",
            "endpoint": "remote.addr",
            "role": FieldTransform(lambda: "client", description="legacy default role"),
        },
    }


class AliasedModel(GatherlinkBaseModel):
    local_port: int = Field(alias="localPort")


class PeerList(GenericListResponse[CanonicalPeer]):
    peers: list[CanonicalPeer]


def test_model_to_model_mapping() -> None:
    peer = CanonicalPeer.from_source(ExternalPeer(name="relay-a", endpoint="198.51.100.1:443"))

    assert peer == CanonicalPeer(node="RELAY-A", endpoint="198.51.100.1:443", role="relay")


def test_named_dict_formats_can_map_to_same_target_model() -> None:
    minimal = CanonicalPeer.from_mapping(
        {"node": {"name": "client-a"}, "peer": {"endpoint": "203.0.113.10:55180"}},
        source_format="minimal-client",
    )
    legacy = CanonicalPeer.from_mapping(
        {"hostname": "client-b", "remote": {"addr": "203.0.113.20:55180"}},
        source_format="legacy-client",
    )

    assert minimal == CanonicalPeer(node="client-a", endpoint="203.0.113.10:55180")
    assert legacy == CanonicalPeer(node="client-b", endpoint="203.0.113.20:55180")


def test_conversion_source_and_into_instance_update() -> None:
    source = ConversionSource(
        source_format="minimal-client",
        data={"node": {"name": "client-c"}, "peer": {"endpoint": "192.0.2.10:1"}},
    )
    existing = CanonicalPeer(node="old", endpoint="old")

    updated = CanonicalPeer.from_source(source, into_instance=existing)

    assert updated is existing
    assert existing.node == "client-c"
    assert existing.endpoint == "192.0.2.10:1"


def test_export_dict_uses_aliases_and_excludes_none() -> None:
    assert AliasedModel(localPort=55180).export_dict() == {"localPort": 55180}


def test_mapping_report_includes_transform_descriptions() -> None:
    rows = CanonicalPeer.generate_mapping_dict()
    text = CanonicalPeer.generate_mapping_text_report()

    assert any(row["transformation"] == "upper-case value" for row in rows)
    assert "minimal-client" in text
    assert "legacy default role" in text


def test_generic_list_response_behaves_like_list() -> None:
    peer = CanonicalPeer(node="client", endpoint="127.0.0.1:1")
    response = PeerList(peers=[peer])

    assert len(response) == 1
    assert response[0] == peer
    assert list(response) == [peer]
