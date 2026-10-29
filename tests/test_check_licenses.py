"""CI license gate (NFR-8): fail on GPL, AGPL and non-commercial licenses, warn on weaker copyleft."""

from __future__ import annotations

import json

import pytest

import check_licenses as cl


@pytest.mark.parametrize(
    "license_text",
    [
        "MIT",
        "MIT License",
        "BSD License",
        "BSD-3-Clause",
        "Apache-2.0",
        "Apache Software License",
        "ISC License (ISCL)",
    ],
)
def test_permissive_licenses_pass(license_text):
    assert cl.classify(license_text) == "ok"


@pytest.mark.parametrize(
    "license_text",
    [
        "GPL-3.0-only",
        "GPL-2.0-or-later",
        "GNU General Public License v3 (GPLv3)",
        "GNU General Public License v2 or later (GPLv2+)",
        "GNU General Public License",
        "AGPL-3.0-or-later",
        "GNU Affero General Public License v3",
        "SSPL-1.0",
        "BUSL-1.1",
        "Business Source License 1.1",
        "CC-BY-NC-4.0",
        "Non-Commercial use only",
        "PolyForm Noncommercial 1.0.0",
    ],
)
def test_licenses_that_forbid_commercial_use_fail(license_text):
    assert cl.classify(license_text) == "fail"


@pytest.mark.parametrize(
    "license_text",
    [
        "LGPL-3.0-only",
        "GNU Lesser General Public License v3 (LGPLv3)",
        "GNU Library or Lesser General Public License (LGPL)",
        "MPL-2.0",
        "Mozilla Public License 2.0 (MPL 2.0)",
        "EPL-2.0",
        "UNKNOWN",
        "",
    ],
)
def test_weak_copyleft_and_unknown_licenses_warn(license_text):
    assert cl.classify(license_text) == "warn"


def test_a_dual_license_passes_when_one_choice_is_permissive():
    assert cl.classify("GPL-2.0-or-later OR MIT") == "ok"


def test_all_of_several_licenses_apply():
    assert cl.classify("MIT AND GPL-3.0-only") == "fail"


def test_pip_licenses_joins_several_classifiers_with_a_semicolon_and_all_apply():
    assert cl.classify("MIT License;GNU General Public License v3 (GPLv3)") == "fail"


def test_check_lists_failures_and_warnings_by_package():
    packages = [
        {"Name": "good", "Version": "1.0", "License": "MIT License"},
        {"Name": "bad", "Version": "2.0", "License": "GPL-3.0-only"},
        {"Name": "meh", "Version": "3.0", "License": "LGPL-3.0-only"},
    ]
    fails, warns = cl.check(packages)
    assert [p["Name"] for p in fails] == ["bad"]
    assert [p["Name"] for p in warns] == ["meh"]


def test_an_allowlisted_package_is_skipped_whatever_its_name_style():
    packages = [{"Name": "My_Package", "Version": "1", "License": "GPL-3.0-only"}]
    fails, _ = cl.check(packages, allow={"my-package"})
    assert fails == []


def test_main_exits_1_on_a_failure_and_names_the_package(capsys):
    data = json.dumps([{"Name": "bad", "Version": "2.0", "License": "AGPL-3.0-only"}])
    assert cl.main(data) == 1
    assert "bad" in capsys.readouterr().out


def test_main_exits_0_when_only_warnings(capsys):
    data = json.dumps([{"Name": "meh", "Version": "3.0", "License": "LGPL-3.0-only"}])
    assert cl.main(data) == 0
    assert "meh" in capsys.readouterr().out
