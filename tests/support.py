"""Shared switches for tests that exercise withdrawn capabilities.

Several features are built, tested, and switched off in the shipped build:
AI file editing, automatic test execution, and the three AI CLI slots. Tests
that cover those paths turn the relevant switch on explicitly and restore it
afterwards.

Explicit is the point. When a capability was withdrawn, every test that had
been quietly relying on it broke at once, which was noise rather than signal —
the tests could not say whether they meant "this must work in the shipped
build" or "this is how the machinery behaves when enabled".
"""

from __future__ import annotations

import unittest

from src import config


def _set_flag(test_case: unittest.TestCase, name: str, value: bool) -> None:
    test_case.addCleanup(setattr, config, name, getattr(config, name))
    setattr(config, name, value)


def enable_implementation_writes(test_case: unittest.TestCase) -> None:
    """Allow WriteGrant construction and write-mode launches for one test.

    Ships off (`config.IMPLEMENTATION_WRITES_ENABLED`) because there is no OS
    sandbox to confine an editing CLI. Tests of the grant machinery still need
    to build one.
    """
    _set_flag(test_case, "IMPLEMENTATION_WRITES_ENABLED", True)


def enable_all_slots(test_case: unittest.TestCase) -> None:
    """Open the three AI CLI slots for one test.

    All three ship closed. Tests that merely need "an agent whose argv we do
    not rebuild" say so here rather than depending on which slots happen to be
    open.
    """
    for flag in (
        "CLAUDE_SLOT_ENABLED",
        "CODEX_SLOT_ENABLED",
        "ANTIGRAVITY_SLOT_ENABLED",
    ):
        _set_flag(test_case, flag, True)


def enable_automatic_tests(test_case: unittest.TestCase) -> None:
    """Let the implementation phase run the project's suite for one test."""
    _set_flag(test_case, "RUN_TESTS_AUTOMATICALLY", True)
