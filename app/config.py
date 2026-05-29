import os
from dataclasses import dataclass
from typing import Any

import yaml


@dataclass(frozen=True)
class User:
    name: str
    token_env: str

    def token(self) -> str:
        token = os.environ.get(self.token_env)
        if not token:
            raise RuntimeError(f"missing env var {self.token_env} for user {self.name}")
        return token


@dataclass(frozen=True)
class Config:
    plex_url: str
    label_name: str
    refresh_interval_seconds: int
    min_offset_ms: int
    libraries: list[str]
    users: list[User]


def load_config(path: str) -> Config:
    with open(path) as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}

    if "plex_url" not in data:
        raise ValueError("config: plex_url is required")
    if not data.get("libraries"):
        raise ValueError("config: at least one library is required")
    if not data.get("users"):
        raise ValueError("config: at least one user is required")

    users = [User(name=u["name"], token_env=u["token_env"]) for u in data["users"]]
    names = [u.name for u in users]
    if len(set(names)) != len(names):
        raise ValueError("config: duplicate user names")

    return Config(
        plex_url=data["plex_url"],
        label_name=data.get("label_name", "sync"),
        refresh_interval_seconds=int(data.get("refresh_interval_seconds", 600)),
        min_offset_ms=int(data.get("min_offset_ms", 30000)),
        libraries=list(data["libraries"]),
        users=users,
    )
