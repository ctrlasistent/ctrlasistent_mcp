# tool_websearch.py
# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.

import re
import logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs

from core.CtrlAsistent_tools import Tool

logger = logging.getLogger(__name__)


class WebSearch(Tool):

    @property
    def name(self):
        return "ddg_search"

    @property
    def description(self):
        return (
            "Pretražuje internet koristeći DuckDuckGo. "
            "Vraća do 9 rezultata s naslovom, URL-om i kratkim opisom."
        )

    @property
    def parameters_schema(self):
        return {
            "query": {
                "type": "string",
                "description": "Upit za pretraživanje interneta. Prezentiraj korisniku i url",
            }
        }

    def execute(self, query: str) -> str:
        logger.info("WEB_SEARCH: query=%r", query)
        url = "https://lite.duckduckgo.com/lite/"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml," "application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "hr-HR,hr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://lite.duckduckgo.com/",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        }

        try:
            response = requests.post(
                url,
                data={"q": query},
                headers=headers,
                timeout=15,
                allow_redirects=True,
            )

        except requests.RequestException as e:
            logger.error("WEB_SEARCH HTTP GREŠKA: %s", e)
            return f"Greška pri pristupu DuckDuckGo: {e}"

        logger.info(
            "WEB_SEARCH: HTTP=%s URL=%s",
            response.status_code,
            response.url,
        )

        if response.status_code != 200:
            logger.warning(
                "WEB_SEARCH: DuckDuckGo vratio HTTP %s",
                response.status_code,
            )
            return (
                "DuckDuckGo nije vratio rezultate.\n"
                f"HTTP status: {response.status_code}"
            )

        html = response.text

        logger.info(
            "WEB_SEARCH: primljeno HTML znakova=%d",
            len(html),
        )

        soup = BeautifulSoup(html, "html.parser")

        results = []

        for link in soup.select("a.result-link"):
            title = link.get_text(" ", strip=True)
            href = link.get("href", "")

            if not title or not href:
                continue

            # DuckDuckGo ponekad koristi redirect:
            # https://duckduckgo.com/l/?uddg=...
            try:
                parsed = urlparse(href)
                qs = parse_qs(parsed.query)

                if "uddg" in qs:
                    href = qs["uddg"][0]

            except Exception:
                pass

            description = ""

            row = link.find_parent("tr")

            if row:
                # DDG Lite: snippet je u sljedećem <tr> ispod linka
                next_row = row.find_next_sibling("tr")
                snippet_cell = None
                if next_row:
                    snippet_cell = next_row.find("td", class_="result-snippet")
                    if not snippet_cell:
                        # fallback: uzmi najdulji tekst iz sljedećeg reda
                        for cell in next_row.find_all("td"):
                            t = cell.get_text(" ", strip=True)
                            if t and len(t) > 20 and not re.fullmatch(r"\d+\.?", t):
                                snippet_cell = cell
                                break

                if snippet_cell:
                    description = snippet_cell.get_text(" ", strip=True)

            # Čišćenje opisa
            description = re.sub(
                r"\s+",
                " ",
                description,
            ).strip()

            description = re.sub(
                r"https?://\S+",
                "",
                description,
            ).strip()

            description = re.sub(
                r"^[\s:|–—-]+",
                "",
                description,
            ).strip()

            # Maksimalno 500 znakova opisa
            if len(description) > 500:
                description = description[:500].rsplit(" ", 1)[0] + "..."

            results.append(
                {
                    "title": title,
                    "url": href,
                    "description": description,
                }
            )

            # Maksimalno 9 rezultata
            if len(results) >= 9:
                break

        logger.info(
            "WEB_SEARCH: pronađeno rezultata=%d",
            len(results),
        )

        if not results:
            logger.warning(
                "WEB_SEARCH: nema rezultata. HTML početak=%r",
                html[:500],
            )
            return (
                "DuckDuckGo je odgovorio, ali nisam pronašao rezultate.\n"
                f"HTTP: {response.status_code}\n"
                f"HTML: {len(html)} znakova"
            )

        output = [
            "REZULTATI WEB PRETRAGE",
            f"Upit: {query}",
            "",
        ]

        for i, result in enumerate(results, 1):
            output.append(f"IZVOR {i}")
            output.append(f"Naslov: {result['title']}")

            if result["description"]:
                output.append(f"Opis: {result['description']}")

            output.append(f"URL: {result['url']}")
            output.append("")

        return "\n".join(output)
