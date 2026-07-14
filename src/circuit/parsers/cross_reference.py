from __future__ import annotations

from difflib import SequenceMatcher

from src.circuit.models import ComponentInstance, CrossReference, SchematicPage


def _normalize_label(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch.isalnum())


class CrossReferenceEngine:
    """Three-level EDF/PDF name matcher: exact, normalized, fuzzy."""

    def __init__(self, fuzzy_threshold: float = 0.86):
        self.fuzzy_threshold = fuzzy_threshold

    def match(
        self,
        instances: list[ComponentInstance],
        pages: list[SchematicPage],
    ) -> list[CrossReference]:
        labels = [
            label
            for page in pages
            for label in page.labels
            if label.kind == "refdes"
        ]
        exact = {label.text.upper(): label for label in labels}
        normalized = {_normalize_label(label.text): label for label in labels}

        refs: list[CrossReference] = []
        matched_labels: set[tuple[str, int]] = set()
        for instance in instances:
            refdes = instance.refdes.upper()
            label = exact.get(refdes)
            if label:
                refs.append(
                    CrossReference(
                        edf_refdes=instance.refdes,
                        pdf_label=label.text,
                        page_number=label.page_number,
                        confidence=1.0,
                        strategy="exact",
                    )
                )
                matched_labels.add((label.text, label.page_number))
                continue

            norm_ref = _normalize_label(refdes)
            label = normalized.get(norm_ref)
            if label:
                refs.append(
                    CrossReference(
                        edf_refdes=instance.refdes,
                        pdf_label=label.text,
                        page_number=label.page_number,
                        confidence=0.95,
                        strategy="normalized",
                    )
                )
                matched_labels.add((label.text, label.page_number))
                continue

            best_label = None
            best_score = 0.0
            for candidate in labels:
                if (candidate.text, candidate.page_number) in matched_labels:
                    continue
                score = SequenceMatcher(None, norm_ref, _normalize_label(candidate.text)).ratio()
                if score > best_score:
                    best_score = score
                    best_label = candidate
            if best_label and best_score >= self.fuzzy_threshold:
                refs.append(
                    CrossReference(
                        edf_refdes=instance.refdes,
                        pdf_label=best_label.text,
                        page_number=best_label.page_number,
                        confidence=round(best_score, 3),
                        strategy="fuzzy",
                    )
                )
                matched_labels.add((best_label.text, best_label.page_number))
        return refs
