#!/usr/bin/env python3
# Copyright (c) 2023 FIRST.ORG, Inc., Red Hat, and contributors
# SPDX-License-Identifier: BSD-2-Clause
"""Strict CVSS v4.0 Base-vector parser and scorer.

Base-only Python port of FIRST's reference algorithm:
https://github.com/FIRSTdotorg/cvss-v4-calculator
"""

from __future__ import annotations

import sys
from decimal import Decimal, ROUND_HALF_UP
from itertools import product


class CVSS4Error(ValueError):
    pass


BASE_ORDER = ("AV", "AC", "AT", "PR", "UI", "VC", "VI", "VA", "SC", "SI", "SA")
VALUES = {
    "AV": frozenset("NALP"),
    "AC": frozenset("LH"),
    "AT": frozenset("NP"),
    "PR": frozenset("NLH"),
    "UI": frozenset("NPA"),
    "VC": frozenset("HLN"),
    "VI": frozenset("HLN"),
    "VA": frozenset("HLN"),
    "SC": frozenset("HLN"),
    "SI": frozenset("HLN"),
    "SA": frozenset("HLN"),
}

_LOOKUP_DATA = """000000=10
000001=9.9
000010=9.8
000011=9.5
000020=9.5
000021=9.2
000100=10
000101=9.6
000110=9.3
000111=8.7
000120=9.1
000121=8.1
000200=9.3
000201=9
000210=8.9
000211=8
000220=8.1
000221=6.8
001000=9.8
001001=9.5
001010=9.5
001011=9.2
001020=9
001021=8.4
001100=9.3
001101=9.2
001110=8.9
001111=8.1
001120=8.1
001121=6.5
001200=8.8
001201=8
001210=7.8
001211=7
001220=6.9
001221=4.8
002001=9.2
002011=8.2
002021=7.2
002101=7.9
002111=6.9
002121=5
002201=6.9
002211=5.5
002221=2.7
010000=9.9
010001=9.7
010010=9.5
010011=9.2
010020=9.2
010021=8.5
010100=9.5
010101=9.1
010110=9
010111=8.3
010120=8.4
010121=7.1
010200=9.2
010201=8.1
010210=8.2
010211=7.1
010220=7.2
010221=5.3
011000=9.5
011001=9.3
011010=9.2
011011=8.5
011020=8.5
011021=7.3
011100=9.2
011101=8.2
011110=8
011111=7.2
011120=7
011121=5.9
011200=8.4
011201=7
011210=7.1
011211=5.2
011220=5
011221=3
012001=8.6
012011=7.5
012021=5.2
012101=7.1
012111=5.2
012121=2.9
012201=6.3
012211=2.9
012221=1.7
100000=9.8
100001=9.5
100010=9.4
100011=8.7
100020=9.1
100021=8.1
100100=9.4
100101=8.9
100110=8.6
100111=7.4
100120=7.7
100121=6.4
100200=8.7
100201=7.5
100210=7.4
100211=6.3
100220=6.3
100221=4.9
101000=9.4
101001=8.9
101010=8.8
101011=7.7
101020=7.6
101021=6.7
101100=8.6
101101=7.6
101110=7.4
101111=5.8
101120=5.9
101121=5
101200=7.2
101201=5.7
101210=5.7
101211=5.2
101220=5.2
101221=2.5
102001=8.3
102011=7
102021=5.4
102101=6.5
102111=5.8
102121=2.6
102201=5.3
102211=2.1
102221=1.3
110000=9.5
110001=9
110010=8.8
110011=7.6
110020=7.6
110021=7
110100=9
110101=7.7
110110=7.5
110111=6.2
110120=6.1
110121=5.3
110200=7.7
110201=6.6
110210=6.8
110211=5.9
110220=5.2
110221=3
111000=8.9
111001=7.8
111010=7.6
111011=6.7
111020=6.2
111021=5.8
111100=7.4
111101=5.9
111110=5.7
111111=5.7
111120=4.7
111121=2.3
111200=6.1
111201=5.2
111210=5.7
111211=2.9
111220=2.4
111221=1.6
112001=7.1
112011=5.9
112021=3
112101=5.8
112111=2.6
112121=1.5
112201=2.3
112211=1.3
112221=0.6
200000=9.3
200001=8.7
200010=8.6
200011=7.2
200020=7.5
200021=5.8
200100=8.6
200101=7.4
200110=7.4
200111=6.1
200120=5.6
200121=3.4
200200=7
200201=5.4
200210=5.2
200211=4
200220=4
200221=2.2
201000=8.5
201001=7.5
201010=7.4
201011=5.5
201020=6.2
201021=5.1
201100=7.2
201101=5.7
201110=5.5
201111=4.1
201120=4.6
201121=1.9
201200=5.3
201201=3.6
201210=3.4
201211=1.9
201220=1.9
201221=0.8
202001=6.4
202011=5.1
202021=2
202101=4.7
202111=2.1
202121=1.1
202201=2.4
202211=0.9
202221=0.4
210000=8.8
210001=7.5
210010=7.3
210011=5.3
210020=6
210021=5
210100=7.3
210101=5.5
210110=5.9
210111=4
210120=4.1
210121=2
210200=5.4
210201=4.3
210210=4.5
210211=2.2
210220=2
210221=1.1
211000=7.5
211001=5.5
211010=5.8
211011=4.5
211020=4
211021=2.1
211100=6.1
211101=5.1
211110=4.8
211111=1.8
211120=2
211121=0.9
211200=4.6
211201=1.8
211210=1.7
211211=0.7
211220=0.8
211221=0.2
212001=5.3
212011=2.4
212021=1.4
212101=2.4
212111=1.2
212121=0.5
212201=1
212211=0.3
212221=0.1"""
LOOKUP = {
    key: float(value)
    for key, value in (line.split("=", 1) for line in _LOOKUP_DATA.splitlines())
}

