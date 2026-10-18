"""Password-free connection profile serialization."""

from __future__ import annotations

import configparser
from dataclasses import dataclass


PROFILE_PREFIX = "profile:"


@dataclass(frozen=True)
class ConnectionProfile:
    name: str
    mysql_path: str
    host: str
    port: str
    user: str


def profiles_from_config(config: configparser.ConfigParser) -> dict[str, ConnectionProfile]:
    profiles = {}
    for section in config.sections():
        if not section.casefold().startswith(PROFILE_PREFIX):
            continue
        name = section[len(PROFILE_PREFIX):].strip()
        if not name:
            continue
        values = config[section]
        profiles[name] = ConnectionProfile(
            name=name,
            mysql_path=values.get("mysql_path", ""),
            host=values.get("host", "127.0.0.1"),
            port=values.get("port", "3306"),
            user=values.get("user", "root"),
        )
    return profiles


def add_profiles_to_config(
    config: configparser.ConfigParser,
    profiles: dict[str, ConnectionProfile],
) -> None:
    for name in sorted(profiles, key=str.casefold):
        profile = profiles[name]
        config[f"{PROFILE_PREFIX}{name}"] = {
            "mysql_path": profile.mysql_path,
            "host": profile.host,
            "port": profile.port,
            "user": profile.user,
        }
