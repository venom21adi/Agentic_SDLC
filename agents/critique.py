"""Integrity rules shared by every critique gate (plan, code).

A critic's output is only accepted if each finding quotes the artifact verbatim, says where, and each
clean pillar is justified with specifics. This is what stops a critic from rubber-stamping or inventing issues.
"""
import re
import uuid
from typing import Any

from schemas import CritiqueItem, Pillar, Severity

MIN_FINDING_CHARS = 10
MIN_EVIDENCE_CHARS = 12  # normalized; keeps one-word "quotes" from counting as evidence
MIN_JUSTIFICATION_CHARS = 20


def flatten(obj: Any) -> str:
    """Render nested content as plain text (keys and string values) for quoting and matching."""
    if isinstance(obj, dict):
        return "\n".join(f"{k}\n{flatten(v)}" for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return "\n".join(flatten(v) for v in obj)
    return str(obj)


def normalize(text: str) -> str:
    """Lowercase and collapse everything but letters/digits, so quotes survive whitespace/punctuation drift."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


class CritiqueValidationError(ValueError):
    pass


def parse_critique(data: dict, corpus: str) -> tuple[list[CritiqueItem], dict[str, str]]:
    """
    Turn raw LLM output into findings, enforcing the integrity rules.
    Raises CritiqueValidationError listing every problem found.
    """
    pillars = data.get("pillars")
    if not isinstance(pillars, dict):
        raise CritiqueValidationError("response has no 'pillars' object")

    haystack = normalize(corpus)
    problems: list[str] = []
    items: list[CritiqueItem] = []
    justifications: dict[str, str] = {}

    for pillar in Pillar:
        block = pillars.get(pillar.value)
        if not isinstance(block, dict) or not isinstance(block.get("findings", []), list):
            problems.append(f"{pillar.value}: missing or malformed")
            continue
        findings = block.get("findings", [])

        if not findings:
            why = block.get("pass_justification")
            if not isinstance(why, str) or len(why.strip()) < MIN_JUSTIFICATION_CHARS:
                problems.append(f"{pillar.value}: no findings and no real pass_justification")
            else:
                justifications[pillar.value] = why.strip()
            continue

        for n, f in enumerate(findings, 1):
            tag = f"{pillar.value}[{n}]"
            if not isinstance(f, dict):
                problems.append(f"{tag}: not an object")
                continue
            try:
                severity = Severity(f.get("severity"))
            except ValueError:
                problems.append(f"{tag}: invalid severity {f.get('severity')!r}")
                continue
            text, evidence, ref = f.get("finding"), f.get("evidence"), f.get("reference")
            if not isinstance(text, str) or len(text.strip()) < MIN_FINDING_CHARS:
                problems.append(f"{tag}: finding text too short")
                continue
            if not isinstance(ref, str) or not ref.strip():
                problems.append(f"{tag}: no reference")
                continue
            ev = normalize(evidence) if isinstance(evidence, str) else ""
            if len(ev) < MIN_EVIDENCE_CHARS:
                problems.append(f"{tag}: evidence missing or too short to be a real quote")
                continue
            if ev not in haystack:
                problems.append(f"{tag}: evidence is not a quote from the artifact")
                continue
            items.append(CritiqueItem(
                id=str(uuid.uuid4()), pillar=pillar, severity=severity,
                finding=text.strip(), evidence=evidence.strip(), reference=ref.strip(),
            ))

    if problems:
        raise CritiqueValidationError("; ".join(problems))
    return items, justifications