MAX_COMPOSED = {
    1: {
        0: ("AV:N/PR:N/UI:N/",),
        1: ("AV:A/PR:N/UI:N/", "AV:N/PR:L/UI:N/", "AV:N/PR:N/UI:P/"),
        2: ("AV:P/PR:N/UI:N/", "AV:A/PR:L/UI:P/"),
    },
    2: {
        0: ("AC:L/AT:N/",),
        1: ("AC:H/AT:N/", "AC:L/AT:P/"),
    },
    3: {
        0: {
            0: ("VC:H/VI:H/VA:H/CR:H/IR:H/AR:H/",),
            1: (
                "VC:H/VI:H/VA:L/CR:M/IR:M/AR:H/",
                "VC:H/VI:H/VA:H/CR:M/IR:M/AR:M/",
            ),
        },
        1: {
            0: (
                "VC:L/VI:H/VA:H/CR:H/IR:H/AR:H/",
                "VC:H/VI:L/VA:H/CR:H/IR:H/AR:H/",
            ),
            1: (
                "VC:L/VI:H/VA:L/CR:H/IR:M/AR:H/",
                "VC:L/VI:H/VA:H/CR:H/IR:M/AR:M/",
                "VC:H/VI:L/VA:H/CR:M/IR:H/AR:M/",
                "VC:H/VI:L/VA:L/CR:M/IR:H/AR:H/",
                "VC:L/VI:L/VA:H/CR:H/IR:H/AR:M/",
            ),
        },
        2: {1: ("VC:L/VI:L/VA:L/CR:H/IR:H/AR:H/",)},
    },
    4: {
        0: ("SC:H/SI:S/SA:S/",),
        1: ("SC:H/SI:H/SA:H/",),
        2: ("SC:L/SI:L/SA:L/",),
    },
    5: {0: ("E:A/",), 1: ("E:P/",), 2: ("E:U/",)},
}

MAX_SEVERITY = {
    1: {0: 1, 1: 4, 2: 5},
    2: {0: 1, 1: 2},
    3: {0: {0: 7, 1: 6}, 1: {0: 8, 1: 8}, 2: {1: 10}},
    4: {0: 6, 1: 5, 2: 4},
    5: {0: 1, 1: 1, 2: 1},
}

