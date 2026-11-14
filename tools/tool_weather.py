# tool_prognoza.py
# Contributors to the CtrlAsistent project
#
# The authors disclaim copyright to this source code.
# This work is dedicated to the public domain.
#
# May you share freely, never taking more than you give.

import requests
from typing import Any, Dict

from core.CtrlAsistent_tools import Tool


class WeatherTool(Tool):
    @property
    def name(self) -> str:
        return "weather"

    @property
    def description(self) -> str:
        return "Dohvaća trenutnu temperaturu i vremenske uvjete za zadani grad."

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {"city": {"type": "string", "description": "Ime grada (npr. Zagreb)"}}

    def execute(self, city: str) -> str:
        geo_url = "https://geocoding-api.open-meteo.com/v1/search"
        weather_url = "https://api.open-meteo.com/v1/forecast"
        timeout = (3, 10)  # povezivanje: 3 s, odgovor: 10 s

        try:
            geo_response = requests.get(
                geo_url,
                params={"name": city, "count": 1, "format": "json"},
                timeout=timeout,
            )
            geo_response.raise_for_status()
            geo = geo_response.json()

            if not geo.get("results"):
                return f"Nisam pronašao grad '{city}'."

            loc = geo["results"][0]
            lat, lon, name = loc["latitude"], loc["longitude"], loc["name"]

            weather_response = requests.get(
                weather_url,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current_weather": True,
                },
                timeout=timeout,
            )
            weather_response.raise_for_status()
            weather = weather_response.json()

            cur = weather["current_weather"]
            temp = cur["temperature"]
            wind = cur["windspeed"]
            wind_dir = cur["winddirection"]
            code = cur["weathercode"]

        except requests.exceptions.Timeout:
            return "Zahtjev prema vremenskom servisu je istekao. Pokušaj ponovo."
        except requests.exceptions.RequestException:
            return "Nije moguće povezati se s vremenskim servisom. Pokušaj ponovo."
        except (KeyError, ValueError, TypeError):
            return "Vremenski servis je vratio neočekivan odgovor."

        kode = {
            0: "vedro",
            1: "pretežno vedro",
            2: "djelomično oblačno",
            3: "oblačno",
            45: "magla",
            51: "slaba kiša",
            61: "kiša",
            80: "pljusak",
        }
        desc = kode.get(code, f"nepoznato ({code})")

        return (
            f"Trenutno vrijeme za {name}: {temp}°C, {desc}, "
            f"brzina vjetra {wind} km/h, smjer vjetra {wind_dir}."
        )
