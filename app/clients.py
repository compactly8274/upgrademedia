import time
from pathlib import Path
import requests


class _Base:
    def __init__(self, base_url: str, headers: dict):
        self.base = base_url.strip().strip("<>").rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(headers)

    def _get(self, path: str, timeout: int = 60, **params):
        for wait in (0, 3, 6):
            if wait:
                time.sleep(wait)
            try:
                r = self.session.get(f"{self.base}{path}", params=params, timeout=timeout)
                r.raise_for_status()
                return r.json()
            except (requests.Timeout, requests.ConnectionError):
                if wait == 6:
                    raise

    def _post(self, path: str, payload: dict, timeout: int = 30):
        r = self.session.post(f"{self.base}{path}", json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str, timeout: int = 30, **params):
        r = self.session.delete(f"{self.base}{path}", params=params, timeout=timeout)
        r.raise_for_status()


class RadarrClient(_Base):
    def __init__(self, url: str, api_key: str):
        super().__init__(url, {"X-Api-Key": api_key, "Accept": "application/json"})

    def movies(self):
        return self._get("/api/v3/movie", timeout=120)

    def movie(self, movie_id: int):
        return self._get(f"/api/v3/movie/{movie_id}")

    def search(self, movie_id: int):
        return self._post("/api/v3/command", {"name": "MoviesSearch", "movieIds": [movie_id]})

    def delete_file(self, movie_file_id: int):
        self._delete(f"/api/v3/moviefile/{movie_file_id}")

    def delete(self, movie_id: int):
        self._delete(f"/api/v3/movie/{movie_id}", deleteFiles="true", addImportExclusion="true")

    def queue_movie_ids(self) -> set:
        try:
            data = self._get("/api/v3/queue", pageSize=1000)
            records = data.get("records", []) if isinstance(data, dict) else data
            return {int(r["movieId"]) for r in records if r.get("movieId")}
        except Exception:
            return set()


class SonarrClient(_Base):
    def __init__(self, url: str, api_key: str):
        super().__init__(url, {"X-Api-Key": api_key, "Accept": "application/json"})

    def series(self):
        return self._get("/api/v3/series", timeout=120)

    def episodes(self, series_id: int):
        return self._get("/api/v3/episode", seriesId=series_id)

    def search_series(self, series_id: int):
        return self._post("/api/v3/command", {"name": "SeriesSearch", "seriesId": series_id})

    def search_season(self, series_id: int, season_number: int):
        return self._post("/api/v3/command", {"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": season_number})

    def search_episode(self, ep_ids: list):
        return self._post("/api/v3/command", {"name": "EpisodeSearch", "episodeIds": ep_ids})

    def queue_series_ids(self) -> set:
        try:
            data = self._get("/api/v3/queue", pageSize=1000)
            records = data.get("records", []) if isinstance(data, dict) else data
            return {int(r["seriesId"]) for r in records if r.get("seriesId")}
        except Exception:
            return set()

    def delete(self, series_id: int):
        self._delete(f"/api/v3/series/{series_id}", deleteFiles="true", addImportListExclusion="true")


class PlexClient(_Base):
    def __init__(self, url: str, token: str):
        super().__init__(url, {"X-Plex-Token": token, "Accept": "application/json"})

    def sections(self):
        return self._get("/library/sections")["MediaContainer"].get("Directory", [])

    def all_items(self, key: str, type_: int):
        return self._get(f"/library/sections/{key}/all", type=type_)["MediaContainer"].get("Metadata", [])

    def delete_item(self, rating_key: str):
        self._delete(f"/library/metadata/{rating_key}")
