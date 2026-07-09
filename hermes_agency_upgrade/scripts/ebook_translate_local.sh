#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage:
  ebook_translate_local.sh INPUT [OUTPUT] [TARGET_LANGUAGE] [--format epub|mobi|azw3|both] [--style comfortable|compact]

Local-only eBook translation and rebuild via Ollama's OpenAI-compatible endpoint.

Best path:
  INPUT .epub  -> translated, image-preserving, reader-optimized .epub
  INPUT .txt/.md -> translated reader-optimized .epub
  MOBI/AZW3 output is attempted when Calibre ebook-convert is installed.

Defaults:
  OLLAMA_OPENAI_BASE_URL=http://127.0.0.1:11434/v1
  OLLAMA_EBOOK_MODEL=qwen3:8b
  TARGET_LANGUAGE=Traditional Chinese
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
  usage
  exit $([[ $# -lt 1 ]] && echo 2 || echo 0)
fi

base_url="${OLLAMA_OPENAI_BASE_URL:-http://127.0.0.1:11434/v1}"
model="${OLLAMA_EBOOK_MODEL:-qwen3:8b}"
target_default="${HERMES_EBOOK_TARGET_LANG:-Traditional Chinese}"

python3 - "$base_url" "$model" "$target_default" "$@" <<'PY'
from __future__ import annotations

import argparse
import html
import json
import os
import pathlib
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from xml.etree import ElementTree as ET


BASE_URL = sys.argv[1].rstrip("/")
MODEL = sys.argv[2]
TARGET_DEFAULT = sys.argv[3]
ARGV = sys.argv[4:]
TIMEOUT = int(os.environ.get("HERMES_EBOOK_TIMEOUT", "600"))
CHUNK_CHARS = int(os.environ.get("HERMES_EBOOK_CHARS", "7000"))
BATCH_CHARS = int(os.environ.get("HERMES_EBOOK_BATCH_CHARS", "4200"))
BATCH_ITEMS = int(os.environ.get("HERMES_EBOOK_BATCH_ITEMS", "24"))

XHTML_NS = "http://www.w3.org/1999/xhtml"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
XML_NS = "http://www.w3.org/XML/1998/namespace"

ET.register_namespace("", XHTML_NS)
ET.register_namespace("opf", OPF_NS)
ET.register_namespace("dc", DC_NS)


def progress(event: str, **fields) -> None:
    payload = {"event": event, **fields}
    print("HERMES_PROGRESS " + json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


def die(message: str, code: int = 1) -> None:
    progress("error", task="ebook", model=MODEL, environment="local Ollama", message=message)
    raise SystemExit(message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("input")
    parser.add_argument("output", nargs="?")
    parser.add_argument("target_language", nargs="?")
    parser.add_argument("--format", choices=("epub", "mobi", "azw3", "both"), default=None)
    parser.add_argument("--style", choices=("comfortable", "compact"), default="comfortable")
    parser.add_argument("--help", "-h", action="store_true")
    ns, extras = parser.parse_known_args(ARGV)
    if ns.help:
        print("See shell usage.", file=sys.stderr)
        raise SystemExit(0)
    if extras:
        die("Unknown ebook options: " + " ".join(extras), 2)

    # Backward-compatible shorthand:
    #   /ebook book.epub "Traditional Chinese"
    # means target language, not output path.
    if ns.output and not ns.target_language:
        suffix = pathlib.Path(ns.output).suffix.lower()
        looks_like_path = suffix in {".epub", ".mobi", ".azw3", ".md", ".txt"} or "/" in ns.output or ns.output.startswith("~")
        if not looks_like_path:
            ns.target_language = ns.output
            ns.output = None
    ns.target_language = ns.target_language or TARGET_DEFAULT
    return ns


ARGS = parse_args()
SOURCE = pathlib.Path(ARGS.input).expanduser().resolve()
if not SOURCE.exists():
    die(f"Input not found: {SOURCE}", 2)


def requested_outputs() -> tuple[pathlib.Path, pathlib.Path | None, str]:
    output_arg = pathlib.Path(ARGS.output).expanduser() if ARGS.output else None
    suffix = output_arg.suffix.lower() if output_arg else ""
    fmt = ARGS.format
    if fmt is None:
        if suffix in {".mobi", ".azw3"}:
            fmt = suffix[1:]
        else:
            fmt = "epub"

    if output_arg is None:
        epub_path = SOURCE.with_suffix(".zh.epub")
    elif suffix == ".epub":
        epub_path = output_arg
    elif suffix in {".mobi", ".azw3"}:
        epub_path = output_arg.with_suffix(".epub")
    elif suffix:
        epub_path = output_arg.with_suffix(".epub")
    else:
        epub_path = output_arg.with_suffix(".epub")
    epub_path = epub_path.expanduser().resolve()

    convert_path: pathlib.Path | None = None
    if fmt == "both":
        convert_path = epub_path.with_suffix(".mobi")
    elif fmt in {"mobi", "azw3"}:
        if output_arg is not None and suffix == f".{fmt}":
            convert_path = output_arg.expanduser().resolve()
        else:
            convert_path = epub_path.with_suffix(f".{fmt}")

    return epub_path, convert_path, fmt


EPUB_OUTPUT, CONVERT_OUTPUT, OUTPUT_FORMAT = requested_outputs()


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1].lower()
    return tag.lower()


def ns_tag(ns: str, name: str) -> str:
    return f"{{{ns}}}{name}" if ns else name


def first_by_local(root: ET.Element, name: str) -> ET.Element | None:
    for item in root.iter():
        if local_name(item.tag) == name:
            return item
    return None


def all_by_local(root: ET.Element, name: str) -> list[ET.Element]:
    return [item for item in root.iter() if local_name(item.tag) == name]


def preserve_space(original: str, translated: str) -> str:
    prefix = original[: len(original) - len(original.lstrip())]
    suffix = original[len(original.rstrip()) :]
    return prefix + translated.strip() + suffix


def should_translate_text(text: str | None) -> bool:
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < 2:
        return False
    # English-to-Chinese path: skip pure numbers, punctuation, CJK-only text, and symbols.
    return bool(re.search(r"[A-Za-z]", stripped))


def split_batches(items: list[str], max_chars: int = BATCH_CHARS, max_items: int = BATCH_ITEMS):
    batch: list[str] = []
    chars = 0
    for item in items:
        size = len(item)
        if batch and (len(batch) >= max_items or chars + size > max_chars):
            yield batch
            batch = []
            chars = 0
        batch.append(item)
        chars += size
    if batch:
        yield batch


def extract_json_array(text: str):
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.I)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("[")
        end = stripped.rfind("]")
        if start >= 0 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


def request_translation(messages: list[dict]) -> str:
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.2,
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Local model request failed: {exc}. Check Ollama is running and model {MODEL!r} is pulled."
        ) from exc
    return body["choices"][0]["message"]["content"].strip()


def translate_items(items: list[str], target: str, status: str, done_ref: list[int], total_batches: int) -> list[str]:
    translated: list[str] = []
    for batch in split_batches(items):
        done_ref[0] += 1
        elapsed = time.time() - STARTED
        eta = (elapsed / max(done_ref[0] - 1, 1)) * (total_batches - done_ref[0] + 1) if done_ref[0] > 1 else None
        progress(
            "progress",
            status=status,
            model=MODEL,
            environment="local Ollama",
            chunk=done_ref[0],
            total=total_batches,
            percent=round((done_ref[0] - 1) / max(total_batches, 1) * 100, 1),
            elapsed_sec=round(elapsed, 1),
            eta_sec=round(eta, 1) if eta is not None else None,
        )
        system = (
            "You are a precise literary eBook translation engine. Translate English to the requested Chinese target. "
            "Keep meaning, paragraph tone, names, numbers, punctuation intent, and book style. "
            "Return only a JSON array of strings, same length and same order as input. "
            "No markdown fences, no comments, no numbering."
        )
        user = {
            "target_language": target,
            "items": batch,
        }
        raw = request_translation(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ]
        )
        try:
            parsed = extract_json_array(raw)
        except Exception:
            # Fallback for models that refuse strict JSON: translate item by item.
            parsed = []
            for item in batch:
                one = request_translation(
                    [
                        {
                            "role": "system",
                            "content": (
                                "Translate faithfully to the requested Chinese target. "
                                "Return only the translated text."
                            ),
                        },
                        {"role": "user", "content": f"Target: {target}\n\n{item}"},
                    ]
                )
                parsed.append(one)
        if not isinstance(parsed, list) or len(parsed) != len(batch):
            raise RuntimeError("Model returned an invalid translation batch shape.")
        translated.extend(str(item) for item in parsed)
        progress(
            "progress",
            status=status,
            model=MODEL,
            environment="local Ollama",
            chunk=done_ref[0],
            total=total_batches,
            percent=round(done_ref[0] / max(total_batches, 1) * 100, 1),
            elapsed_sec=round(time.time() - STARTED, 1),
            eta_sec=round(((time.time() - STARTED) / max(done_ref[0], 1)) * (total_batches - done_ref[0]), 1),
        )
    return translated


def readable_css(style: str) -> str:
    if style == "compact":
        p_margin = "0 0 0.55em 0"
        line_height = "1.65"
    else:
        p_margin = "0 0 0.9em 0"
        line_height = "1.82"
    return f"""/* Hermes optimized Chinese reading stylesheet. */
html, body {{
  margin: 0;
  padding: 0;
  line-height: {line_height};
  font-family: -apple-system, BlinkMacSystemFont, "PingFang TC", "PingFang SC",
    "Noto Serif CJK TC", "Noto Serif CJK SC", "Source Han Serif TC",
    "Microsoft JhengHei", "Heiti TC", serif;
  font-size: 1em;
  letter-spacing: 0.02em;
  word-break: break-word;
  overflow-wrap: anywhere;
  widows: 2;
  orphans: 2;
}}
body {{
  padding: 0 0.2em;
}}
p {{
  margin: {p_margin};
  text-indent: 2em;
  text-align: start;
}}
h1, h2, h3, h4, h5, h6 {{
  line-height: 1.35;
  margin: 1.45em 0 0.75em 0;
  text-indent: 0;
  font-weight: 700;
  page-break-after: avoid;
  break-after: avoid;
}}
blockquote {{
  margin: 1em 0;
  padding-left: 1em;
  border-left: 0.18em solid #888;
}}
blockquote p {{
  text-indent: 0;
}}
ul, ol {{
  margin: 0.85em 0 0.85em 1.3em;
  padding-left: 1.2em;
}}
li {{
  margin: 0.35em 0;
}}
img, svg {{
  max-width: 100%;
  height: auto;
  display: block;
  margin: 1em auto;
  page-break-inside: avoid;
  break-inside: avoid;
}}
figure {{
  margin: 1em 0;
  text-align: center;
}}
figcaption {{
  font-size: 0.92em;
  text-align: center;
  margin-top: 0.4em;
}}
table {{
  max-width: 100%;
  border-collapse: collapse;
}}
pre, code {{
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  word-break: normal;
  overflow-wrap: normal;
}}
"""


def secure_extract_epub(epub_path: pathlib.Path, out_dir: pathlib.Path) -> None:
    with zipfile.ZipFile(epub_path, "r") as zf:
        for member in zf.infolist():
            name = member.filename
            if not name or name.endswith("/"):
                continue
            target = (out_dir / name).resolve()
            if not str(target).startswith(str(out_dir.resolve())):
                raise RuntimeError(f"Unsafe EPUB path: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(member))


def container_opf_path(epub_dir: pathlib.Path) -> pathlib.PurePosixPath:
    container = epub_dir / "META-INF" / "container.xml"
    if not container.exists():
        raise RuntimeError("EPUB is missing META-INF/container.xml")
    root = ET.parse(container).getroot()
    for item in root.iter():
        if local_name(item.tag) == "rootfile" and item.get("full-path"):
            return pathlib.PurePosixPath(item.get("full-path", ""))
    raise RuntimeError("EPUB container does not point to an OPF package file.")


def opf_namespace(root: ET.Element) -> str:
    if root.tag.startswith("{"):
        return root.tag.split("}", 1)[0].strip("{")
    return ""


def manifest_map(opf_root: ET.Element) -> dict[str, dict]:
    manifest = first_by_local(opf_root, "manifest")
    if manifest is None:
        raise RuntimeError("OPF manifest missing.")
    result: dict[str, dict] = {}
    for item in list(manifest):
        if local_name(item.tag) == "item" and item.get("id") and item.get("href"):
            result[item.get("id", "")] = {
                "href": item.get("href", ""),
                "media_type": item.get("media-type", ""),
                "element": item,
            }
    return result


def spine_html_paths(opf_root: ET.Element, opf_path: pathlib.PurePosixPath) -> list[pathlib.PurePosixPath]:
    mapping = manifest_map(opf_root)
    spine = first_by_local(opf_root, "spine")
    if spine is None:
        return [
            opf_path.parent / pathlib.PurePosixPath(item["href"])
            for item in mapping.values()
            if item["media_type"] in {"application/xhtml+xml", "text/html"}
        ]
    paths: list[pathlib.PurePosixPath] = []
    for ref in list(spine):
        if local_name(ref.tag) != "itemref":
            continue
        item = mapping.get(ref.get("idref", ""))
        if item and item["media_type"] in {"application/xhtml+xml", "text/html"}:
            paths.append(opf_path.parent / pathlib.PurePosixPath(item["href"]))
    return paths


def ensure_css(epub_dir: pathlib.Path, opf_path: pathlib.PurePosixPath, opf_root: ET.Element, html_paths: list[pathlib.PurePosixPath]) -> None:
    opf_dir = opf_path.parent
    css_rel = pathlib.PurePosixPath("Styles/hermes-readable-zh.css")
    css_path = epub_dir / opf_dir / css_rel
    css_path.parent.mkdir(parents=True, exist_ok=True)
    css_path.write_text(readable_css(ARGS.style), encoding="utf-8")

    manifest = first_by_local(opf_root, "manifest")
    ns = opf_namespace(opf_root)
    if manifest is not None:
        exists = any(item.get("href") == str(css_rel) for item in list(manifest) if local_name(item.tag) == "item")
        if not exists:
            ET.SubElement(
                manifest,
                ns_tag(ns, "item"),
                {"id": "hermes-readable-zh-css", "href": str(css_rel), "media-type": "text/css"},
            )

    for html_rel in html_paths:
        html_path = epub_dir / html_rel
        if not html_path.exists():
            continue
        try:
            tree = ET.parse(html_path)
        except ET.ParseError:
            continue
        root = tree.getroot()
        head = first_by_local(root, "head")
        if head is None:
            continue
        rel_from_html = posixpath.relpath(str(opf_dir / css_rel), start=str(html_rel.parent))
        already = False
        for item in list(head):
            if local_name(item.tag) == "link" and item.get("href") == rel_from_html:
                already = True
        if not already:
            ET.SubElement(head, ns_tag(XHTML_NS, "link"), {"rel": "stylesheet", "type": "text/css", "href": rel_from_html})
        root.set("lang", "zh-Hant")
        root.set(f"{{{XML_NS}}}lang", "zh-Hant")
        tree.write(html_path, encoding="utf-8", xml_declaration=True)


def update_language_metadata(opf_root: ET.Element, target: str) -> None:
    language = "zh-Hant" if "Traditional" in target or "繁" in target or "Cantonese" in target else "zh"
    metadata = first_by_local(opf_root, "metadata")
    if metadata is None:
        return
    languages = [item for item in metadata.iter() if local_name(item.tag) == "language"]
    if languages:
        for item in languages:
            item.text = language
    else:
        ET.SubElement(metadata, ns_tag(DC_NS, "language")).text = language


def collect_text_refs(root: ET.Element) -> list[tuple[ET.Element, str, str]]:
    refs: list[tuple[ET.Element, str, str]] = []
    skip_depth = 0
    skip_tags = {"script", "style", "code", "pre", "kbd", "samp", "math", "svg"}

    def walk(elem: ET.Element, skipping: bool = False) -> None:
        tag = local_name(elem.tag)
        now_skip = skipping or tag in skip_tags
        if not now_skip and should_translate_text(elem.text):
            refs.append((elem, "text", elem.text or ""))
        for child in list(elem):
            walk(child, now_skip)
            if not now_skip and should_translate_text(child.tail):
                refs.append((child, "tail", child.tail or ""))

    walk(root)
    return refs


def translate_xhtml_file(path: pathlib.Path, target: str, done_ref: list[int], total_batches: int) -> int:
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise RuntimeError(f"Could not parse XHTML {path.name}: {exc}") from exc
    root = tree.getroot()
    refs = collect_text_refs(root)
    texts = [ref[2].strip() for ref in refs]
    if not texts:
        return 0
    translated = translate_items(texts, target, "translating EPUB text", done_ref, total_batches)
    for (elem, slot, original), value in zip(refs, translated):
        if slot == "text":
            elem.text = preserve_space(original, value)
        else:
            elem.tail = preserve_space(original, value)
    root.set("lang", "zh-Hant")
    root.set(f"{{{XML_NS}}}lang", "zh-Hant")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return len(refs)


def count_batches_for_epub(epub_dir: pathlib.Path, html_paths: list[pathlib.PurePosixPath]) -> int:
    total = 0
    for html_rel in html_paths:
        html_path = epub_dir / html_rel
        if not html_path.exists():
            continue
        try:
            refs = collect_text_refs(ET.parse(html_path).getroot())
        except ET.ParseError:
            continue
        texts = [ref[2].strip() for ref in refs]
        total += sum(1 for _ in split_batches(texts))
    return max(total, 1)


def write_epub(epub_dir: pathlib.Path, output: pathlib.Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    mimetype = epub_dir / "mimetype"
    if not mimetype.exists():
        mimetype.write_text("application/epub+zip", encoding="ascii")
    with zipfile.ZipFile(output, "w") as zf:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, mimetype.read_bytes())
        for path in sorted(epub_dir.rglob("*")):
            if path.is_dir() or path.name == ".DS_Store":
                continue
            rel = path.relative_to(epub_dir).as_posix()
            if rel == "mimetype":
                continue
            zf.write(path, rel, compress_type=zipfile.ZIP_DEFLATED)


def build_from_epub(source: pathlib.Path, output: pathlib.Path, target: str) -> pathlib.Path:
    with tempfile.TemporaryDirectory(prefix="hermes-epub-rebuild.") as tmp:
        epub_dir = pathlib.Path(tmp) / "epub"
        epub_dir.mkdir(parents=True)
        secure_extract_epub(source, epub_dir)
        opf_rel = container_opf_path(epub_dir)
        opf_path = epub_dir / opf_rel
        opf_tree = ET.parse(opf_path)
        opf_root = opf_tree.getroot()
        html_paths = spine_html_paths(opf_root, opf_rel)
        total_batches = count_batches_for_epub(epub_dir, html_paths)
        progress(
            "progress",
            status="preserving original images/assets and preparing EPUB spine",
            model=MODEL,
            environment="local EPUB rebuild",
            chunk=0,
            total=total_batches,
            percent=0,
            elapsed_sec=round(time.time() - STARTED, 1),
        )
        done = [0]
        for html_rel in html_paths:
            html_path = epub_dir / html_rel
            if html_path.exists():
                translate_xhtml_file(html_path, target, done, total_batches)
        ensure_css(epub_dir, opf_rel, opf_root, html_paths)
        update_language_metadata(opf_root, target)
        opf_tree.write(opf_path, encoding="utf-8", xml_declaration=True)
        progress(
            "progress",
            status="packing optimized EPUB",
            model=MODEL,
            environment="local EPUB rebuild",
            chunk=total_batches,
            total=total_batches,
            percent=100,
            elapsed_sec=round(time.time() - STARTED, 1),
            eta_sec=0,
        )
        write_epub(epub_dir, output)
    return output


def paragraphs_from_text(text: str) -> list[str]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n"))]
    return [block for block in blocks if block]


def build_minimal_text_epub(source: pathlib.Path, output: pathlib.Path, target: str) -> pathlib.Path:
    raw = source.read_text(encoding="utf-8", errors="replace")
    blocks = paragraphs_from_text(raw)
    total_batches = max(sum(1 for _ in split_batches(blocks)), 1)
    done = [0]
    translated = translate_items(blocks, target, "translating text book", done, total_batches)
    title = source.stem
    with tempfile.TemporaryDirectory(prefix="hermes-text-epub.") as tmp:
        root = pathlib.Path(tmp) / "epub"
        (root / "META-INF").mkdir(parents=True)
        (root / "OEBPS" / "Text").mkdir(parents=True)
        (root / "OEBPS" / "Styles").mkdir(parents=True)
        (root / "mimetype").write_text("application/epub+zip", encoding="ascii")
        (root / "META-INF" / "container.xml").write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""",
            encoding="utf-8",
        )
        (root / "OEBPS" / "Styles" / "hermes-readable-zh.css").write_text(readable_css(ARGS.style), encoding="utf-8")
        body_parts = []
        for block in translated:
            escaped = html.escape(block)
            if block.lstrip().startswith("#"):
                text = html.escape(block.lstrip("#").strip())
                body_parts.append(f"<h1>{text}</h1>")
            else:
                escaped = escaped.replace("\n", "<br/>")
                body_parts.append(f"<p>{escaped}</p>")
        chapter = f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="{XHTML_NS}" xml:lang="zh-Hant" lang="zh-Hant">
  <head>
    <title>{html.escape(title)}</title>
    <link rel="stylesheet" type="text/css" href="../Styles/hermes-readable-zh.css"/>
  </head>
  <body>
    <h1>{html.escape(title)}</h1>
    {''.join(body_parts)}
  </body>
</html>
"""
        (root / "OEBPS" / "Text" / "chapter001.xhtml").write_text(chapter, encoding="utf-8")
        nav = f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="{XHTML_NS}" xml:lang="zh-Hant" lang="zh-Hant">
  <head><title>目錄</title></head>
  <body><nav epub:type="toc" id="toc"><h1>目錄</h1><ol><li><a href="Text/chapter001.xhtml">{html.escape(title)}</a></li></ol></nav></body>
</html>
"""
        (root / "OEBPS" / "nav.xhtml").write_text(nav, encoding="utf-8")
        opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="{OPF_NS}" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="{DC_NS}">
    <dc:identifier id="bookid">hermes-{int(time.time())}</dc:identifier>
    <dc:title>{html.escape(title)}</dc:title>
    <dc:language>zh-Hant</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="css" href="Styles/hermes-readable-zh.css" media-type="text/css"/>
    <item id="chapter001" href="Text/chapter001.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="chapter001"/>
  </spine>
</package>
"""
        (root / "OEBPS" / "content.opf").write_text(opf, encoding="utf-8")
        write_epub(root, output)
    return output


def find_ebook_convert() -> str | None:
    candidates = [
        shutil.which("ebook-convert"),
        "/Applications/calibre.app/Contents/MacOS/ebook-convert",
        "/Applications/Calibre.app/Contents/MacOS/ebook-convert",
    ]
    for item in candidates:
        if item and pathlib.Path(item).exists():
            return item
    return None


def convert_with_calibre(epub_path: pathlib.Path, target_path: pathlib.Path) -> bool:
    exe = find_ebook_convert()
    if not exe:
        progress(
            "progress",
            status=f"MOBI/AZW3 skipped; Calibre ebook-convert is not installed. EPUB is ready.",
            model=MODEL,
            environment="local EPUB rebuild",
            output=str(epub_path),
            elapsed_sec=round(time.time() - STARTED, 1),
            eta_sec=0,
        )
        return False
    progress(
        "progress",
        status=f"converting EPUB to {target_path.suffix.lstrip('.').upper()}",
        model=MODEL,
        environment="Calibre ebook-convert",
        output=str(target_path),
        elapsed_sec=round(time.time() - STARTED, 1),
    )
    proc = subprocess.run(
        [exe, str(epub_path), str(target_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=int(os.environ.get("HERMES_EBOOK_CONVERT_TIMEOUT", "1800")),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ebook-convert failed with exit code {proc.returncode}: {proc.stdout[-1200:]}")
    return True


STARTED = time.time()
progress(
    "start",
    task="ebook",
    model=MODEL,
    environment=f"local Ollama + EPUB rebuild ({BASE_URL})",
    base_url=BASE_URL,
    input=str(SOURCE),
    output=str(EPUB_OUTPUT),
    target_language=ARGS.target_language,
    output_format=OUTPUT_FORMAT,
    style=ARGS.style,
    chunk_chars=CHUNK_CHARS,
)

suffix = SOURCE.suffix.lower()
if suffix == ".epub":
    epub_result = build_from_epub(SOURCE, EPUB_OUTPUT, ARGS.target_language)
elif suffix in {".txt", ".md", ".markdown"}:
    epub_result = build_minimal_text_epub(SOURCE, EPUB_OUTPUT, ARGS.target_language)
elif suffix in {".mobi", ".azw3"}:
    exe = find_ebook_convert()
    if not exe:
        die("MOBI/AZW3 input requires Calibre ebook-convert to convert the source into EPUB first.", 3)
    with tempfile.TemporaryDirectory(prefix="hermes-source-convert.") as tmp:
        source_epub = pathlib.Path(tmp) / "source.epub"
        proc = subprocess.run([exe, str(SOURCE), str(source_epub)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        if proc.returncode != 0:
            die(f"Could not convert source to EPUB: {proc.stdout[-1200:]}", 3)
        epub_result = build_from_epub(source_epub, EPUB_OUTPUT, ARGS.target_language)
else:
    die(f"Unsupported input type: {SOURCE.suffix}. Use EPUB, TXT, MD, or MOBI/AZW3 with Calibre.", 2)

final_outputs = [str(epub_result)]
if CONVERT_OUTPUT is not None:
    if convert_with_calibre(epub_result, CONVERT_OUTPUT):
        final_outputs.append(str(CONVERT_OUTPUT))

progress(
    "complete",
    task="ebook",
    model=MODEL,
    environment="local Ollama + optimized EPUB rebuild",
    output="; ".join(final_outputs),
    elapsed_sec=round(time.time() - STARTED, 1),
)
print("; ".join(final_outputs))
PY
