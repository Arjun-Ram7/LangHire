"""Read application links directly from SpeedyApply's published README."""
import asyncio
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

SPEEDYAPPLY_URL = "https://github.com/speedyapply/2027-SWE-College-Jobs"
README_URL = "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/README.md"
SECTIONS = {"FAANG+", "Other"}


class _Cell(HTMLParser):
    def __init__(self, value: str):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[str] = []
        self.feed(value)

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs).get("href") or "")
        elif tag == "br":
            self.parts.append(" ")

    @property
    def text(self):
        return " ".join("".join(self.parts).split())


def parse_readme(markdown: str) -> list[dict]:
    """Keep FAANG+/Other rows, taking URLs only from the Posting column."""
    rows = []
    section = ""
    headers = []
    seen_sections = set()
    for line in markdown.splitlines():
        if line.startswith("#"):
            section = line.lstrip("#").strip()
            headers = []
        if section not in SECTIONS or not line.startswith("|"):
            continue
        cells = [_Cell(value.replace(r"\|", "|")) for value in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        values = [cell.text.lower() for cell in cells]
        if {"company", "position", "location", "posting"}.issubset(values):
            headers = values
            seen_sections.add(section)
            continue
        if not headers or all(re.fullmatch(r":?-+:?", value) for value in values):
            continue
        if len(cells) != len(headers):
            raise ValueError(f"Unexpected SpeedyApply row in {section}: column count changed")
        fields = dict(zip(headers, cells))
        links = fields["posting"].links
        if not links:  # Closed listings have no Apply link.
            continue
        url = links[0].strip()
        if urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc:
            raise ValueError(f"Invalid SpeedyApply application URL in {section}")
        rows.append({
            "company": fields["company"].text,
            "position": fields["position"].text,
            "location": fields["location"].text,
            "url": url,
            "source_section": section,
            "salary": fields["salary"].text if "salary" in fields else "",
            "posting_age": fields["age"].text if "age" in fields else "",
        })
    if seen_sections != SECTIONS:
        raise ValueError("SpeedyApply README is missing the FAANG+ or Other table")
    return rows


def _download_readme() -> str:
    request = Request(README_URL, headers={"User-Agent": "LangHire/1.0"})
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


async def fetch_rows() -> list[dict]:
    return parse_readme(await asyncio.to_thread(_download_readme))