LEVELS = {
    "AV": {"N": 0.0, "A": 0.1, "L": 0.2, "P": 0.3},
    "PR": {"N": 0.0, "L": 0.1, "H": 0.2},
    "UI": {"N": 0.0, "P": 0.1, "A": 0.2},
    "AC": {"L": 0.0, "H": 0.1},
    "AT": {"N": 0.0, "P": 0.1},
    "VC": {"H": 0.0, "L": 0.1, "N": 0.2},
    "VI": {"H": 0.0, "L": 0.1, "N": 0.2},
    "VA": {"H": 0.0, "L": 0.1, "N": 0.2},
    "SC": {"H": 0.1, "L": 0.2, "N": 0.3},
    "SI": {"S": 0.0, "H": 0.1, "L": 0.2, "N": 0.3},
    "SA": {"S": 0.0, "H": 0.1, "L": 0.2, "N": 0.3},
    "CR": {"H": 0.0, "M": 0.1, "L": 0.2},
    "IR": {"H": 0.0, "M": 0.1, "L": 0.2},
    "AR": {"H": 0.0, "M": 0.1, "L": 0.2},
}


def parse_base_vector(vector: str) -> dict[str, str]:
    if not isinstance(vector, str) or not vector:
        raise CVSS4Error("CVSS vector is empty")
    if vector.endswith("/"):
        raise CVSS4Error("CVSS vector has a trailing slash")
    parts = vector.split("/")
    if parts[0] != "CVSS:4.0":
        raise CVSS4Error("CVSS vector must start with CVSS:4.0")
    fields = parts[1:]
    if len(fields) != len(BASE_ORDER):
        raise CVSS4Error(
            f"CVSS Base vector must contain exactly {len(BASE_ORDER)} metrics"
        )
    parsed: dict[str, str] = {}
    for expected, field in zip(BASE_ORDER, fields):
        if field.count(":") != 1:
            raise CVSS4Error(f"malformed CVSS metric {field!r}")
        metric, value = field.split(":", 1)
        if metric != expected:
            raise CVSS4Error(
                f"CVSS metric order invalid: expected {expected}, got {metric or '<empty>'}"
            )
        if value not in VALUES[metric]:
            raise CVSS4Error(f"invalid CVSS value {metric}:{value}")
        parsed[metric] = value
    return parsed


def _macro(metrics: dict[str, str]) -> tuple[int, int, int, int, int, int]:
    av, pr, ui = (metrics[key] for key in ("AV", "PR", "UI"))
    if av == pr == ui == "N":
        eq1 = 0
    elif (av == "N" or pr == "N" or ui == "N") and av != "P":
        eq1 = 1
    else:
        eq1 = 2

    eq2 = 0 if metrics["AC"] == "L" and metrics["AT"] == "N" else 1

    vc, vi, va = (metrics[key] for key in ("VC", "VI", "VA"))
    if vc == vi == "H":
        eq3 = 0
    elif "H" in (vc, vi, va):
        eq3 = 1
    else:
        eq3 = 2

    eq4 = 1 if "H" in (metrics["SC"], metrics["SI"], metrics["SA"]) else 2
    eq5 = 0  # Base-only vectors default Exploit Maturity to Attacked.
    eq6 = 0 if (
        (vc == "H") or (vi == "H") or (va == "H")
    ) else 1  # CR/IR/AR default High for Base scoring.
    return eq1, eq2, eq3, eq4, eq5, eq6


def _parse_fragment(fragment: str) -> dict[str, str]:
    return {
        key: value
        for key, value in (
            item.split(":", 1) for item in fragment.strip("/").split("/") if item
        )
    }


