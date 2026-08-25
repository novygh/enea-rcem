"""PSE RCEm client and parser."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
import logging
import re

from aiohttp import ClientError, ClientSession

from .const import RCEM_URL

_LOGGER = logging.getLogger(__name__)

MONTHS = {
    "styczen": 1,
    "luty": 2,
    "marzec": 3,
    "kwiecien": 4,
    "maj": 5,
    "czerwiec": 6,
    "lipiec": 7,
    "sierpien": 8,
    "wrzesien": 9,
    "pazdziernik": 10,
    "listopad": 11,
    "grudzien": 12,
}

_POLISH_MAP = str.maketrans("ąćęłńóśźż", "acelnoszz")


@dataclass(frozen=True, slots=True)
class RcemPrice:
    """One published RCEm value."""

    month: str
    price_pln_mwh: float
    published: str


class _TableParser(HTMLParser):
    """Minimal HTML table parser."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[tuple[str, str]]]] = []
        self._table: list[list[tuple[str, str]]] | None = None
        self._row: list[tuple[str, str]] | None = None
        self._cell_tag: str | None = None
        self._cell_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell_tag = tag
            self._cell_text = []
        elif tag == "br" and self._cell_tag is not None:
            self._cell_text.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell_tag is not None:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell_tag == tag and self._row is not None:
            text = " ".join("".join(self._cell_text).split())
            self._row.append((tag, text))
            self._cell_tag = None
            self._cell_text = []
        elif tag == "tr" and self._row is not None and self._table is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None


def _normalize_month(value: str) -> str:
    value = value.lower().translate(_POLISH_MAP)
    return re.sub(r"[^a-z]", "", value)


def parse_rcem_html(html: str) -> dict[str, RcemPrice]:
    """Parse PSE RCEm HTML and keep the newest publication per month."""
    parser = _TableParser()
    parser.feed(html)

    result: dict[str, RcemPrice] = {}

    for table in parser.tables:
        year: int | None = None
        month_name: str | None = None

        for row in table:
            headers = [text for tag, text in row if tag == "th"]
            cells = [text for tag, text in row if tag == "td"]

            if len(headers) == 1:
                match = re.search(r"20\d{2}", headers[0])
                if match:
                    year = int(match.group(0))
                    month_name = None
                continue

            if len(cells) == 1:
                normalized = _normalize_month(cells[0])
                if normalized in MONTHS:
                    month_name = normalized
                continue

            if year is None or month_name is None or len(cells) < 3:
                continue

            try:
                price = float(cells[1].replace(" ", "").replace(",", "."))
            except ValueError:
                continue

            published_match = re.search(r"\d{2}\.\d{2}\.\d{4}", cells[2])
            if not published_match:
                continue
            published_text = published_match.group(0)

            month = f"{year:04d}-{MONTHS[month_name]:02d}"
            item = RcemPrice(month=month, price_pln_mwh=price, published=published_text)
            existing = result.get(month)
            if existing is None:
                result[month] = item
                continue

            try:
                new_date = datetime.strptime(item.published, "%d.%m.%Y")
                old_date = datetime.strptime(existing.published, "%d.%m.%Y")
            except ValueError:
                continue
            if new_date > old_date:
                result[month] = item

    if not result:
        raise ValueError("No RCEm values found on PSE page")

    return result


class RcemClient:
    """Fetch RCEm data from PSE."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session

    async def async_fetch(self) -> dict[str, RcemPrice]:
        """Fetch and parse RCEm values."""
        try:
            async with self._session.get(RCEM_URL, timeout=30) as response:
                response.raise_for_status()
                html = await response.text()
        except (ClientError, TimeoutError) as err:
            raise ConnectionError(f"Unable to fetch PSE RCEm: {err}") from err

        prices = parse_rcem_html(html)
        _LOGGER.debug("Fetched %d RCEm months from PSE", len(prices))
        return prices
