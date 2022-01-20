# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import itertools
from typing import Iterable

import pytest

from pants.backend.python.pip_requirement import PipRequirement
from pants.backend.python.util_rules.interpreter_constraints import InterpreterConstraints
from pants.backend.python.util_rules.lockfile_metadata import (
    PythonLockfileMetadata,
    PythonLockfileMetadataV1,
    PythonLockfileMetadataV2,
)
from pants.core.util_rules.lockfile_metadata import calculate_invalidation_digest

INTERPRETER_UNIVERSE = ["2.7", "3.5", "3.6", "3.7", "3.8", "3.9", "3.10"]


def reqset(*a) -> set[PipRequirement]:
    return {PipRequirement.parse(i) for i in a}


def test_metadata_header_round_trip() -> None:
    input_metadata = PythonLockfileMetadata.new(
        InterpreterConstraints(["CPython==2.7.*", "PyPy", "CPython>=3.6,<4,!=3.7.*"]),
        reqset("ansicolors==0.1.0"),
    )
    serialized_lockfile = input_metadata.add_header_to_lockfile(
        b"req1==1.0", regenerate_command="./pants lock"
    )
    output_metadata = PythonLockfileMetadata.from_lockfile(serialized_lockfile)
    assert input_metadata == output_metadata


def test_add_header_to_lockfile() -> None:
    input_lockfile = b"""dave==3.1.4 \\
    --hash=sha256:cab0c0c0c0c0dadacafec0c0c0c0cafedadabeefc0c0c0c0feedbeeffeedbeef \\
    """

    expected = b"""
# This lockfile was autogenerated by Pants. To regenerate, run:
#
#    ./pants lock
#
# --- BEGIN PANTS LOCKFILE METADATA: DO NOT EDIT OR REMOVE ---
# {
#   "version": 2,
#   "valid_for_interpreter_constraints": [
#     "CPython>=3.7"
#   ],
#   "generated_with_requirements": [
#     "ansicolors==0.1.0"
#   ]
# }
# --- END PANTS LOCKFILE METADATA ---
dave==3.1.4 \\
    --hash=sha256:cab0c0c0c0c0dadacafec0c0c0c0cafedadabeefc0c0c0c0feedbeeffeedbeef \\
    """

    def line_by_line(b: bytes) -> list[bytes]:
        return [i for i in (j.strip() for j in b.splitlines()) if i]

    metadata = PythonLockfileMetadata.new(
        InterpreterConstraints([">=3.7"]), reqset("ansicolors==0.1.0")
    )
    result = metadata.add_header_to_lockfile(input_lockfile, regenerate_command="./pants lock")
    assert line_by_line(result) == line_by_line(expected)


def test_invalidation_digest() -> None:
    a = "flake8-pantsbuild>=2.0,<3"
    b = "flake8-2020>=1.6.0,<1.7.0"
    c = "flake8"

    def assert_eq(left: Iterable[str], right: Iterable[str]) -> None:
        assert calculate_invalidation_digest(left) == calculate_invalidation_digest(right)

    def assert_neq(left: Iterable[str], right: Iterable[str]) -> None:
        assert calculate_invalidation_digest(left) != calculate_invalidation_digest(right)

    for reqs in itertools.permutations([a, b, c]):
        assert_eq(reqs, [a, b, c])
        assert_neq(reqs, [a, b])

    assert_eq([], [])
    assert_neq([], [a])
    assert_eq([a, a, a, a], [a])


@pytest.mark.parametrize(
    "user_digest, expected_digest, user_ic, expected_ic, matches",
    [
        (
            "yes",
            "yes",
            [">=3.5.5"],
            [">=3.5, <=3.6"],
            False,
        ),  # User ICs contain versions in the 3.7 range
        ("yes", "yes", [">=3.5.5, <=3.5.10"], [">=3.5, <=3.6"], True),
        ("yes", "no", [">=3.5.5, <=3.5.10"], [">=3.5, <=3.6"], False),  # Digests do not match
        (
            "yes",
            "yes",
            [">=3.5.5, <=3.5.10"],
            [">=3.5", "<=3.6"],
            True,
        ),  # User ICs match each of the actual ICs individually
        (
            "yes",
            "yes",
            [">=3.5.5, <=3.5.10"],
            [">=3.5", "<=3.5.4"],
            True,
        ),  # User ICs do not match one of the individual ICs
        ("yes", "yes", ["==3.5.*, !=3.5.10"], [">=3.5, <=3.6"], True),
        (
            "yes",
            "yes",
            ["==3.5.*"],
            [">=3.5, <=3.6, !=3.5.10"],
            False,
        ),  # Excluded IC from expected range is valid for user ICs
        ("yes", "yes", [">=3.5, <=3.6", ">= 3.8"], [">=3.5"], True),
        (
            "yes",
            "yes",
            [">=3.5, <=3.6", ">= 3.8"],
            [">=3.5, !=3.7.10"],
            True,
        ),  # Excluded version from expected ICs is not in a range specified
    ],
)
def test_is_valid_for_v1(user_digest, expected_digest, user_ic, expected_ic, matches) -> None:
    m: PythonLockfileMetadata
    m = PythonLockfileMetadataV1(InterpreterConstraints(expected_ic), expected_digest)
    assert (
        bool(
            m.is_valid_for(
                user_digest,
                InterpreterConstraints(user_ic),
                INTERPRETER_UNIVERSE,
                set(),
            )
        )
        == matches
    )


@pytest.mark.parametrize(
    "user_ics_iter, expected_ics_iter, user_reqs, expected_reqs, matches",
    [
        # Exact requirements match
        [[">=3.5"], [">=3.5"], ["ansicolors==0.1.0"], ["ansicolors==0.1.0"], True],
        # Version mismatch
        [[">=3.5"], [">=3.5"], ["ansicolors==0.1.0"], ["ansicolors==0.1.1"], False],
        # Range specifier mismatch
        [[">=3.5"], [">=3.5"], ["ansicolors==0.1.0"], ["ansicolors>=0.1.0"], False],
        # Requirements mismatch
        [[">=3.5"], [">=3.5"], ["requests==1.0.0"], ["ansicolors==0.1.0"], False],
        # Multiple requirements
        [
            [">=3.5"],
            [">=3.5"],
            ["ansicolors==0.1.0", "requests==1.0.0"],
            ["ansicolors==0.1.0", "requests==1.0.0"],
            True,
        ],
        # Multiple requirements, order mismatch still passes
        [
            [">=3.5"],
            [">=3.5"],
            ["ansicolors==0.1.0", "requests==1.0.0"],
            ["requests==1.0.0", "ansicolors==0.1.0"],
            True,
        ],
        # Exact requirements match, non-matching ICs (user includes 3.7 range and above)
        [[">=3.5.5"], [">=3.5, <=3.6"], ["ansicolors==0.1.0"], ["ansicolors==0.1.0"], False],
    ],
)
def test_is_valid_for_v2_only(
    user_ics_iter, expected_ics_iter, user_reqs, expected_reqs, matches
) -> None:
    user_ic = InterpreterConstraints(user_ics_iter)
    expected_ic = InterpreterConstraints(expected_ics_iter)
    m = PythonLockfileMetadataV2(expected_ic, reqset(*expected_reqs))
    assert bool(m.is_valid_for("", user_ic, INTERPRETER_UNIVERSE, reqset(*user_reqs))) == matches