"""Endpoint model for node connection points."""

from enum import StrEnum

from pydantic import BaseModel, Field


class EndpointProtocol(StrEnum):
    """Protocol types for node endpoints."""

    TCP = "tcp"
    UDP = "udp"
    HTTP = "http"
    HTTPS = "https"
    NFS = "nfs"
    GRPC = "grpc"
    WEBSOCKET = "ws"
    SSH = "ssh"
    MYSQL = "mysql"
    POSTGRES = "postgres"
    REDIS = "redis"
    MONGODB = "mongodb"


class EndpointDirection(StrEnum):
    """Direction of endpoint (input accepts connections, output initiates them)."""

    INPUT = "input"
    OUTPUT = "output"


class Endpoint(BaseModel):
    """A typed connection point that a node exposes or consumes."""

    name: str = Field(..., description="Endpoint name (e.g., 'public-https', 'db-internal')")
    protocol: EndpointProtocol = Field(default=EndpointProtocol.TCP)
    port: int = Field(..., ge=1, le=65535, description="Port number")
    direction: EndpointDirection = Field(
        default=EndpointDirection.INPUT,
        description="Input accepts connections, output initiates them",
    )
    domains: list[str] = Field(default_factory=list, description="Optional list of domains served")
    attributes: dict[str, str | int | bool] = Field(default_factory=dict, description="Additional configuration")

    model_config = {"extra": "forbid"}
