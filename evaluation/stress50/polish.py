"""Remove inherited title rules without changing any tested document body.

Retain the original input hash for evaluation provenance. Only styles.xml changes;
all document.xml bytes (including tables, headings and facts) stay identical.
"""

import hashlib
import io
import json
from pathlib import Path
from zipfile import ZipFile

from lxml import etree

ROOT = Path(__file__).resolve().parents[2] / "evaluation/generated/stress50"
NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def main():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    for spec in manifest["documents"][:40]:
        path = ROOT / "documents" / spec["filename"]
        original = path.read_bytes()
        result = io.BytesIO()
        with ZipFile(io.BytesIO(original)) as source, ZipFile(result, "w") as dest:
            for info in source.infolist():
                content = source.read(info.filename)
                if info.filename == "word/styles.xml":
                    tree = etree.fromstring(content)
                    for node in tree.xpath(
                        '//w:style[@w:styleId="Title"]/w:pPr/w:pBdr', namespaces=NS
                    ):
                        node.getparent().remove(node)
                    content = etree.tostring(
                        tree, xml_declaration=True, encoding="UTF-8", standalone=True
                    )
                dest.writestr(info, content)
        revised = result.getvalue()
        with ZipFile(io.BytesIO(original)) as a, ZipFile(io.BytesIO(revised)) as b:
            assert all(a.read(n) == b.read(n) for n in a.namelist() if n != "word/styles.xml")
        spec.setdefault("tested_sha256", hashlib.sha256(original).hexdigest())
        spec["sha256"] = hashlib.sha256(revised).hexdigest()
        spec["bytes"] = len(revised)
        spec["body_unchanged_after_style_qa"] = True
        path.write_bytes(revised)
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