def _next_macro_scores(
    macro: tuple[int, int, int, int, int, int]
) -> tuple[float | None, ...]:
    eq1, eq2, eq3, eq4, eq5, eq6 = macro

    def value(parts: tuple[int, ...]) -> float | None:
        return LOOKUP.get("".join(str(part) for part in parts))

    score1 = value((eq1 + 1, eq2, eq3, eq4, eq5, eq6))
    score2 = value((eq1, eq2 + 1, eq3, eq4, eq5, eq6))
    if eq3 == 0 and eq6 == 0:
        candidates = (
            value((eq1, eq2, eq3, eq4, eq5, eq6 + 1)),
            value((eq1, eq2, eq3 + 1, eq4, eq5, eq6)),
        )
        score36 = max(candidate for candidate in candidates if candidate is not None)
    elif (eq3, eq6) in ((1, 1), (0, 1)):
        score36 = value((eq1, eq2, eq3 + 1, eq4, eq5, eq6))
    elif (eq3, eq6) == (1, 0):
        score36 = value((eq1, eq2, eq3, eq4, eq5, eq6 + 1))
    else:
        score36 = value((eq1, eq2, eq3 + 1, eq4, eq5, eq6 + 1))
    score4 = value((eq1, eq2, eq3, eq4 + 1, eq5, eq6))
    score5 = value((eq1, eq2, eq3, eq4, eq5 + 1, eq6))
    return score1, score2, score36, score4, score5


def score_base_vector(vector: str) -> float:
    metrics = parse_base_vector(vector)
    if all(metrics[key] == "N" for key in ("VC", "VI", "VA", "SC", "SI", "SA")):
        return 0.0

    macro = _macro(metrics)
    key = "".join(str(part) for part in macro)
    try:
        macro_score = LOOKUP[key]
    except KeyError as exc:
        raise CVSS4Error(f"unsupported CVSS MacroVector {key}") from exc

    eq1, eq2, eq3, eq4, eq5, eq6 = macro
    fragments = (
        MAX_COMPOSED[1][eq1],
        MAX_COMPOSED[2][eq2],
        MAX_COMPOSED[3][eq3][eq6],
        MAX_COMPOSED[4][eq4],
        MAX_COMPOSED[5][eq5],
    )
    selected = dict(metrics)
    selected.update({"CR": "H", "IR": "H", "AR": "H", "E": "A"})

    distances = None
    for combination in product(*fragments):
        maximum = {}
        for fragment in combination:
            maximum.update(_parse_fragment(fragment))
        candidate = {
            metric: LEVELS[metric][selected[metric]] - LEVELS[metric][maximum[metric]]
            for metric in (
                "AV", "PR", "UI", "AC", "AT", "VC", "VI", "VA",
                "SC", "SI", "SA", "CR", "IR", "AR",
            )
        }
        if all(distance >= 0 for distance in candidate.values()):
            distances = candidate
            break
    if distances is None:
        raise CVSS4Error("CVSS vector has no matching maximum-severity vector")

    current = (
        distances["AV"] + distances["PR"] + distances["UI"],
        distances["AC"] + distances["AT"],
        sum(distances[key] for key in ("VC", "VI", "VA", "CR", "IR", "AR")),
        distances["SC"] + distances["SI"] + distances["SA"],
        0.0,
    )
    depths = (
        MAX_SEVERITY[1][eq1] * 0.1,
        MAX_SEVERITY[2][eq2] * 0.1,
        MAX_SEVERITY[3][eq3][eq6] * 0.1,
        MAX_SEVERITY[4][eq4] * 0.1,
        MAX_SEVERITY[5][eq5] * 0.1,
    )
    lower_scores = _next_macro_scores(macro)

    normalized = []
    for lower, severity_distance, depth in zip(lower_scores, current, depths):
        if lower is not None:
            normalized.append((macro_score - lower) * (severity_distance / depth))
    mean_distance = sum(normalized) / len(normalized) if normalized else 0.0
    raw = min(10.0, max(0.0, macro_score - mean_distance))
    return float(
        Decimal(str(raw + 1e-6)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    )


def tier_for_score(score: float) -> str:
    if score >= 9.0:
        return "P0"
    if score >= 7.0:
        return "P1"
    if score >= 4.0:
        return "P2"
    if score >= 0.1:
        return "P3"
    return "P4"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: cvss_v4.py <CVSS:4.0/Base-vector>", file=sys.stderr)
        return 2
    try:
        print(f"{score_base_vector(argv[0]):.1f}")
    except CVSS4Error as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
