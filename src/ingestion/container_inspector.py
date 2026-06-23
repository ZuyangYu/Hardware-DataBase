import os
import zipfile
from dataclasses import dataclass, field


OOXML_EXTENSIONS = {".docx", ".xlsx"}
EMBEDDED_PREFIXES = (
    "word/embeddings/",
    "xl/embeddings/",
)
MEDIA_PREFIXES = (
    "word/media/",
    "xl/media/",
)


@dataclass
class ContainerInspection:
    file_name: str
    embedded_objects: list[str] = field(default_factory=list)
    media_objects: list[str] = field(default_factory=list)
    warning: str = ""

    @property
    def has_unprocessed_children(self) -> bool:
        return bool(self.embedded_objects or self.media_objects)

    def to_warning_message(self) -> str:
        if not self.has_unprocessed_children:
            return ""
        parts = []
        if self.embedded_objects:
            parts.append(f"{len(self.embedded_objects)} embedded object(s)")
        if self.media_objects:
            parts.append(f"{len(self.media_objects)} media object(s)")
        return (
            f"{self.file_name}: detected {', '.join(parts)}; "
            "embedded content is not expanded into child pipelines yet."
        )

    def to_metadata(self) -> dict:
        return {
            "embedded_object_count": len(self.embedded_objects),
            "media_object_count": len(self.media_objects),
            "embedded_objects": self.embedded_objects[:20],
            "media_objects": self.media_objects[:20],
            "warning": self.warning or self.to_warning_message(),
        }


def inspect_container_file(file_path: str) -> ContainerInspection:
    file_name = os.path.basename(file_path)
    extension = os.path.splitext(file_name.lower())[1]
    inspection = ContainerInspection(file_name=file_name)
    if extension not in OOXML_EXTENSIONS:
        return inspection
    if not zipfile.is_zipfile(file_path):
        inspection.warning = f"{file_name}: OOXML package could not be inspected."
        return inspection

    try:
        with zipfile.ZipFile(file_path) as archive:
            names = archive.namelist()
    except (OSError, zipfile.BadZipFile) as exc:
        inspection.warning = f"{file_name}: OOXML package inspection failed: {exc}"
        return inspection

    inspection.embedded_objects = [
        name for name in names if name.startswith(EMBEDDED_PREFIXES) and not name.endswith("/")
    ]
    inspection.media_objects = [
        name for name in names if name.startswith(MEDIA_PREFIXES) and not name.endswith("/")
    ]
    return inspection
