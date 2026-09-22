"""Explicit settings loading; validation never constructs API clients."""
import os
from dataclasses import dataclass, field
from typing import Mapping

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    api_keys: tuple[str, ...] = field(repr=False)
    discord_token: str | None = field(repr=False)

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]):
        return cls(
            tuple(values[f"GOOGLE_API_KEY{i}"] for i in range(1, 7)
                  if values.get(f"GOOGLE_API_KEY{i}")),
            values.get("DISCORD_BOT_TOKEN"),
        )


def load_settings(path="./ini.env") -> Settings:
    # Preserve the original dotenv/environment precedence; only run explicitly.
    load_dotenv(dotenv_path=path)
    return Settings.from_mapping(os.environ)


def validate_settings(settings: Settings) -> None:
    if not settings.api_keys:
        raise ValueError("Google Generative AI API KEY ERROR!")
    if not settings.discord_token:
        raise ValueError("Discord client TOKEN ERROR!")
