"""
App adapters. Each one knows how to stand up a sandbox for one product and
gives the recreator agent tools to drive it. Mattermost is the first.

To add an app later (Teams, Outlook...), write a class with the same methods
as AppAdapter and add it to ADAPTERS below. Nothing else needs to change.
"""
from .base import AppAdapter
from .mattermost import MattermostAdapter

ADAPTERS: list[type[AppAdapter]] = [MattermostAdapter]


def adapter_for(app_name: str) -> type[AppAdapter] | None:
    return next((a for a in ADAPTERS if a.handles(app_name)), None)
